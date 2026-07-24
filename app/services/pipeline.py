"""Full IDP pipeline: OCR → Classify → Group → Extract → Validate → Output."""

import logging
import time

from app.core.config import Settings
from app.schemas.context import PipelineContext
from app.schemas.extraction import (
    AssetExtraction,
    CertificationInfo,
    ExtractionResult,
    HouseholdDemographics,
    IncomeExtraction,
    PageOcrRecord,
    PreviousCertification,
    PreviousCertIncomeSource,
    VerificationIncomeEntry,
)
from app.services.bug_detector import detect_known_bugs
from app.services.cert_type_rules import validate_cert_type_requirements
from app.services.cross_doc_validator import (
    validate_asset_consistency,
    validate_asset_worksheet_rules,
    validate_cert_summary_vs_income,
    validate_duplicate_income,
    validate_household_consistency,
    validate_income_consistency,
    validate_rent_assistance,
    validate_tic_totals,
)
from app.services.extractor import (
    extract_assets,
    extract_certification_info,
    extract_demographics,
    extract_income,
)
from app.services.income_calculator import calculate_all_methods, match_paystubs_to_sources
from app.services.inventory_builder import build_financial_inventory, build_hud_inventory
from app.services.parsers.questionnaire_parser import parse_questionnaire
from app.services.questionnaire_extractor import (
    extract_questionnaire_disclosures,
    validate_affirmative_responses,
)
from app.services.field_scorer import (
    build_score_summary,
    score_business_rules,
    score_cross_doc_consistency,
)
from app.services.signature_validator import validate_signatures
from app.services.special_scenarios import check_special_scenarios
from app.services.two_pass_classifier import classify_and_group

logger = logging.getLogger(__name__)


def run_extraction_pipeline(
    page_texts: list[dict],
    settings: Settings,
    *,
    funding_program: str | None = None,
    certification_type: str | None = None,
    source_files: list[dict] | None = None,
) -> ExtractionResult:
    """Run the full extraction pipeline on OCR results.

    Args:
        page_texts: list of {"page": int, "text": str} from OCR stage
        settings: application settings

    Returns:
        ExtractionResult with all MuleSoft schemas populated
    """
    start = time.perf_counter()
    logger.info("Starting extraction pipeline for %d pages", len(page_texts))

    # Build pipeline context from API params or settings defaults
    ctx = PipelineContext(
        funding_program=funding_program or settings.funding_program or None,
        certification_type=certification_type or settings.certification_type_override or None,
    )

    # Build skip_pages set — blank/low-quality pages that should never go to extraction
    skip_pages: set[int] = set()
    # Build OCR quality map — used for source verification scoring
    ocr_quality: dict[int, dict] = {}  # page → {flag, score, text}
    for pt in page_texts:
        flag_details = pt.get("ocr_flag_details", [])
        ocr_quality[pt["page"]] = {
            "flag": pt.get("ocr_flag", "green"),
            "score": pt.get("ocr_score"),
            "text": pt.get("text", ""),
        }
        if isinstance(flag_details, list) and ("blank_page" in flag_details or "low_quality_scan" in flag_details):
            skip_pages.add(pt["page"])
        elif not pt.get("text", "").strip():
            skip_pages.add(pt["page"])

    # Steps 1+2: Two-pass classify and group
    # Pass 1: keyword classify + summarize (instant, no LLM)
    # Pass 2: LLM correct + group (one call, sees full file context)
    logger.info("Steps 1-2/6: Two-pass classification + grouping")
    classification, document_groups = classify_and_group(page_texts, settings)

    # With all case PDFs merged newest-first, a stale resubmission's cert
    # form appears further down the page order than the current one —
    # demote it so extraction never reads superseded numbers.
    if source_files and len(source_files) > 1:
        _demote_superseded_cert_groups(document_groups, source_files)

    # Step 3: Extract structured data — LLM handles ALL extraction
    # Classification routes documents, LLM reads content and maps to schema.
    # Works for any form type (HUD 50059, RD 3560-8, LIHTC TIC, etc.)
    logger.info("Step 3/6: Extracting structured data (LLM)")

    # Extract previous certification data (for IR delta comparison)
    previous_certification = _extract_previous_cert(document_groups)

    include_groups = [g for g in document_groups if g.category != "ignore"]

    # Filter out groups where ALL pages are blank/skip
    llm_eligible_groups = [
        g for g in include_groups
        if not all(p in skip_pages for p in g.pages)
    ]
    if len(llm_eligible_groups) < len(include_groups):
        skipped_count = len(include_groups) - len(llm_eligible_groups)
        logger.info("  Filtered %d groups with all-skip pages from extraction", skipped_count)

    # --- Route groups by extraction category ---
    # Only send relevant doc types to each LLM call to minimize tokens.
    # Each set matches the extractor's doc_type filter — no double filtering.

    _DEMO_TYPES = {
        "HUD 50059", "Tenant Income Certification (TIC)", "HUD 3560 Form",
        "HUD Model Lease",
        "Application / Housing Questionnaire", "Student Status Certification",
        "Owner Summary Sheet", "Family Summary Sheet",
    }
    _CERT_TYPES = {
        "HUD 50059", "Tenant Income Certification (TIC)", "HUD 3560 Form",
        "HUD Model Lease",
    }
    _INCOME_TYPES = {
        "Paystub", "Verification of Income (VOI)",
        "SSA Benefit Letter", "SSI Benefit Letter", "SSDI Benefit Letter",
        "Pension Statement", "TANF Verification", "TANF / Public Assistance Verification",
        "Child Support Statement", "Child Support / Alimony Affidavit",
        "Zero Income Certification", "Unemployment Affidavit",
        "Gift Income Verification", "Verification of Disability Benefits",
        "Work Number / Equifax Report",
        "Application / Housing Questionnaire",
        "Tenant Income Certification (TIC)", "HUD 50059", "HUD 3560 Form",
    }
    _ASSET_TYPES = {
        "Verification of Assets (VOA)", "Bank Statement",
        "Life Insurance Policy", "Asset Self-Certification",
        "Investment Account Statement", "Direct Express Card Verification",
        "No Asset Certification", "Disposal of Assets Certification",
        "Debit Card Asset Self-Certification",
        "Application / Housing Questionnaire",
        "Tenant Income Certification (TIC)", "HUD 50059", "HUD 3560 Form",
    }

    def _route(types: set[str]) -> list:
        return [g for g in llm_eligible_groups if g.document_type in types]

    demo_groups = _route(_DEMO_TYPES)
    cert_groups = _route(_CERT_TYPES)
    income_groups = _route(_INCOME_TYPES)
    asset_groups = _route(_ASSET_TYPES)

    def _est_tokens(groups: list) -> int:
        """Rough token estimate: ~4 chars per token for OCR HTML text."""
        return sum(len(g.combined_text) for g in groups) // 4

    logger.info(
        "  Group routing: demo=%d (~%dk tok), cert=%d (~%dk tok), "
        "income=%d (~%dk tok), asset=%d (~%dk tok)",
        len(demo_groups), _est_tokens(demo_groups) // 1000,
        len(cert_groups), _est_tokens(cert_groups) // 1000,
        len(income_groups), _est_tokens(income_groups) // 1000,
        len(asset_groups), _est_tokens(asset_groups) // 1000,
    )

    # --- LLM extraction: one call per category ---
    # Household demographics
    household = _llm_fallback(
        "Demographics", extract_demographics,
        demo_groups or llm_eligible_groups, settings,
        default=HouseholdDemographics(),
        certification_type=ctx.certification_type,
    )

    # Certification info
    certification_info = _llm_fallback(
        "Certification info", extract_certification_info,
        cert_groups or llm_eligible_groups, settings,
        default=CertificationInfo(),
        certification_type=ctx.certification_type,
    )

    # Supplement cert info from Notice of Rent Change if fields are missing
    if certification_info:
        _supplement_cert_info_from_rent_change(certification_info, document_groups)

    # Vision-verify "not signed": handwriting never survives OCR, so a
    # text-level isSigned=No is unreliable (wet-signed and blank forms
    # look identical in text). One targeted vision call settles it and
    # also detects draft watermarks ("not a final document").
    draft_watermark = False
    if certification_info and certification_info.isSigned == "No":
        verdict = _verify_cert_signature_vision(cert_groups, page_texts, settings)
        if verdict:
            if verdict["signed"]:
                logger.info(
                    "Vision found signatures on cert page %s — overriding "
                    "text-level isSigned=No", verdict["page"],
                )
                certification_info.isSigned = "Yes"
            draft_watermark = verdict["draft_watermark"]
            if draft_watermark:
                logger.info(
                    "Vision detected draft watermark on cert page %s",
                    verdict["page"],
                )

    # Income
    income = _llm_fallback(
        "Income", extract_income,
        income_groups or llm_eligible_groups, settings,
        default=IncomeExtraction(),
        certification_type=ctx.certification_type,
    )

    # Assets
    assets = _llm_fallback(
        "Assets", extract_assets,
        asset_groups or llm_eligible_groups, settings,
        default=AssetExtraction(),
        certification_type=ctx.certification_type,
    )

    # Step 3b1: Deduplicate asset records.
    # The LLM sometimes creates multiple records for the same account
    # because it sees the asset mentioned across several documents
    # (TIC Part IV, bank statement, questionnaire). Merge by accountNumber
    # (primary key) and drop pure stubs that carry no real data.
    if assets and assets.assetInformation:
        assets.assetInformation = _deduplicate_assets(assets.assetInformation)

    # Resolve certification type: API override > extracted > None.
    # The cert type is authoritatively provided by the caller (frontend upload
    # form / API param). Write the resolved value back onto certification_info
    # so downstream scoring/findings see the user-provided value and don't
    # falsely flag certificationType as missing.
    if ctx.certification_type:
        if certification_info:
            certification_info.certificationType = ctx.certification_type
    elif certification_info and certification_info.certificationType:
        ctx.certification_type = certification_info.certificationType

    # Step 3b2: Inherit memberName/sourceName on orphan paystubs from VI entries
    if income:
        vi_entries = income.sourceIncome.verificationIncome
        for ps in income.sourceIncome.payStub:
            if not ps.sourceName or not ps.memberName:
                # Try to match by sourceName or find the only Equifax employer
                for vi in vi_entries:
                    if vi.type_of_VOI == "Work Number" and vi.sourceName:
                        if not ps.sourceName:
                            ps.sourceName = vi.sourceName
                        if not ps.memberName:
                            ps.memberName = vi.memberName
                        break

    # Step 3b3: AR-SC fallback — seed income from TIC when third-party docs absent.
    # AR-SC (Alternate/Self-Certification) means the TIC is the source of truth
    # and third-party verification isn't required. When the LLM extracted no
    # real income records (only a $0 Self-Declaration stub, or nothing), copy
    # the cert form's declared householdIncome into a Self-Declaration record
    # so downstream validation, display, and scoring have the right amount.
    if (ctx.certification_type == "AR-SC"
            and income is not None
            and certification_info is not None
            and certification_info.householdIncome):
        vi_entries = income.sourceIncome.verificationIncome
        ps_entries = income.sourceIncome.payStub

        def _has_real_income() -> bool:
            if ps_entries:
                return True
            for vi in vi_entries:
                try:
                    amt = float(vi.selfDeclaredAmount or "0") if vi.selfDeclaredAmount else 0
                except ValueError:
                    amt = 0
                if amt > 0:
                    return True
                try:
                    rate = float(vi.rateOfPay or "0") if vi.rateOfPay else 0
                except ValueError:
                    rate = 0
                if rate > 0:
                    return True
            return False

        if not _has_real_income():
            try:
                tic_total = float(certification_info.householdIncome)
            except (ValueError, TypeError):
                tic_total = 0.0
            if tic_total > 0:
                # Pick the head of household's name as memberName when available
                head_name = None
                if household and household.houseHold:
                    head = next(
                        (m for m in household.houseHold if m.head == "H"),
                        household.houseHold[0],
                    )
                    head_name = (
                        f"{head.FirstName or ''} {head.LastName or ''}".strip()
                        or None
                    )
                # Replace stubs with a single authoritative Self-Declaration record
                income.sourceIncome.verificationIncome = [
                    VerificationIncomeEntry(
                        sourceName="Self-Declaration (TIC)",
                        memberName=head_name,
                        selfDeclaredAmount=f"{tic_total:.2f}",
                        selfDeclaredSource="AR-SC TIC",
                        incomeType="Self-Declared",
                        type_of_VOI="Self-Declaration",
                    )
                ]
                logger.info(
                    "AR-SC: seeded Self-Declaration income from TIC householdIncome $%.2f",
                    tic_total,
                )

    # Step 3c: Compute income calculations (Section 9)
    logger.info("Step 3c/6: Computing income calculations")
    income_calculations = []
    if income:
        vi_entries = income.sourceIncome.verificationIncome
        ps_entries = income.sourceIncome.payStub
        ps_map = match_paystubs_to_sources(ps_entries, vi_entries)

        # Effective date anchors the stale-wage guard: EIV / Work Number
        # wage-history quarters years before the cert must not be
        # annualized into current income.
        from app.services.income_calculator import _parse_date as _parse_ic_date
        reference_date = _parse_ic_date(
            certification_info.effectiveDate if certification_info else None
        )

        for i, vi in enumerate(vi_entries):
            results = calculate_all_methods(
                vi, ps_map.get(i, []), ctx.funding_program,
                reference_date=reference_date,
            )
            income_calculations.extend(results)

        # Handle paystubs not matched to any VI entry
        matched_ps = {id(ps) for psl in ps_map.values() for ps in psl}
        unmatched_ps = [ps for ps in ps_entries if id(ps) not in matched_ps]
        if unmatched_ps:
            by_source: dict[str, list] = {}
            for ps in unmatched_ps:
                key = (ps.sourceName or "Unknown").lower()
                by_source.setdefault(key, []).append(ps)
            for source_ps in by_source.values():
                results = calculate_all_methods(
                    None, source_ps, ctx.funding_program,
                    reference_date=reference_date,
                )
                income_calculations.extend(results)

    # Step 4: Build document inventories (deterministic — no LLM)
    logger.info("Step 4/6: Building document inventories (no LLM)")
    inventory_financial = build_financial_inventory(document_groups)
    inventory_hud = build_hud_inventory(document_groups)

    # Step 4b: Extract questionnaire disclosures
    # ALWAYS use LLM for YES/NO determination — keyword parser can't distinguish
    # "YES NO Employed" (question text) from actual YES answers.
    # Keyword parser is only used as fallback if LLM is unavailable.
    logger.info("Step 4b/6: Extracting questionnaire disclosures (LLM-first)")
    has_questionnaire = any(
        any(kw in g.document_type.lower() for kw in ("application", "questionnaire", "recertification"))
        for g in include_groups
    )
    questionnaire_disclosures = None
    if has_questionnaire:
        questionnaire_disclosures = _llm_fallback(
            "Questionnaire", extract_questionnaire_disclosures, llm_eligible_groups, settings,
            default=None,
        )
    if questionnaire_disclosures is None and has_questionnaire:
        # LLM failed — keyword parser as last resort (better than nothing)
        questionnaire_disclosures = parse_questionnaire(include_groups)

    # Step 4c: Link questionnaire disclosures to income entries
    if questionnaire_disclosures and income:
        _link_questionnaire_to_income(questionnaire_disclosures, income, document_groups)

    # Step 4d: Reconcile name variants across all records
    from app.services.name_reconciler import reconcile_names
    name_findings = reconcile_names(household, income, assets, document_groups)

    # Step 4e: Deduplicate household members (after name reconciliation)
    # Multiple extraction sources (cert form, questionnaire, application, VOI)
    # can produce duplicate member records for the same person.
    if household and household.houseHold:
        dedup_findings = _deduplicate_household_members(household)
        name_findings.extend(dedup_findings)

    # Step 5: Compile findings
    logger.info("Step 5/6: Compiling findings")
    findings = _generate_findings(
        classification,
        document_groups,
        household=household,
        certification_info=certification_info,
        income=income,
        assets=assets,
        inventory_financial=inventory_financial,
        inventory_hud=inventory_hud,
        income_calculations=income_calculations,
        questionnaire_disclosures=questionnaire_disclosures,
        ctx=ctx,
        previous_certification=previous_certification,
    )
    findings.extend(name_findings)
    if draft_watermark:
        findings.append(
            "Certification form is a watermarked DRAFT ('not a final "
            "document') — signed final version required; resubmission "
            "required per Section 11"
        )
    for calc in income_calculations:
        details = calc.details or ""
        if details.startswith("[historical]"):
            note = details[len("[historical] "):].split(";")[0]
            findings.append(
                f"Income source '{calc.sourceName}': {note} — excluded "
                f"from current-income comparison; verify employment "
                f"status (Section 9)"
            )

    # Step 5b: Populate compliance tracking on certification_info
    if certification_info:
        forms_present = list({
            g.document_type for g in document_groups
            if g.category != "ignore" and g.document_type != "Unknown"
        })
        # Only relocate the section-6 compliance-form findings this dedup
        # was built for. A substring match ("missing required"/"not found")
        # silently swallowed the "Missing required certification form"
        # headline into a field the findings text never renders — the one
        # finding that explains every derivative RED on a no-cert packet.
        missing_forms = [
            f for f in findings
            if f.startswith("Missing required compliance document")
        ]
        certification_info.formsPresent = sorted(forms_present)
        certification_info.missingForms = missing_forms
        # Compliance status must consider BOTH missingForms (moved out of
        # findings by the dedup step below) AND the remaining findings.
        has_issues = (
            bool(missing_forms)
            or any(
                "missing" in f.lower() or "not signed" in f.lower() or "resubmission" in f.lower()
                for f in findings
            )
        )
        certification_info.complianceStatus = "Incomplete" if has_issues else "Complete"

        # Remove the findings that were moved into missingForms — each fact
        # should appear exactly once in the output.
        missing_set = set(missing_forms)
        findings[:] = [f for f in findings if f not in missing_set]

    # Step 5c: Resolve duplicate self-declarations (most recent wins)
    if income:
        income.sourceIncome.verificationIncome = _resolve_duplicate_self_declarations(
            income.sourceIncome.verificationIncome
        )

    # Step 6: Multi-stage field-level scoring
    # Score from FINAL data objects after all merging/validation.
    logger.info("Step 6/6: Running field-level scoring pipeline")
    from app.services.field_scorer import score_pydantic_records, score_source_verification

    # Stage 1: Extraction presence (populated vs null)
    score_cards = score_pydantic_records(
        household=household,
        certification_info=certification_info,
        income=income,
        assets=assets,
    )

    # Stage 1b: Source verification (OCR quality + value-in-text check)
    score_source_verification(score_cards, document_groups, ocr_quality)

    # Stage 2: Cross-document consistency (compare same fields across records)
    score_cross_doc_consistency(score_cards)

    # Stage 3: Business rule validation (range, format, logic checks)
    score_business_rules(score_cards, certification_type=ctx.certification_type)

    # Build summary and surface red/yellow fields as findings.
    # Suppress field-level duplicates of facts the business rules already
    # reported in plain language. Each fact should appear exactly once in
    # the findings list, not once per source path.
    _BUSINESS_RULE_COVERED = {
        # Household null-field checks — aggregate business rule covers
        # all members in one finding, so per-field REDs are redundant.
        ("household_member", "disabled"),
        ("household_member", "student"),
        # Certification fields that already have plain-language business
        # rules from the cross-doc / signature / cert-type validators.
        ("certification", "householdIncome"),  # cross_doc_validator
        ("certification", "isSigned"),         # signature_validator
    }
    score_summary = build_score_summary(score_cards)
    for card in score_cards:
        for fs in card.flagged_fields:
            if (card.record_type, fs.field_name) in _BUSINESS_RULE_COVERED:
                continue
            findings.append(
                f"[{fs.flag.value.upper()}] {card.record_label or card.record_type}"
                f" → {fs.field_name}: {fs.flag_message}"
            )

    logger.info(
        "Scoring complete: %d fields — %d green, %d yellow, %d red (overall %.0f%%)",
        score_summary.total_fields, score_summary.green_fields,
        score_summary.yellow_fields, score_summary.red_fields,
        score_summary.overall_composite * 100,
    )

    elapsed = time.perf_counter() - start
    logger.info("Extraction pipeline complete in %.2fs", elapsed)

    # Persist per-page OCR provenance (flag, score, char count, text) so a
    # post-hoc review can tell OCR failures from LLM-extraction failures.
    # OCR is nondeterministic — re-running it later proves nothing about
    # what THIS run's extractors actually saw.
    page_ocr: list[PageOcrRecord] = []
    for pt in page_texts:
        score = pt.get("ocr_score")
        if isinstance(score, dict):
            score = score.get("composite")
        flag_names: list[str] = []
        for f in pt.get("ocr_flag_details") or []:
            if isinstance(f, str):
                flag_names.append(f)
            elif isinstance(f, dict):
                name = f.get("type") or f.get("flag") or f.get("name") or f.get("code")
                if name:
                    flag_names.append(str(name))
        text = pt.get("text") or ""
        page_ocr.append(PageOcrRecord(
            page=pt["page"],
            flag=pt.get("ocr_flag"),
            score=float(score) if isinstance(score, (int, float)) else None,
            chars=len(text.strip()),
            flags=flag_names,
            text=text or None,
        ))

    return ExtractionResult(
        classification=classification,
        document_groups=document_groups,
        household_demographics=household,
        certification_info=certification_info,
        previous_certification=previous_certification,
        income=income,
        assets=assets,
        document_inventory_financial=inventory_financial,
        document_inventory_hud=inventory_hud,
        income_calculations=income_calculations,
        questionnaire_disclosures=questionnaire_disclosures,
        findings=findings,
        field_scores=score_summary,
        page_ocr=page_ocr,
    )


def _link_questionnaire_to_income(
    disclosures,
    income: IncomeExtraction,
    document_groups: list,
) -> None:
    """Link questionnaire employer disclosures to income entries.

    If the questionnaire names employers, try to match them to existing VI records
    and populate selfDeclaredSource. Also create stub VI entries for disclosed
    employers that have no matching income record (e.g., Mobil from questionnaire).
    """
    from app.services.parsers.source_normalizer import normalize_source_name

    if not disclosures or not disclosures.employers:
        return

    vi_entries = income.sourceIncome.verificationIncome

    # Build lowercase sourceName lookup for existing VI entries
    existing_sources = {}
    for i, vi in enumerate(vi_entries):
        name = (vi.sourceName or "").lower().strip()
        if name:
            existing_sources[name] = i

    # Abbreviations/fragments that should never become stub employers.
    # "Ss" / "SS" is a common OCR fragment of "Social Security" on
    # questionnaires; "n/a", "none", etc. come from empty form fields.
    _REJECT_NAMES = {"ss", "s s", "n/a", "na", "none", "null", "tbd", "unknown", "-"}

    for employer in disclosures.employers:
        employer_norm = (normalize_source_name(employer) or employer).lower().strip()

        # Skip garbage employer names (addresses, form labels, fragments, etc.)
        if not employer_norm or len(employer_norm) < 3:
            continue
        if employer_norm in _REJECT_NAMES:
            continue
        if any(kw in employer_norm for kw in ("address", "phone", "date", "income per", "source of")):
            continue

        # Try to find a matching VI entry (fuzzy: check if employer is substring or vice versa)
        matched = False
        for source_key, idx in existing_sources.items():
            if (employer_norm in source_key or source_key in employer_norm
                    or _fuzzy_employer_match(employer_norm, source_key)):
                vi = vi_entries[idx]
                if not vi.selfDeclaredSource:
                    vi.selfDeclaredSource = _get_questionnaire_source(document_groups)
                matched = True
                break

        if not matched:
            # Disclosed employer with no matching income record — flag, don't guess
            logger.info("Questionnaire employer '%s' has no matching income record", employer)
            stub = VerificationIncomeEntry(
                sourceName=normalize_source_name(employer) or employer,
                selfDeclaredSource=_get_questionnaire_source(document_groups),
                incomeType="Non-Federal Wage",
                employmentStatus="Flagged — disclosed on questionnaire but no VOI/paystub found",
            )
            vi_entries.append(stub)


_SIGNATURE_VISION_PROMPT = """\
You are inspecting a housing certification form page for signatures.

Look at the Signature / Tenant Signatures / Owner-Agent Signature areas.
- A signature counts if there is ANY handwritten signature (cursive ink
  marks), signed name, or electronic-signature stamp on or near a
  signature line. A typed label "Signature" next to an EMPTY line does
  NOT count.
- Also check whether the page carries a diagonal draft watermark such as
  "This is Not a Final Document" / "watermark will be removed upon
  completion".

Return STRICT JSON only:
{"signed": true/false, "draft_watermark": true/false, "evidence": "<one short sentence>"}"""


def _verify_cert_signature_vision(cert_groups, page_texts, settings) -> dict | None:
    """Vision-verify a text-level isSigned=No on the cert form.

    OCR cannot see handwriting: a wet-signed TIC and a blank one both OCR
    to empty signature cells, so text-based isSigned=No is a coin flip
    (measured: fires on 82% of cases with zero correlation to human
    reviewer rejections). Only the page image can tell. Checks the cert
    group's signature pages (pages whose text mentions 'signature',
    falling back to the group's last page).

    Returns {"signed": bool, "draft_watermark": bool, "page": int} or
    None when no page could be checked (missing images, vision failure)
    — callers keep the text-based verdict in that case.
    """
    from app.services.llm_service import call_llm_vision_json

    group = next(
        (g for g in cert_groups
         if "(previous)" not in g.document_type.lower()
         and any(m in g.document_type.lower()
                 for m in ("50059", "tenant income certification", "(tic)", "3560"))),
        None,
    )
    if not group:
        return None
    paths = {pt["page"]: pt.get("image_path") for pt in page_texts}
    texts = {pt["page"]: (pt.get("text") or "").lower() for pt in page_texts}
    sig_pages = [p for p in group.pages if "signature" in texts.get(p, "")]
    if not sig_pages:
        sig_pages = [group.pages[-1]]
    signed = watermark = False
    checked = 0
    best_page = None
    for pn in sig_pages[:2]:
        img = paths.get(pn)
        if not img:
            continue
        try:
            verdict = call_llm_vision_json(
                _SIGNATURE_VISION_PROMPT,
                f"Inspect this certification form page (packet page {pn}).",
                [str(img)], settings,
            )
        except Exception:
            logger.exception("Signature vision check failed for page %d", pn)
            continue
        checked += 1
        if verdict.get("signed") and not signed:
            signed = True
            best_page = pn
        if verdict.get("draft_watermark"):
            watermark = True
            best_page = best_page or pn
    if not checked:
        return None
    return {"signed": signed, "draft_watermark": watermark,
            "page": best_page or sig_pages[0]}


def _supplement_cert_info_from_rent_change(
    ci: CertificationInfo,
    document_groups: list,
) -> None:
    """Resolve rent fields by effective date — most recent date wins.

    Collects rent values from all sources (HUD 50059, Lease Amendment, etc.)
    with their effective dates, then picks the most recent for each field.
    """
    import re
    from app.services.validation import normalize_date, normalize_money

    # Collect (effective_date, field_values) from each rent-bearing document
    rent_sources: list[tuple[str | None, dict]] = []

    for g in document_groups:
        if g.category == "ignore":
            continue

        text = g.combined_text
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\\+[()]", "", clean)

        # Extract effective date from this document
        eff_date = None
        for pattern in [
            r"[Ee]ffective\s*(?:[Dd]ate)?[:\s]*(\d{1,2}/\d{1,2}/\d{2,4})",
            r"effective with the rent due for the month of\s*(\d{1,2}/\d{1,2}/\d{4})",
            r"[Ee]ffective\s*[Dd]ate[:\s]*(\d{4}[/-]\d{1,2}[/-]\d{1,2})",
        ]:
            m = re.search(pattern, clean)
            if m:
                eff_date = normalize_date(m.group(1))
                break

        # Extract rent fields
        fields: dict = {}
        rent_patterns = [
            ("tenantRent", r"Tenant (?:Paid )?Rent\s*\(?\$?\s*([\d,]+\.?\d*)"),
            ("utilityAllowance", r"Utility Allowance\s*\(?\$?\s*([\d,]+\.?\d*)"),
            ("grossRent", r"Gross Rent\s*\(?\$?\s*([\d,]+\.?\d*)"),
            ("_totalTenantPayment", r"Total Tenant Payment\s*\(?\$?\s*([\d,]+\.?\d*)"),
            ("_assistancePayment", r"Assistance Payment\s*\(?\$?\s*([\d,]+\.?\d*)"),
        ]
        for field_name, pattern in rent_patterns:
            m = re.search(pattern, clean, re.IGNORECASE)
            if m:
                val = normalize_money(m.group(1))
                if val:
                    fields[field_name] = val

        if fields and eff_date:
            rent_sources.append((eff_date, fields))

    if not rent_sources:
        return

    # Sort by effective date descending — most recent first
    rent_sources.sort(key=lambda x: x[0] or "", reverse=True)

    # Apply: most recent date wins for each field
    best_date, best_fields = rent_sources[0]
    for field_name, value in best_fields.items():
        current = getattr(ci, field_name, None) if not field_name.startswith("_") else None
        if field_name.startswith("_"):
            # Internal fields — just set
            setattr(ci, field_name, value)
        elif value != current:
            logger.info(
                "Rent field %s: %s → %s (from doc with effective date %s)",
                field_name, current, value, best_date,
            )
            setattr(ci, field_name, value)

    # Fill householdIncome only if null
    if not ci.householdIncome:
        for _, fields in rent_sources:
            if "householdIncome" in fields:
                ci.householdIncome = fields["householdIncome"]
                break


def _fuzzy_employer_match(a: str, b: str) -> bool:
    """Check if two employer names are likely the same (basic fuzzy match)."""
    # Remove common suffixes
    for suffix in ("inc", "llc", "corp", "ltd", "co", "company"):
        a = a.replace(suffix, "").strip()
        b = b.replace(suffix, "").strip()
    # Check significant overlap
    a_words = set(a.split())
    b_words = set(b.split())
    if not a_words or not b_words:
        return False
    overlap = a_words & b_words
    return len(overlap) >= 1 and len(overlap) / min(len(a_words), len(b_words)) >= 0.5


def _get_questionnaire_source(document_groups: list) -> str:
    """Determine the selfDeclaredSource based on questionnaire document type."""
    for g in document_groups:
        dt = g.document_type.lower()
        if "application" in dt:
            return "Application"
        if "questionnaire" in dt or "recertification report" in dt:
            return "Questionnaire"
    return "Questionnaire"


def _deduplicate_assets(asset_records: list) -> list:
    """Merge duplicate asset records and drop pure stubs.

    The LLM sometimes creates multiple records for the same account because
    it sees the asset mentioned across several documents (TIC Part IV row,
    bank statement, questionnaire Part B disclosure, VOA form). This pass:

    1. Drops pure stubs: records where accountNumber, currentBalance,
       averageSixMonthBalance, and selfDeclaredAmount are ALL null.
       These carry no real information and only add noise to scoring.

    2. Merges records with matching accountNumber. Two mentions of the
       same account across different documents should become one record
       with the union of populated fields. The richer record wins on
       conflicts; the other's non-null fields fill gaps.

    3. Falls back to (sourceName, accountType) matching when accountNumber
       is missing on both — but ONLY when both records lack an account
       number. Never merges records with different account numbers even
       if the bank and type match (two real separate accounts).
    """
    if not asset_records:
        return []

    # Pass 1: drop stubs that carry no real data
    def _is_stub(rec) -> bool:
        return (
            not rec.accountNumber
            and not rec.currentBalance
            and not rec.averageSixMonthBalance
            and not rec.selfDeclaredAmount
        )

    live = [r for r in asset_records if not _is_stub(r)]
    if len(live) < len(asset_records):
        logger.info(
            "Asset dedup: dropped %d stub record(s) with no real data",
            len(asset_records) - len(live),
        )

    # Pass 2: group by accountNumber (primary key); fall back to
    # (sourceName|accountType) only when accountNumber is missing.
    def _key(rec) -> str:
        if rec.accountNumber:
            return f"acct:{rec.accountNumber.strip()}"
        return (
            f"sa:{(rec.sourceName or '').lower().strip()}|"
            f"{(rec.accountType or '').lower().strip()}"
        )

    groups: dict[str, list] = {}
    for rec in live:
        groups.setdefault(_key(rec), []).append(rec)

    def _populated_count(rec) -> int:
        """How many non-null scalar fields this record has — used to pick
        the richest record when merging."""
        n = 0
        for f in (
            "accountNumber", "currentBalance", "averageSixMonthBalance",
            "selfDeclaredAmount", "incomeAmount", "interestType",
            "percentageOfOwnership", "dateReceived",
        ):
            if getattr(rec, f, None):
                n += 1
        return n

    merged: list = []
    for key, recs in groups.items():
        if len(recs) == 1:
            merged.append(recs[0])
            continue
        # Pick the richest record as the base, fill gaps from the others
        recs_sorted = sorted(recs, key=_populated_count, reverse=True)
        base = recs_sorted[0]
        for other in recs_sorted[1:]:
            for f in (
                "accountNumber", "currentBalance", "averageSixMonthBalance",
                "selfDeclaredAmount", "incomeAmount", "interestType",
                "percentageOfOwnership", "dateReceived", "sourceName",
                "assetOwner", "socialSecurityNumber", "accountType",
                "selfDeclaredSource", "address",
            ):
                if not getattr(base, f, None) and getattr(other, f, None):
                    setattr(base, f, getattr(other, f))
            # Merge bank statements lists if both have them
            if getattr(other, "bankStatment", None):
                base_stmts = getattr(base, "bankStatment", None) or []
                other_stmts = other.bankStatment
                seen_dates = {s.statementDate for s in base_stmts if getattr(s, "statementDate", None)}
                for s in other_stmts:
                    if getattr(s, "statementDate", None) not in seen_dates:
                        base_stmts.append(s)
                base.bankStatment = base_stmts
        merged.append(base)
        logger.info(
            "Asset dedup: merged %d records into 1 for key=%s",
            len(recs), key,
        )

    return merged


def _resolve_duplicate_self_declarations(vi_entries: list) -> list:
    """If multiple sources declare the same income, keep the most recent."""
    from collections import defaultdict
    by_source: dict[str, list] = defaultdict(list)
    non_dupes: list = []

    for vi in vi_entries:
        source = (vi.sourceName or "").lower().strip()
        if source and vi.selfDeclaredAmount:
            by_source[source].append(vi)
        else:
            non_dupes.append(vi)

    for source, entries in by_source.items():
        if len(entries) <= 1:
            non_dupes.extend(entries)
        else:
            # Sort by dateReceived or hireDate descending, keep most recent
            entries.sort(
                key=lambda v: v.dateReceived or v.hireDate or "",
                reverse=True,
            )
            non_dupes.append(entries[0])  # Keep most recent

    return non_dupes


def _llm_fallback(label, func, groups, settings, *, default=None, **kwargs):
    """Call an LLM extraction function with graceful degradation on rate limit."""
    import anthropic
    logger.info("  %s: LLM fallback", label)
    try:
        return func(groups, settings, **kwargs)
    except anthropic.RateLimitError:
        logger.warning("  %s: LLM rate limited — returning empty result", label)
        return default
    except Exception:
        logger.exception("  %s: LLM fallback failed", label)
        return default


def _deduplicate_household_members(household) -> list[str]:
    """Merge duplicate household members from multiple extraction sources.

    After name reconciliation, members with the same first+last name are duplicates.
    Merge by keeping the record with the most populated fields, filling gaps from
    the other copy. Works for any file type — not doc-specific.

    Returns findings about merged members.
    """
    findings: list[str] = []
    members = household.houseHold
    if len(members) < 2:
        return findings

    # Group by normalized name key (first + last, lowered)
    groups: dict[str, list[int]] = {}
    for i, m in enumerate(members):
        first = (m.FirstName or "").lower().strip()
        last = (m.LastName or "").lower().strip()
        key = f"{first} {last}".strip()
        if key:
            groups.setdefault(key, []).append(i)

    # Merge duplicates
    to_remove: set[int] = set()
    for key, indices in groups.items():
        if len(indices) < 2:
            continue

        # Score each copy: count non-null fields
        _MERGE_FIELDS = (
            "householdMemberNumber", "FirstName", "MiddleName", "LastName",
            "socialSecurityNumber", "DOB", "head", "disabled", "student",
        )

        def _field_count(m) -> int:
            return sum(1 for f in _MERGE_FIELDS if getattr(m, f, None) is not None)

        # Sort by field count descending — richest record first
        scored = sorted(indices, key=lambda i: _field_count(members[i]), reverse=True)
        primary_idx = scored[0]
        primary = members[primary_idx]

        for dup_idx in scored[1:]:
            dup = members[dup_idx]
            # Fill gaps in primary from duplicate
            for field in _MERGE_FIELDS:
                if getattr(primary, field, None) is None and getattr(dup, field, None) is not None:
                    setattr(primary, field, getattr(dup, field))
            to_remove.add(dup_idx)
            dup_name = f"{dup.FirstName or ''} {dup.LastName or ''}".strip()
            findings.append(
                f"Merged duplicate household member '{dup_name}' "
                f"(member #{dup.householdMemberNumber or '?'} into #{primary.householdMemberNumber or '?'})"
            )

    if to_remove:
        household.houseHold = [m for i, m in enumerate(members) if i not in to_remove]
        # Renumber members sequentially
        for i, m in enumerate(household.houseHold):
            m.householdMemberNumber = f"{i + 1:02d}"

    return findings



_CURRENT_CERT_FORM_TYPES = (
    "HUD 50059", "Tenant Income Certification (TIC)", "HUD 3560 Form",
)


def _source_file_of(page: int, source_files: list[dict]) -> str | None:
    """Title of the merged-source file a 1-indexed page belongs to."""
    title = None
    for sf in sorted(source_files, key=lambda s: s["start_page"]):
        if page >= sf["start_page"]:
            title = sf["title"]
    return title


def _demote_superseded_cert_groups(
    document_groups: list, source_files: list[dict],
) -> None:
    """Demote duplicate current cert forms from stale resubmissions.

    Source files are merged newest-first, so for each cert form type the
    group starting earliest in the merged page order is the current one.
    A same-type group is demoted only when it starts in a DIFFERENT
    source file — a duplicate within one file is a classifier split of
    one physical form (e.g. its signature page), not a resubmission, and
    is left alone. "(Previous)" groups carry a different document_type
    and are never touched.
    """
    kept: dict[str, tuple[int, str | None]] = {}
    duplicates = sorted(
        (g for g in document_groups
         if g.document_type in _CURRENT_CERT_FORM_TYPES
         and g.category != "ignore" and g.pages),
        key=lambda g: min(g.pages),
    )
    for g in duplicates:
        src = _source_file_of(min(g.pages), source_files)
        if g.document_type not in kept:
            kept[g.document_type] = (min(g.pages), src)
            continue
        kept_page, kept_src = kept[g.document_type]
        if src != kept_src:
            logger.info(
                "Demoting superseded %s at pages %s (file %r) — current "
                "copy starts at page %d in newer file %r",
                g.document_type, g.pages, src, kept_page, kept_src,
            )
            g.document_type += " (Superseded)"
            g.category = "ignore"


def _extract_previous_cert(document_groups: list) -> PreviousCertification | None:
    """Extract key fields from previous certification groups for IR delta comparison.

    Uses simple regex on the previous cert's combined_text to pull summary-level data.
    Works across form types (HUD 50059, TIC, RD 3560-8) because it targets common
    patterns: effective date, total income, rent, and employment summary tables.
    """
    import re
    from app.services.validation import normalize_date, normalize_money

    prev_groups = [g for g in document_groups if "(Previous)" in g.document_type]
    if not prev_groups:
        return None

    g = prev_groups[0]
    clean = re.sub(r"<[^>]+>", " ", g.combined_text)
    clean = re.sub(r"\\+[()]", "", clean)

    prev = PreviousCertification(source_pages=g.pages)

    # Effective date
    for pat in [
        r"[Ee]ffective\s*(?:[Dd]ate)?[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
        r"[Cc]ertification\s*[Dd]ate[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
    ]:
        m = re.search(pat, clean)
        if m:
            prev.effectiveDate = normalize_date(m.group(1))
            break

    # Total income — works for HUD field 86, TIC Total Income (E), RD Annual Income
    for pat in [
        r"Total\s*(?:Annual\s*)?Income[:\s]*\$?\s*([\d,]+\.?\d*)",
        r"Annual\s*Income[:\s]*\$?\s*([\d,]+\.?\d*)",
        r"f\.\s*Annual\s*Income[:\s]*\$?\s*([\d,]+\.?\d*)",
    ]:
        m = re.search(pat, clean, re.IGNORECASE)
        if m:
            prev.householdIncome = normalize_money(m.group(1))
            break

    # Tenant rent
    m = re.search(r"Tenant\s*Rent[:\s]*\$?\s*([\d,]+\.?\d*)", clean, re.IGNORECASE)
    if m:
        prev.tenantRent = normalize_money(m.group(1))

    # Gross rent
    m = re.search(r"Gross\s*Rent[:\s]*\$?\s*([\d,]+\.?\d*)", clean, re.IGNORECASE)
    if m:
        prev.grossRent = normalize_money(m.group(1))

    # Per-source income from employment summary table
    # Pattern: "Name | Employer | Annual salary" (common in RD 3560-8, TIC page 3)
    # Word counts are bounded and the name/employer groups cannot absorb
    # whitespace runs: lazy whitespace-inclusive groups here backtrack in
    # O(n^3) on OCR text with no decimal amounts, holding the GIL for hours.
    income_sources: list[PreviousCertIncomeSource] = []
    for m in re.finditer(
        r"([A-Z][a-z][\w.'-]*(?:[ \t][\w.'-]+){0,4}?)[ \t]+"
        r"([A-Z][\w&.'-]*(?:[ \t][\w&.'-]+){0,5}?)[ \t]+"
        r"\$?([\d,]+\.\d{2})(?=\s|$)",
        clean,
    ):
        name = m.group(1).strip()
        employer = m.group(2).strip()
        amount = normalize_money(m.group(3))
        if name.lower() in ("total", "totals") or not amount:
            continue
        try:
            if float(amount) > 0:
                income_sources.append(PreviousCertIncomeSource(
                    sourceName=employer,
                    memberName=name,
                    annualAmount=amount,
                ))
        except ValueError:
            continue

    prev.income_by_source = income_sources

    if prev.effectiveDate or prev.householdIncome or prev.tenantRent:
        logger.info(
            "Previous cert: pages %s, date=%s, income=%s, rent=%s, sources=%d",
            g.pages, prev.effectiveDate, prev.householdIncome, prev.tenantRent,
            len(income_sources),
        )
        return prev

    return None


def _generate_findings(
    classification,
    document_groups,
    household=None,
    certification_info=None,
    income=None,
    assets=None,
    inventory_financial=None,
    inventory_hud=None,
    income_calculations=None,
    questionnaire_disclosures=None,
    ctx: PipelineContext | None = None,
    previous_certification: PreviousCertification | None = None,
) -> list[str]:
    """Generate compliance findings based on classification and extraction results."""
    findings = []

    # --- 1. Low-confidence classifications ---
    for pc in classification.pages:
        if pc.confidence < 0.6:
            findings.append(
                f"Page {pc.page}: Low confidence classification "
                f"({pc.confidence:.0%}) as '{pc.document_type}' — manual review recommended"
            )

    # --- 2. Calculation worksheets excluded ---
    calc_groups = [
        g for g in document_groups
        if "calculation" in g.document_type.lower() or "calc" in g.document_type.lower()
    ]
    for g in calc_groups:
        findings.append(
            f"Pages {g.page_range}: '{g.document_type}' detected — "
            f"excluded from data extraction per Document Exclusion rules"
        )

    # --- 3. Unknown document types ---
    unknown_groups = [g for g in document_groups if g.document_type == "Unknown"]
    for g in unknown_groups:
        findings.append(
            f"Pages {g.page_range}: Unrecognized document type — manual review required"
        )

    # --- 4. Previous certifications detected ---
    prev_groups = [
        g for g in document_groups
        if "(Previous)" in g.document_type
    ]
    for g in prev_groups:
        findings.append(
            f"Pages {g.page_range}: '{g.document_type}' detected — "
            f"excluded from data extraction per Section 14 (Past Certifications)"
        )

    # --- 4b. IR delta comparison (previous vs current cert) ---
    if previous_certification and certification_info and certification_info.certificationType == "IR":
        # Total income delta
        try:
            curr_income = float((certification_info.householdIncome or "0").replace(",", ""))
            prev_income = float((previous_certification.householdIncome or "0").replace(",", ""))
            if curr_income > 0 and prev_income > 0 and curr_income != prev_income:
                delta = curr_income - prev_income
                direction = "increase" if delta > 0 else "decrease"
                findings.append(
                    f"IR income delta: ${prev_income:,.2f} → ${curr_income:,.2f} "
                    f"(${abs(delta):,.2f} {direction})"
                )
        except ValueError:
            pass
        # Per-source deltas
        if previous_certification.income_by_source:
            source_details = []
            for s in previous_certification.income_by_source:
                if not s.annualAmount:
                    continue
                label = s.sourceName or s.incomeType or s.memberName or "Unknown"
                try:
                    source_details.append(f"{label}: ${float(s.annualAmount):,.2f}")
                except ValueError:
                    pass
            if source_details:
                findings.append(
                    f"Previous cert income breakdown: {', '.join(source_details)}"
                )
        # Rent delta
        try:
            curr_rent = float((certification_info.tenantRent or "0").replace(",", ""))
            prev_rent = float((previous_certification.tenantRent or "0").replace(",", ""))
            if curr_rent > 0 and prev_rent > 0 and curr_rent != prev_rent:
                delta = curr_rent - prev_rent
                direction = "increase" if delta > 0 else "decrease"
                findings.append(
                    f"IR rent delta: ${prev_rent:,.2f} → ${curr_rent:,.2f} "
                    f"(${abs(delta):,.2f} {direction})"
                )
        except ValueError:
            pass

    # --- 5. Blank forms detected ---
    blank_groups = [
        g for g in document_groups
        if g.document_type == "Blank Form"
    ]
    for g in blank_groups:
        notes = g.notes or ""
        if "pending employer response" in (notes or "").lower() or "voe sent" in (notes or "").lower():
            findings.append(
                f"Pages {g.page_range}: VOE sent to employer but not returned — "
                f"pending employer response. Flag for follow-up. {notes}"
            )
        else:
            findings.append(
                f"Pages {g.page_range}: Blank verification form detected — "
                f"excluded per Section 14 (Blank Verification Forms)"
            )

    # --- 6. Missing required HUD compliance forms ---
    doc_types_found = {g.document_type for g in document_groups}
    required_hud_forms = {
        "HUD 9887": "HUD 9887 (Notice and Consent) — required for HUD properties, signed by all adults",
        "HUD 9887-A": "HUD 9887-A (Applicant's Consent) — required per adult member for HUD properties",
        "Acknowledgement of Receipt of HUD Forms": "Acknowledgement of Receipt of HUD Forms — signed by all adults",
    }
    is_hud_property = any(
        "HUD 50059" in g.document_type for g in document_groups
        if "(Previous)" not in g.document_type
    )
    if is_hud_property:
        for form, description in required_hud_forms.items():
            if form not in doc_types_found:
                findings.append(
                    f"Missing required compliance document: {description}"
                )

    # --- 6b. Certification form presence + unreadable pages ---
    # A packet with no current cert form at all is the single most
    # important thing to tell the analyst — every downstream field
    # finding (rent not extracted, date mismatches vs MuleSoft, unsigned
    # cert) is derivative noise without this context.
    cert_markers = ("50059", "tenant income certification", "(tic)", "3560")
    cert_form_present = any(
        any(m in g.document_type.lower() for m in cert_markers)
        for g in document_groups
        if g.category != "ignore" and "(Previous)" not in g.document_type
    )
    ocr_failed_pages = sorted(
        p for g in document_groups if g.document_type == "OCR Failed"
        for p in g.pages
    )
    if not cert_form_present:
        hidden_hint = (
            f" — it may be among the {len(ocr_failed_pages)} page(s) that "
            f"failed OCR" if ocr_failed_pages else ""
        )
        findings.append(
            "Missing required certification form: no current TIC / HUD 50059 "
            f"/ RD 3560 found in packet{hidden_hint}. Extraction is based on "
            "secondary documents only (EIV, correspondence, etc.)"
        )
    if ocr_failed_pages:
        findings.append(
            f"{len(ocr_failed_pages)} page(s) could not be read by OCR "
            f"(pages {', '.join(str(p) for p in ocr_failed_pages)}) — "
            f"content unavailable to the audit; manual review recommended"
        )

    # --- 7. Unsigned certification form ---
    # Only meaningful when a cert form actually exists — with no form in
    # the packet, "not signed" misstates the real problem (form missing).
    if cert_form_present and certification_info and certification_info.isSigned == "No":
        findings.append(
            "Certification form (TIC/HUD 50059) is NOT signed — "
            "resubmission required per Section 11"
        )

    # --- 8. Certification type not identified ---
    if certification_info and not certification_info.certificationType:
        findings.append(
            "Certification type could not be determined from TIC/HUD 50059 — "
            "manual review required"
        )

    # --- 9. DOB discrepancies ---
    if household and household.houseHold:
        dob_by_name: dict[str, str] = {}
        for member in household.houseHold:
            name_key = f"{(member.FirstName or '').lower()} {(member.LastName or '').lower()}".strip()
            if not name_key or not member.DOB:
                continue
            if name_key in dob_by_name and dob_by_name[name_key] != member.DOB:
                findings.append(
                    f"DOB discrepancy for '{member.FirstName} {member.LastName}': "
                    f"{dob_by_name[name_key]} vs {member.DOB} — manual verification required"
                )
            else:
                dob_by_name[name_key] = member.DOB

    # --- 10. Disabled/student fields null when cert doc exists ---
    if household and household.houseHold:
        cert_doc_exists = any(
            g.document_type in ("HUD 50059", "Tenant Income Certification (TIC)")
            for g in document_groups if g.category == "include"
        )
        if cert_doc_exists:
            null_disabled = all(m.disabled is None for m in household.houseHold)
            null_student = all(m.student is None for m in household.houseHold)
            if null_disabled:
                findings.append(
                    "Disability status is null for all household members — "
                    "verify against HUD 50059 Section 4 or TIC household composition"
                )
            if null_student:
                findings.append(
                    "Student status is null for all household members — "
                    "verify against HUD 50059 Section 4 or TIC household composition"
                )

    # --- 11. Missing self-declared amounts ---
    if income:
        vi_records = income.sourceIncome.verificationIncome
        has_self_declared = any(v.selfDeclaredAmount for v in vi_records)
        questionnaire_exists = any(
            g.document_type == "Application / Housing Questionnaire"
            for g in document_groups if g.category == "include"
        )
        if questionnaire_exists and not has_self_declared:
            findings.append(
                "Self-declared income amounts not extracted from questionnaire/application — "
                "review for income declarations per Section 9"
            )

    # --- 12. Zero income worksheet needed for head of household ---
    if household and income and household.houseHold:
        # Reuse the name reconciler's own same-person notion (its similarity
        # function AND its calibrated threshold) rather than re-deciding here.
        # Household members are intentionally NOT renamed to canonical form, so
        # the head can read 'Alexandra Depina' while income reads 'Alexandra R.
        # DePina'; an exact-string check produced a false "head has no income".
        from app.services.name_reconciler import _CLUSTER_THRESHOLD, _name_similarity

        income_member_names: list[str] = [
            ps.memberName for ps in income.sourceIncome.payStub if ps.memberName
        ] + [
            vi.memberName for vi in income.sourceIncome.verificationIncome if vi.memberName
        ]

        for member in household.houseHold:
            if member.head == "H":
                name = f"{(member.FirstName or '')} {(member.LastName or '')}".strip()
                if not name:
                    continue
                has_income = any(
                    _name_similarity(name, other) >= _CLUSTER_THRESHOLD
                    for other in income_member_names
                )
                if not has_income:
                    findings.append(
                        f"Head of household '{member.FirstName} {member.LastName}' has no income records — "
                        f"zero income worksheet required per Section 9"
                    )

    # --- 13. Terminated employment without termination date ---
    if income:
        for vi in income.sourceIncome.verificationIncome:
            if vi.employmentStatus == "Terminated" and not vi.terminationDate:
                findings.append(
                    f"Employment at '{vi.sourceName}' is terminated but no termination date captured — "
                    f"verify termination date for IR processing"
                )

    # --- 14. Funding program not specified ---
    if ctx and not ctx.funding_program:
        findings.append(
            "Funding program not specified — hours range rules not applied. "
            "Provide funding_program parameter for program-specific hours resolution (Section 10)"
        )

    # --- 15. Signature & compliance validation (Section 11) ---
    findings.extend(validate_signatures(
        inventory_hud, inventory_financial, household,
        certification_info, document_groups, ctx or PipelineContext(),
    ))

    # --- 16. Certification type-specific requirements (Section 12) ---
    cert_type = ctx.certification_type if ctx else None
    findings.extend(validate_cert_type_requirements(
        cert_type, document_groups, inventory_hud, household,
    ))

    # --- 17. Affirmative response cross-reference (Section 11) ---
    findings.extend(validate_affirmative_responses(
        questionnaire_disclosures, document_groups,
    ))

    # --- 18. Cross-document validation (Sections 7, 8) ---
    findings.extend(validate_income_consistency(income, income_calculations or []))
    findings.extend(validate_duplicate_income(income, certification_info))
    findings.extend(validate_tic_totals(certification_info, income, income_calculations or []))
    findings.extend(validate_asset_consistency(assets))
    findings.extend(validate_household_consistency(household, income, assets))
    findings.extend(validate_asset_worksheet_rules(assets, document_groups))
    findings.extend(validate_rent_assistance(certification_info, document_groups))
    findings.extend(validate_cert_summary_vs_income(income, income_calculations or [], document_groups))

    # --- 19. Known IDP bug detection (Section 17) ---
    findings.extend(detect_known_bugs(
        classification, document_groups, income,
        household=household, certification_info=certification_info,
    ))

    # --- 20. Special scenarios (Section 19) ---
    findings.extend(check_special_scenarios(
        household, income, certification_info,
        document_groups, inventory_hud, ctx,
        assets=assets,
    ))

    return findings
