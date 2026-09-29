"""Full IDP pipeline: OCR → Classify → Group → Extract → Validate → Output."""

import logging
import re
import time

from app.core.config import Settings
from app.schemas.context import PipelineContext
from app.schemas.scoring import GREEN_THRESHOLD
from app.schemas.extraction import (
    AssetExtraction,
    CertificationInfo,
    ExtractionResult,
    HouseholdDemographics,
    HouseholdMember,
    IncomeExtraction,
    PageOcrRecord,
    PreviousCertification,
    PreviousCertIncomeSource,
    VerificationIncomeEntry,
)
from app.services.bug_detector import detect_known_bugs
from app.services.completeness import check_completeness
from app.services.doc_taxonomy import (
    ROUTE_ASSET, ROUTE_CERT, ROUTE_DEMO, ROUTE_INCOME, assert_known,
    canonical_label, is_current_certification_form, is_previous_certification,
    labels_for_route, category_of,
)
from app.services.findings import (
    ASSIGN_CLIENT, ASSIGN_INTERNAL, CATEGORY_FILE_REVIEW, CATEGORY_INCOME,
    CATEGORY_MEMBER, CATEGORY_UNIT_RENT, RESOLVE_PRESENCE, make_finding, slug,
)
from app.services.identity import resolve_identities
from app.services.findings import dedupe as dedupe_findings
from app.services.findings import records as finding_records
from app.services.findings import text_of
from app.services.findings import render as render_findings
from app.services.cert_type_rules import validate_cert_type_requirements
from app.services.cross_doc_validator import (
    validate_asset_consistency,
    validate_asset_worksheet_rules,
    validate_cert_summary_vs_income,
    validate_confirmation_reports,
    validate_duplicate_income,
    validate_household_consistency,
    validate_income_consistency,
    validate_rent_assistance,
    validate_tic_totals,
)
from app.services.extractor import (
    CRITICAL_CERT_FIELDS,
    source_names_overlap,
    build_group_texts,
    extract_assets,
    extract_certification_info,
    extract_demographics,
    extract_income,
    merge_member_fields,
    required_member_gaps,
    retry_cert_info_fields,
    retry_member_fields,
)
from app.services.income_calculator import PAYSTUB_GUIDANCE_COUNT, calculate_all_methods, match_paystubs_to_sources
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
    score_findings,
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
        codes = set()
        for f in flag_details if isinstance(flag_details, list) else []:
            codes.add(f if isinstance(f, str) else (f.get("code") if isinstance(f, dict) else None))
        repaired = bool(codes & {"vision_fallback", "text_layer"})
        if "blank_page" in codes:
            skip_pages.add(pt["page"])
        elif "low_quality_scan" in codes and not repaired:
            skip_pages.add(pt["page"])
        elif not pt.get("text", "").strip():
            skip_pages.add(pt["page"])

    # Steps 1+2: Two-pass classify and group
    # Pass 1: keyword classify + summarize (instant, no LLM)
    # Pass 2: LLM correct + group (one call, sees full file context)
    logger.info("Steps 1-2/6: Two-pass classification + grouping")
    classification, document_groups = classify_and_group(page_texts, settings)
    # A recertification packet assembled before the new certification is
    # executed holds only last year's form. Read as "previous" it reached
    # no extractor, and the case went out with no household at all. The
    # most recent certification in the file is then the form of record,
    # and a finding says the current one is not in the packet.
    ctx.certification_is_prior = _promote_prior_certification(document_groups, classification)

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

    # Routing sets come from the taxonomy: a label feeds the extractors its
    # entry names, so a new type routes the day it is added and no literal
    # list here can drift from the classifier's (four of them had).
    _DEMO_TYPES = labels_for_route(ROUTE_DEMO)
    _CERT_TYPES = labels_for_route(ROUTE_CERT)
    _INCOME_TYPES = labels_for_route(ROUTE_INCOME)
    _ASSET_TYPES = labels_for_route(ROUTE_ASSET)

    def _route(types) -> list:
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

    # Second-stage retry, from the page image, for the cells a certification
    # form must carry. Runs after the rent-change supplement so it fires only
    # on gaps nothing text-based could fill. Failure keeps first-pass values.
    required_field_findings: list = []
    try:
        required_field_findings = _recover_required_fields_from_images(
            household, certification_info, cert_groups, page_texts, ocr_quality,
            settings, ctx.certification_type,
        )
    except Exception:
        logger.exception("Required-field recovery failed — keeping first-pass values")

    # Vision-verify the signature verdict in BOTH directions. Handwriting
    # does not survive OCR, so a wet-signed form and a blank one produce the
    # same text — which makes the text-level verdict unreliable whichever way
    # it lands, not only when it says "No".
    #
    # Guarding one direction guarded the harmless one. A false "No" raises a
    # finding a reviewer dismisses in seconds. A false "Yes" silently removes
    # a finding nobody knows was needed, and reports an unsigned
    # certification as compliant — which is the failure that survives to an
    # external audit.
    #
    # The direction is also the one most easily produced by accident: a
    # signature date read off an adjacent document filed under the same
    # classification is enough, and the certification form travels with
    # several signed companions.
    #
    # Cost is one vision call per case, where it was previously one on the
    # 82% of cases whose text said "No".
    draft_watermark = False
    if certification_info:
        verdict = _verify_cert_signature_vision(cert_groups, page_texts, settings)
        if verdict:
            text_verdict = certification_info.isSigned
            vision_verdict = "Yes" if verdict["signed"] else "No"
            if text_verdict != vision_verdict:
                logger.info(
                    "Vision overrides text-level isSigned=%s with %s on cert "
                    "page %s", text_verdict, vision_verdict, verdict["page"],
                )
            certification_info.isSigned = vision_verdict
            draft_watermark = verdict["draft_watermark"]
            if draft_watermark:
                logger.info(
                    "Vision detected draft watermark on cert page %s",
                    verdict["page"],
                )

    # The roster is passed to the income and asset extractors so memberName
    # comes back in the certification's spelling; each of their calls now
    # sees a single document and would otherwise have no roster to match.
    household_names = [
        f"{m.FirstName or ''} {m.LastName or ''}".strip()
        for m in (household.houseHold if household else [])
        if (m.FirstName or m.LastName)
    ]

    # Income. The page reader lets the extractor re-read, from the image, a
    # stub or benefit letter whose amount the text did not carry.
    income = _llm_fallback(
        "Income", extract_income,
        income_groups or llm_eligible_groups, settings,
        default=IncomeExtraction(),
        certification_type=ctx.certification_type,
        household_names=household_names,
        declared_total=certification_info.householdIncome if certification_info else None,
        page_reader=_image_page_reader(page_texts, ocr_quality, income_groups or llm_eligible_groups, settings),
    )

    # Assets
    assets = _llm_fallback(
        "Assets", extract_assets,
        asset_groups or llm_eligible_groups, settings,
        default=AssetExtraction(),
        certification_type=ctx.certification_type,
        household_names=household_names,
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
    # The caller's type is the contract and stays on the record; the
    # document-requirement rules follow what the document shows itself to
    # be, and a contradiction is reported rather than acted on silently.
    cert_type_findings: list = []
    ctx.document_certification_type = ctx.certification_type
    if certification_info:
        shown = _document_certification_type(certification_info)
        if shown:
            ctx.document_certification_type = shown
            if ctx.certification_type and not _same_cert_type(shown, ctx.certification_type):
                cert_type_findings.append(make_finding(
                    "CERT_TYPE_CONTRADICTS_DOCUMENT",
                    f"The case was submitted as {ctx.certification_type} but the certification form "
                    f"shows a move-in: its move-in date {certification_info.moveInDate} equals the "
                    f"effective date — confirm the certification type; the document rules for a "
                    f"move-in were applied (Section 12)",
                    label="Submitted certification type contradicts the document",
                    category=CATEGORY_FILE_REVIEW,
                    subject_type="certification",
                    subject_ref={"field": "certificationType"},
                    assignment=ASSIGN_CLIENT,
                    correction_required="Confirm the certification type against the form and resubmit if it was entered wrongly",
                    resolution_type=RESOLVE_PRESENCE,
                ))

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

    # Step 3c: sources that exist only as paystubs get a record now, so the
    # steps that follow (questionnaire linking, name reconciliation, the
    # duplicate resolver) see them. The calculations themselves are computed
    # once, from the final list, just before the findings — see Step 4f.
    if income:
        _reconstruct_orphan_paystub_sources(income)
    income_calculations: list = []

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
    questionnaire_findings: list = []
    if questionnaire_disclosures and income:
        questionnaire_findings = _link_questionnaire_to_income(questionnaire_disclosures, income, document_groups)

    # Step 4d: Reconcile name variants across all records
    from app.services.name_reconciler import reconcile_names
    name_findings = reconcile_names(household, income, assets, document_groups)

    # Step 4e: Deduplicate household members (after name reconciliation)
    # Multiple extraction sources (cert form, questionnaire, application, VOI)
    # can produce duplicate member records for the same person.
    if household and household.houseHold:
        dedup_findings = _deduplicate_household_members(household)
        name_findings.extend(dedup_findings)

    # Step 4f: the income list is final here — questionnaire stubs added,
    # names reconciled, members merged — so duplicates are resolved and the
    # calculations computed now. Computing them at 3c produced rows for
    # records later removed (a $0.00 SSI row with no record behind it) and
    # under names later renamed, so nothing keyed on (member, source)
    # could join the two.
    if income:
        income.sourceIncome.verificationIncome = _resolve_duplicate_self_declarations(
            income.sourceIncome.verificationIncome
        )
        income.sourceIncome.verificationIncome, merge_findings = _merge_household_level_sources(
            income.sourceIncome.verificationIncome
        )
        name_findings.extend(merge_findings)
        income.sourceIncome.verificationIncome, declared_findings = _collapse_declared_duplicates(
            income.sourceIncome.verificationIncome
        )
        name_findings.extend(declared_findings)
    # Step 4g: identity fields by document authority. The extractor picked
    # whichever SSN or date of birth it read first; the certification's
    # printed value now wins over a questionnaire's handwriting, and every
    # disagreement between documents is a finding.
    identity_findings: list = []
    if household and household.houseHold:
        try:
            identity_findings = resolve_identities(
                household, document_groups,
                {pn: (q.get("text") or "") for pn, q in ocr_quality.items()},
            )
        except Exception:
            logger.exception("Identity resolution failed — keeping extracted values")
    logger.info("Step 4f/6: Computing income calculations from the final income list")
    income_calculations = _compute_income_calculations(income, certification_info, ctx) if income else []

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
    findings.extend(questionnaire_findings)
    findings.extend(required_field_findings)
    findings.extend(cert_type_findings)
    findings.extend(_rent_identity_findings(certification_info, document_groups))
    findings.extend(_reconciliation_findings(income, ctx))
    findings.extend(identity_findings)
    # Pages the classifier could only place approximately: reviewable, not
    # silently extracted as something they are not.
    approx = [p for p in classification.pages if p.fit in ("nearest", "none")]
    if approx:
        listed = "; ".join(
            f"p{p.page} '{(p.observed_title or 'untitled')[:40]}' → {p.document_type}"
            for p in approx[:8]
        )
        findings.append(
            f"{len(approx)} page(s) matched no canonical document type exactly and were "
            f"routed by nearest match — confirm their type: {listed}"
        )
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
        elif details.startswith("[rejected]"):
            note = details[len("[rejected] "):]
            who = f" ({calc.memberName})" if calc.memberName else ""
            findings.append(
                f"Income source '{calc.sourceName}'{who}: {calc.method} calculation "
                f"rejected — {note} (Section 9)"
            )
        elif calc.method == "paystub-based" and calc.annualIncome:
            m = re.search(r"avg\((\d+) stubs?\)", details)
            if m and int(m.group(1)) < PAYSTUB_GUIDANCE_COUNT:
                n = int(m.group(1))
                findings.append(make_finding(
                    "PAYSTUB_COUNT_BELOW_GUIDANCE",
                    f"{calc.memberName or 'Member'}: income from {calc.sourceName or 'employer'} is "
                    f"calculated from {n} pay stub(s) (${float(calc.annualIncome):,.2f}/year); verification "
                    f"guidance expects at least {PAYSTUB_GUIDANCE_COUNT} consecutive stubs or an employer "
                    f"verification — obtain the missing stubs or a VOI (Section 9)",
                    label="Fewer pay stubs than verification guidance expects",
                    category=CATEGORY_INCOME,
                    subject_type="income_record",
                    subject_ref={"member_name": calc.memberName, "source_name": calc.sourceName},
                    assignment=ASSIGN_CLIENT,
                    correction_required=f"Obtain at least {PAYSTUB_GUIDANCE_COUNT} consecutive pay stubs or an employer verification",
                    resolution_type=RESOLVE_PRESENCE,
                ))
    from app.services.income_calculator import ytd_divergence_findings
    findings.extend(ytd_divergence_findings(income_calculations))

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
            text_of(f) for f in findings
            if text_of(f).startswith("Missing required compliance document")
        ]
        certification_info.formsPresent = sorted(forms_present)
        certification_info.missingForms = missing_forms
        # Compliance status must consider BOTH missingForms (moved out of
        # findings by the dedup step below) AND the remaining findings.
        has_issues = (
            bool(missing_forms)
            or any(
                "missing" in text_of(f).lower()
                or "not signed" in text_of(f).lower()
                or "resubmission" in text_of(f).lower()
                for f in findings
            )
        )
        certification_info.complianceStatus = "Incomplete" if has_issues else "Complete"

        # Remove the findings that were moved into missingForms — each fact
        # should appear exactly once in the output.
        missing_set = set(missing_forms)
        findings[:] = [f for f in findings if text_of(f) not in missing_set]

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
    cert_form_type = next(
        (g.document_type for g in cert_groups if is_current_certification_form(g.document_type)),
        None,
    )
    score_business_rules(
        score_cards, certification_type=ctx.certification_type, cert_form_type=cert_form_type,
    )

    # Stage 4: the audit's own findings. Every stage above asks a question of
    # one value in isolation, so none of them can see that the extracted
    # sources sum to something the certification contradicts — that is a
    # relationship between values, not a property of one. Runs last so a
    # dispute has the final word over a field that passed its format check.
    # Dedupe first: a finding raised once per source document would otherwise
    # be counted once per copy against the same field.
    score_findings(score_cards, dedupe_findings(findings))

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
    findings.extend(_unverifiable_income_amounts(score_cards))
    for card in score_cards:
        for fs in card.flagged_fields:
            if (card.record_type, fs.field_name) in _BUSINESS_RULE_COVERED:
                continue
            if _flagged_only_by_dispute(fs):
                # The dispute that dragged this field down is already in the
                # findings list, stated once and in plain language. Repeating
                # it per field turns one finding into six identical lines —
                # "Disputed by TIC_TOTAL_NO_CALCULATIONS" against every
                # certification field — which is the per-field noise this
                # suppression list exists to prevent.
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

    # Collapse exact repeats before emitting: several detectors iterate per
    # source document rather than per record.
    deduped_findings = dedupe_findings(findings)

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
        findings=render_findings(deduped_findings),
        finding_records=finding_records(deduped_findings),
        field_scores=score_summary,
        page_ocr=page_ocr,
    )


# The fields an income record can carry its amount in. One of them has to
# hold a figure or the record says nothing about how much the household earns.
_INCOME_AMOUNT_FIELDS = ("selfDeclaredAmount", "rateOfPay", "ytdAmount")


def _flagged_only_by_dispute(field_score) -> bool:
    """Whether a field is flagged solely because a finding disputed it.

    A field can be both disputed and independently suspect — an unverified
    value that a mismatch also implicates. Only the first case is redundant,
    so this asks whether anything OTHER than the dispute pulled the field
    below its threshold.
    """
    stages = getattr(field_score, "stages", None) or []
    if not any(s.stage == "finding" for s in stages):
        return False
    return all(
        s.stage == "finding" or s.score >= GREEN_THRESHOLD
        for s in stages
    )


def _unverifiable_income_amounts(score_cards: list) -> list:
    """Report income amounts that appear nowhere the engine is allowed to read.

    An income record whose amount cannot be found in any non-ignored document
    is not a low-confidence field — it is a household income figure resting on
    nothing in the file. That distinction is invisible in the per-field score
    line, which reads like every other "verify manually" nag.

    Seen on a real packet: a Social Security benefit of $1,810.00/month, the
    household's only income, occurring exactly once in thirty-two pages — on
    the Income Calculation Worksheet, which is excluded by design because it
    is management's own arithmetic rather than evidence. Either the model
    derived the figure from the annual total and presented it as extracted, or
    it read a page it should not have. Both are worth a reviewer's attention
    and neither is apparent from a yellow field.
    """
    out: list = []
    for card in score_cards:
        if card.record_type != "income":
            continue
        for fs in card.fields:
            if fs.field_name not in _INCOME_AMOUNT_FIELDS or not fs.value:
                continue
            source = next(
                (st for st in fs.stages if st.stage == "source_verification"),
                None,
            )
            # Only a genuine miss counts. A value found elsewhere in the
            # packet has been corroborated, just not by this record's own
            # documents, and that is already reported as its own field score.
            if source is None or source.score > 0.5:
                continue
            # The card's label is "member — source"; split it back into the
            # parts the subject reference is keyed on. Passing the joined
            # label as one subject produces a key that matches neither half,
            # so the finding would never reach the record it concerns.
            member, _, source = (card.record_label or "").partition("—")
            out.append(make_finding(
                "INCOME_AMOUNT_NOT_IN_SOURCE",
                f"Income amount for '{card.record_label or 'Unknown'}' "
                f"({fs.field_name} = {fs.value}) appears in no document the "
                f"audit reads — it cannot be corroborated against the file "
                f"(Section 9)",
                label="Income amount has no corroboration in the packet",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                subject_ref={
                    "member_name": member.strip(),
                    "source_name": source.strip(),
                },
                assignment=ASSIGN_INTERNAL,
                correction_required=(
                    "Obtain third-party verification of this amount, or "
                    "confirm which document in the file states it"
                ),
                resolution_type=RESOLVE_PRESENCE,
            ))
    return out


def _link_questionnaire_to_income(
    disclosures,
    income: IncomeExtraction,
    document_groups: list,
) -> list:
    """Link questionnaire employer disclosures to income entries.

    If the questionnaire names employers, try to match them to existing VI
    records and populate selfDeclaredSource. An employer nothing in the
    packet accounts for becomes a finding. Returns the findings.
    """
    from app.services.parsers.source_normalizer import normalize_source_name

    findings: list = []
    if not disclosures or not disclosures.employers:
        return findings

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

    # The application's employment section states when each job began; a
    # wage record with no hire date takes it, so a year-to-date projection
    # for a job that started in April is measured from April, not January.
    start_dates: dict[str, str] = {}
    for block in (getattr(disclosures, "employment", None) or []):
        name = (normalize_source_name(block.employer or "") or block.employer or "").lower().strip()
        if name and block.start_date:
            start_dates[name] = block.start_date

    def _adopt_start_date(employer_norm: str, vi) -> None:
        if getattr(vi, "hireDate", None):
            return
        for name, start in start_dates.items():
            if name == employer_norm or _fuzzy_employer_match(name, employer_norm) or source_names_overlap(name, employer_norm):
                vi.hireDate = start
                vi.evidence = dict(vi.evidence or {})
                vi.evidence.setdefault("hireDate", f"start date stated on the application for {block_label(name)}")
                return

    def block_label(name: str) -> str:
        return next((b.employer for b in (getattr(disclosures, "employment", None) or [])
                     if (normalize_source_name(b.employer or "") or b.employer or "").lower().strip() == name), name)

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
                    or _fuzzy_employer_match(employer_norm, source_key)
                    or source_names_overlap(employer_norm, source_key)):
                vi = vi_entries[idx]
                if not vi.selfDeclaredSource:
                    vi.selfDeclaredSource = _get_questionnaire_source(document_groups)
                _adopt_start_date(employer_norm, vi)
                matched = True
                break

        # Paystubs verify employment without a verificationIncome record of
        # their own; an employer they name is not an undisclosed one.
        if not matched and any(
            source_names_overlap(employer_norm, (ps.sourceName or "").lower())
            for ps in income.sourceIncome.payStub
        ):
            matched = True
        # The declared bucket (extractor) already reads the questionnaire;
        # a line it produced for this employer is reconciled there and
        # reported as declared-only when nothing verifies it.
        if not matched and any(
            source_names_overlap(employer_norm, (d.sourceName or "").lower())
            for d in (income.declared or [])
        ):
            matched = True
        # The only job on the application and the only wage source in the
        # file are the same job even when the scan spells the employer two
        # ways ("Stafmark" on the application, "Staffink" on the stub): the
        # start date goes to that record, and the record says on what basis.
        if len(disclosures.employers) == 1:
            wage_records = [vi for vi in vi_entries if "wage" in (vi.incomeType or "").lower()
                            and vi.verificationStatus != "declared_only"]
            if len(wage_records) == 1:
                # "Stafmark" on the application, "Staffink" on the stubs: the
                # names need not match for the one job on the application to
                # be the one wage source in the file.
                sole = wage_records[0]
                matched = True
                if not sole.selfDeclaredSource:
                    sole.selfDeclaredSource = _get_questionnaire_source(document_groups)
                if employer_norm in start_dates and not sole.hireDate:
                    sole.hireDate = start_dates[employer_norm]
                    sole.evidence = dict(sole.evidence or {})
                    sole.evidence.setdefault(
                        "hireDate",
                        f"start date stated on the application for {block_label(employer_norm)}, the only employer "
                        f"it names; this is the only wage source in the file",
                    )
        if not matched:
            # The questionnaire names an employer and states no figure. That
            # is a finding about the packet, not an income record: as a
            # record it arrived with eight empty fields scored red and no
            # member, and Cartograph received an earner nobody could place.
            logger.info("Questionnaire employer '%s' has no matching income record", employer)
            name = normalize_source_name(employer) or employer
            findings.append(make_finding(
                "QUESTIONNAIRE_EMPLOYER_UNVERIFIED",
                f"The {_get_questionnaire_source(document_groups) or 'questionnaire'} names "
                f"'{name}' as an employer, but the packet carries no verification, pay stubs "
                f"or certification line for it — verify whether this employment is current "
                f"and obtain third-party verification if so (Section 11)",
                label="Employer named on the questionnaire with nothing to verify it",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                subject_ref={"source_name": name},
                assignment=ASSIGN_CLIENT,
                correction_required="Obtain an employer verification or pay stubs, or a statement that the employment has ended",
                resolution_type=RESOLVE_PRESENCE,
            ))
    return findings


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
    """Decide from the page image whether the cert form carries signatures.

    OCR cannot see handwriting: a wet-signed form and a blank one OCR to the
    same empty signature cells, so the text-level verdict is a coin flip in
    either direction (measured: text-level "No" fires on 82% of cases with
    zero correlation to human reviewer rejections). Only the image can tell,
    so this is authoritative when it can reach a page.

    Checks the cert group's signature pages — those whose text mentions
    'signature' — falling back to the group's last page.

    Returns {"signed": bool, "draft_watermark": bool, "page": int} or None
    when no page could be checked (missing images, vision failure); callers
    keep the text-based verdict in that case.
    """
    from app.services.llm_service import call_llm_vision_json

    group = next(
        (g for g in cert_groups
         if is_current_certification_form(g.document_type)),
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

# ---------------------------------------------------------------------------
# Required-field recovery from the page image
# ---------------------------------------------------------------------------

# The labels a certification form prints beside each must-exist cell. Used
# only after the page image has been transcribed, to tell "the cell is blank
# on the form" from "the engine could not read the cell".
_CERT_FIELD_LABELS = {
    "effectiveDate": ("effective date",),
    "tenantRent": ("tenant rent", "total tenant payment", "tenant payment"),
    "utilityAllowance": ("utility allowance",),
    "grossRent": ("gross rent",),
    "householdIncome": (
        "total annual household income", "total household income",
        "total annual income", "total income", "household income",
    ),
    "unitNumber": ("unit number", "unit no", "unit #", "apt", "unit"),
    "householdSize": (
        "household size", "number of household members", "no. of members",
        "family size", "number in household",
    ),
    "numberOfBedrooms": ("bedroom", "# br", "br size", "unit size"),
}
_CERT_FIELD_TITLES = {
    "effectiveDate": "Effective Date",
    "tenantRent": "Tenant Rent",
    "utilityAllowance": "Utility Allowance",
    "grossRent": "Gross Rent",
    "householdIncome": "Total Annual Household Income",
    "unitNumber": "Unit Number",
    "householdSize": "Household Size",
    "numberOfBedrooms": "Number of Bedrooms",
}
_CERT_FIELD_CATEGORY = {
    "tenantRent": CATEGORY_UNIT_RENT,
    "utilityAllowance": CATEGORY_UNIT_RENT,
    "grossRent": CATEGORY_UNIT_RENT,
    "householdIncome": CATEGORY_INCOME,
}
_MONEY_CERT_FIELDS = frozenset({"tenantRent", "utilityAllowance", "grossRent", "householdIncome"})
# Member cells the recovery reports on when still missing. The SSN slot is
# left out: a blank one is already a document-side finding
# (MEMBER_SSN_MISSING), and it is the one required cell that can be
# legitimately empty.
_REPORTED_MEMBER_FIELDS = ("DOB", "relationship")
_MEMBER_FIELD_TITLES = {"DOB": "Date of Birth", "relationship": "Relationship"}
_MAX_RECOVERY_PAGES = 3
_MIN_TRANSCRIPT_CHARS = 200
_BLANK_MARK = "[blank]"
_BLANK_WINDOW = 40

_FORM_TRANSCRIPTION_PROMPT = """\
You are transcribing ONE page of an affordable-housing certification form
(LIHTC Tenant Income Certification, HUD 50059, RD 3560-8, or similar) from
its image, so that a text-only reader can recover every field on it.

RULES:
- Write every field label together with the value in its cell, one field per
  line, in reading order: "Tenant Rent: $953.00".
- When a value cell is EMPTY, write the label followed by [blank]:
  "Rental Assistance: [blank]". Never leave a label without a value or [blank].
- Transcribe printed and handwritten values exactly as written. Never derive
  a value from another field or by arithmetic.
- Reproduce tables (household composition, income, assets, rent) as HTML
  <table> rows, one member or source per row, keeping every column and its
  header; empty cells are [blank].
- Checkboxes: "[X] Label" or "[ ] Label".
- Keep part/section headings (e.g. "PART VII. RENT") and form field numbers
  (e.g. "86. Total Annual Income").
Return ONLY the transcription, no commentary."""


def _required_field_gaps(household, certification_info) -> dict:
    """What the certification form must carry but the extraction lacks."""
    cert_missing = (
        [f for f in CRITICAL_CERT_FIELDS if not getattr(certification_info, f, None)]
        if certification_info else []
    )
    members = [m.model_dump() for m in household.houseHold] if household else []
    shortfall = 0
    size_raw = certification_info.householdSize if certification_info else None
    try:
        size = int(float(size_raw)) if size_raw else 0
    except (TypeError, ValueError):
        size = 0
    if size > len(members):
        shortfall = size - len(members)
    return {
        "cert": cert_missing,
        "members": required_member_gaps(members),
        "shortfall": shortfall,
    }


def _label_blank_in(transcript: str, label: str) -> bool:
    """True when the transcript shows `label` followed by the blank marker."""
    start = 0
    while True:
        idx = transcript.find(label, start)
        if idx < 0:
            return False
        window = transcript[idx + len(label): idx + len(label) + _BLANK_WINDOW]
        if _BLANK_MARK in window:
            return True
        start = idx + len(label)


def _required_cert_field_finding(field: str, transcript: str, pages: list[int]):
    title = _CERT_FIELD_TITLES.get(field, field)
    category = _CERT_FIELD_CATEGORY.get(field, CATEGORY_FILE_REVIEW)
    blank = any(_label_blank_in(transcript, lab) for lab in _CERT_FIELD_LABELS.get(field, ()))
    if blank:
        return make_finding(
            "REQUIRED_FIELD_BLANK_ON_FORM",
            f"Certification form shows no value for '{title}' — the field is on the form and "
            f"no entry was legible, even on the page image; if the form carries a faint "
            f"handwritten entry, read it by hand, otherwise the certification is incomplete without it",
            label=f"{title} shows no value on the certification",
            category=category,
            subject_ref={"field": field},
            result="non_compliant",
            assignment=ASSIGN_CLIENT,
            correction_required=f"Complete '{title}' on the certification form",
            resolution_type=RESOLVE_PRESENCE,
            pages=pages,
        )
    page_txt = ", ".join(str(p) for p in pages)
    return make_finding(
        "REQUIRED_FIELD_UNREADABLE",
        f"'{title}' could not be read from the certification form, even after "
        f"re-reading the page image — read it by hand from page(s) {page_txt}",
        label=f"{title} unreadable on the certification",
        category=category,
        subject_ref={"field": field},
        result="na",
        assignment=ASSIGN_INTERNAL,
        correction_required=f"Read '{title}' from the form and enter it by hand",
        resolution_type=RESOLVE_PRESENCE,
        pages=pages,
    )


def _required_member_field_finding(name: str, fields: list[str], pages: list[int]):
    page_txt = ", ".join(str(p) for p in pages)
    what = " and ".join(_MEMBER_FIELD_TITLES.get(f, f) for f in fields)
    return make_finding(
        "REQUIRED_FIELD_UNREADABLE",
        f"{name}: {what} blank or unreadable on the certification form's "
        f"household composition, even after re-reading the page image — "
        f"confirm from page(s) {page_txt}",
        label=f"{what} missing for {name}",
        category=CATEGORY_MEMBER,
        subject_type="household_member",
        subject_ref={"member_name": name, "field": ",".join(fields)},
        result="na",
        assignment=ASSIGN_INTERNAL,
        correction_required=f"Read {what} for {name} from the form and enter it by hand",
        resolution_type=RESOLVE_PRESENCE,
        pages=pages,
    )


_MAX_IMAGE_PAGES_PER_READ = 3


def _image_page_reader(page_texts: list[dict], ocr_quality: dict[int, dict], groups, settings: Settings):
    """A callable the income extractor uses to re-read pages from their images.

    Transcribes up to _MAX_IMAGE_PAGES_PER_READ of the pages asked for,
    once each per case, swaps the transcript in wherever page text is read
    (the page record, the OCR quality map, the owning group's text) and
    returns the pages re-read. A "Pay Statement Preview" stub scored green
    with its gross unread, and an SSA letter's "payment is 994.00" came
    back as "0.00": both are pages whose image says what the text did not.
    """
    from concurrent.futures import ThreadPoolExecutor
    from app.services.llm_service import call_llm_vision

    paths = {pt["page"]: pt.get("image_path") for pt in page_texts}
    by_page = {pt["page"]: pt for pt in page_texts}
    done: set[int] = set()

    def _read(pages: list[int]) -> list[int]:
        todo = []
        for pn in pages:
            flags = (by_page.get(pn) or {}).get("ocr_flag_details") or []
            already = pn in done or any(
                (f.get("code") if isinstance(f, dict) else f) in ("vision_fallback", "required_field_recovery")
                for f in flags
            )
            if paths.get(pn) and not already and pn not in todo:
                todo.append(pn)
        todo = todo[:_MAX_IMAGE_PAGES_PER_READ]
        if not todo:
            return []
        logger.info("Income page recovery: transcribing page(s) %s from image", todo)

        def _transcribe(pn: int) -> tuple[int, str | None]:
            try:
                return pn, call_llm_vision(
                    _FORM_TRANSCRIPTION_PROMPT,
                    f"Transcribe packet page {pn}, an income document (a pay stub or a benefit letter). "
                    f"Keep every earnings row with its rate, hours, current and year-to-date amounts, the pay "
                    f"date and pay period, and every sentence that states a benefit amount, exactly as printed.",
                    [str(paths[pn])], settings,
                )
            except Exception:
                logger.exception("Income page recovery: transcription failed for page %d", pn)
                return pn, None

        got: list[int] = []
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            for pn, text in pool.map(_transcribe, todo):
                done.add(pn)
                if not text or len(text.strip()) < _MIN_TRANSCRIPT_CHARS:
                    continue
                pt = by_page[pn]
                pt["text"] = text
                pt["ocr_flag"] = "green"
                flags = pt.get("ocr_flag_details")
                if not isinstance(flags, list):
                    flags = pt["ocr_flag_details"] = []
                for f in ("vision_fallback", "income_page_recovery"):
                    if f not in flags:
                        flags.append(f)
                q = ocr_quality.setdefault(pn, {})
                q["text"] = text
                q["flag"] = "green"
                got.append(pn)
        for g in groups:
            if any(pn in got for pn in g.pages):
                g.combined_text = "\n\n".join(
                    f"--- Page {p} ---\n{(by_page.get(p) or {}).get('text', '')}" for p in g.pages
                )
        return got

    return _read


def _recover_required_fields_from_images(
    household,
    certification_info,
    cert_groups,
    page_texts: list[dict],
    ocr_quality: dict[int, dict],
    settings: Settings,
    certification_type: str | None,
) -> list:
    """Second-stage retry for the cells a certification form must carry.

    The text retry re-reads the same OCR text, so when OCR dropped the value
    cells it cannot succeed (observed: a TIC whose Part VII labels survived
    and every amount beside them vanished, on a page scored green). This
    stage transcribes the certification form's pages from their images, swaps
    that text in wherever page text is read downstream — the extractor's
    group text, source verification, completeness, and the stored page
    record — and runs the targeted text retries once more over it.

    Bounded: fires only when a must-exist cell is still null, reads at most
    _MAX_RECOVERY_PAGES pages, once per case. Scoped to the certification
    form on purpose: income and asset records may legitimately be absent,
    and hunting for them in images is how derived figures get in.

    Returns findings for what is still missing afterwards. A cell the
    transcript shows blank is the form's omission (client); a cell it shows
    filled that the retry still cannot read is the engine's (internal, "na").
    """
    gaps = _required_field_gaps(household, certification_info)
    if not (gaps["cert"] or gaps["members"] or gaps["shortfall"]):
        return []
    group = next(
        (g for g in cert_groups if is_current_certification_form(g.document_type)),
        None,
    )
    if not group:
        return []
    paths = {pt["page"]: pt.get("image_path") for pt in page_texts}
    pages = [p for p in group.pages if paths.get(p)][:_MAX_RECOVERY_PAGES]
    if not pages:
        logger.info("Required-field recovery: no page images for cert pages %s", group.pages)
        return []
    logger.info(
        "Required-field recovery: cert fields %s, member gaps %s, member shortfall %d "
        "— transcribing cert page(s) %s",
        gaps["cert"],
        [f"{g['name']}:{'/'.join(g['missing'])}" for g in gaps["members"]],
        gaps["shortfall"], pages,
    )

    from concurrent.futures import ThreadPoolExecutor
    from app.services.llm_service import call_llm_vision
    from app.services.validation import normalize_date, normalize_money

    def _transcribe(pn: int) -> tuple[int, str | None]:
        try:
            return pn, call_llm_vision(
                _FORM_TRANSCRIPTION_PROMPT,
                f"Transcribe packet page {pn} of this certification form.",
                [str(paths[pn])], settings,
            )
        except Exception:
            logger.exception("Required-field recovery: transcription failed for page %d", pn)
            return pn, None

    transcripts: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=len(pages)) as pool:
        for pn, text in pool.map(_transcribe, pages):
            if text and len(text.strip()) >= _MIN_TRANSCRIPT_CHARS:
                transcripts[pn] = text
            elif text is not None:
                logger.info("Required-field recovery: page %d transcript too short, keeping OCR text", pn)
    if not transcripts:
        return []

    # Swap the transcripts in everywhere downstream reads page text.
    by_page = {pt["page"]: pt for pt in page_texts}
    for pn, text in transcripts.items():
        pt = by_page[pn]
        logger.info(
            "Required-field recovery: page %d text replaced (%d -> %d chars)",
            pn, len(pt.get("text") or ""), len(text),
        )
        pt["text"] = text
        # Provenance, not a quality verdict: the transcript is the best read
        # of the page, and "yellow" made the scorer treat "not found" on it
        # as poor OCR.
        pt["ocr_flag"] = "green"
        flags = pt.get("ocr_flag_details")
        if not isinstance(flags, list):
            flags = pt["ocr_flag_details"] = []
        for f in ("vision_fallback", "required_field_recovery"):
            if f not in flags:
                flags.append(f)
        q = ocr_quality.setdefault(pn, {})
        q["text"] = text
        q["flag"] = "green"
    group.combined_text = "\n\n".join(
        f"--- Page {p} ---\n{(by_page.get(p) or {}).get('text', '')}" for p in group.pages
    )
    texts = build_group_texts([group])
    transcript_all = "\n".join(transcripts.values()).lower()

    findings: list = []

    # Certification cells
    if gaps["cert"] and certification_info:
        known = {
            k: v for k, v in certification_info.model_dump().items()
            if v not in (None, "", [], "null")
        }
        retry = retry_cert_info_fields(texts, gaps["cert"], known, certification_type, settings)
        recovered: list[str] = []
        for f in gaps["cert"]:
            raw = retry.get(f)
            if raw in (None, "", "null"):
                continue
            if f in _MONEY_CERT_FIELDS:
                val = normalize_money(str(raw))
            elif f == "effectiveDate":
                val = normalize_date(str(raw))
            else:
                val = str(raw).strip() or None
            if val:
                setattr(certification_info, f, val)
                recovered.append(f)
        still = [f for f in gaps["cert"] if f not in recovered]
        logger.info(
            "Required-field recovery: cert fields recovered %s, still missing %s",
            recovered, still,
        )
        for f in still:
            findings.append(_required_cert_field_finding(f, transcript_all, group.pages))

    # Member cells
    if gaps["members"] or gaps["shortfall"]:
        member_dicts = [m.model_dump() for m in household.houseHold]
        retry = retry_member_fields(
            texts, gaps["members"], gaps["shortfall"], certification_type, settings,
        )
        recovered = merge_member_fields(member_dicts, retry, allow_new=gaps["shortfall"])
        household.houseHold = [HouseholdMember.model_validate(d) for d in member_dicts]
        still = [
            {"name": g["name"], "missing": [f for f in g["missing"] if f in _REPORTED_MEMBER_FIELDS]}
            for g in required_member_gaps(member_dicts)
        ]
        still = [g for g in still if g["missing"]]
        logger.info(
            "Required-field recovery: member cells recovered %s, still missing %s",
            recovered, [f"{g['name']}:{'/'.join(g['missing'])}" for g in still],
        )
        for g in still:
            findings.append(
                _required_member_field_finding(g["name"].title(), g["missing"], group.pages)
            )

    return findings


def _reconciliation_findings(income, ctx) -> list:
    """Findings from reconciling the household's declared income against the
    packet's verification documents (see extractor._reconcile_income).

    declared_only: the certification or questionnaire declares a source and
    nothing in the packet verifies it. On an AR-SC the certification is the
    source of truth and no third-party verification is expected, so the
    finding is not raised there.
    verified_not_declared: a source document carries income the
    certification's own income table does not list.
    """
    out: list = []
    if not income:
        return out
    for vi in income.sourceIncome.verificationIncome:
        member = vi.memberName or "A household member"
        what = vi.incomeType or vi.sourceName or "income"
        amount = vi.declaredAnnualAmount or vi.selfDeclaredAmount
        amount_txt = f" of ${float(amount):,.2f}" if amount and str(amount).replace('.', '', 1).isdigit() else ""
        if vi.verificationStatus == "declared_only" and ctx.certification_type != "AR-SC":
            where = vi.declaredSource or vi.selfDeclaredSource or "the certification"
            out.append(make_finding(
                "INCOME_DECLARED_NOT_VERIFIED",
                f"{member}: {what} income{amount_txt} is declared on the {where} but no "
                f"verification document in the packet carries it — third-party "
                f"verification required (Section 9)",
                label=f"Declared {what} income for {member} has no verification",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                subject_ref={"member_name": vi.memberName, "source_name": vi.sourceName},
                result="non_compliant",
                assignment=ASSIGN_CLIENT,
                correction_required=f"Obtain third-party verification of {member}'s {what} income",
                resolution_type=RESOLVE_PRESENCE,
                pages=list(vi.sourcePages or []),
            ))
        elif vi.verificationStatus == "verified_not_declared":
            pages = ", ".join(str(p) for p in (vi.sourcePages or [])) or "?"
            out.append(make_finding(
                "INCOME_VERIFIED_NOT_DECLARED",
                f"{member}: {what} income from {vi.sourceName or 'a source document'} is "
                f"verified in the packet (page(s) {pages}) but the certification's income "
                f"table does not declare it — the certification may be incomplete (Section 9)",
                label=f"Verified {what} income for {member} is not on the certification",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                subject_ref={"member_name": vi.memberName, "source_name": vi.sourceName},
                result="non_compliant",
                assignment=ASSIGN_CLIENT,
                correction_required=f"Add {member}'s {what} income to the certification or document why it is excluded",
                resolution_type=RESOLVE_PRESENCE,
                pages=list(vi.sourcePages or []),
            ))
        also = (vi.evidence or {}).get("alsoDeclared") if isinstance(vi.evidence, dict) else None
        if also:
            # A declared line the reconciliation folded into this record as a
            # second declaration of the same income. The fold is a judgement
            # (same member, nearest figure); it is stated here so a reviewer
            # can disagree and treat the line as a separate income.
            out.append(make_finding(
                "INCOME_SECOND_DECLARATION",
                f"{member}: a further declaration ({also}) was read as a second statement of the "
                f"{what} income from {vi.sourceName or 'this source'}"
                f"{amount_txt.replace(' of ', ' declared at ') if amount_txt else ''} — confirm it is "
                f"the same income and not a separate one (Section 9)",
                label=f"A second declaration was folded into {member}'s {what} income",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                subject_ref={"member_name": vi.memberName, "source_name": vi.sourceName},
                result="non_compliant",
                assignment=ASSIGN_INTERNAL,
                correction_required="If the declaration is a separate income, add it as its own source and verify it",
                resolution_type=RESOLVE_PRESENCE,
                pages=list(vi.sourcePages or []),
            ))
    return out


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
    def _money_key(value) -> str:
        try:
            return f"{float(str(value).replace('$', '').replace(',', '')):.2f}"
        except (TypeError, ValueError):
            return (str(value) if value else "").strip().lower()

    def _key(rec) -> str:
        # Documents print an account number whole, masked, or as its last
        # four ("3872-4603" on the statement, "4603" on the verification):
        # the last four digits are the identity the forms share.
        digits = re.sub(r"\D", "", rec.accountNumber or "")
        if len(digits) >= 4:
            return f"acct:{digits[-4:]}"
        if rec.accountNumber:
            return f"acct:{rec.accountNumber.strip()}"
        # No account number: the asset's identity is who owns it, what kind
        # it is, and what it is worth. Keying on the source name instead let
        # one property become two records — the extractor captured the
        # address on one mention and left it blank on the other, and both
        # $6,294.74 real-estate records were summed and delivered as
        # $12,589.48 of assets, on two consecutive runs. Same owner, same
        # type, same value, neither carrying an account number: one asset.
        value = rec.currentBalance or rec.selfDeclaredAmount or rec.averageSixMonthBalance
        return (
            f"otv:{slug(rec.assetOwner)}|"
            f"{(rec.accountType or '').lower().strip()}|"
            f"{_money_key(value)}"
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


    # Tolerance pass: two records of one owner and one type family, neither
    # carrying a different account number, whose values are within 1% or one
    # digit apart are one asset read twice with a scan slip (6,294.34 beside
    # 6,294.74). The richer record keeps its balance; the other's value is
    # kept as the self-declared figure so a real disagreement still surfaces.
    def _family(t: str | None) -> str:
        t = (t or "").lower()
        for fam, names in (("cash", ("checking", "savings", "cash", "prepaid", "direct express", "debit", "money market", "cd")),
                           ("real estate", ("real estate", "property", "home")),
                           ("investment", ("invest", "retirement", "annuity", "able", "crypto", "brokerage", "401", "ira")),
                           ("life insurance", ("life insurance",))):
            if any(n in t for n in names):
                return fam
        return t or "other"

    def _value_of(rec) -> float | None:
        for f in ("currentBalance", "selfDeclaredAmount", "averageSixMonthBalance"):
            v = getattr(rec, f, None)
            if v:
                try:
                    return float(str(v).replace(",", ""))
                except ValueError:
                    continue
        return None

    def _close(a: float, b: float) -> bool:
        if abs(a - b) <= max(0.02, abs(b) * 0.01):
            return True
        # One digit apart, but never the leading one: a scan slip turns
        # 6,294.74 into 6,294.34, it does not turn $20 into $50.
        sa, sb = f"{a:.2f}", f"{b:.2f}"
        return (len(sa) == len(sb) and sa[0] == sb[0]
                and sum(1 for x, y in zip(sa, sb) if x != y) == 1)

    def _last4(v) -> str:
        return re.sub(r"\D", "", v or "")[-4:]

    collapsed: list = []
    for rec in sorted(merged, key=_populated_count, reverse=True):
        twin = None
        for kept in collapsed:
            if slug(kept.assetOwner) != slug(rec.assetOwner) or _family(kept.accountType) != _family(rec.accountType):
                continue
            if kept.accountNumber and rec.accountNumber and _last4(kept.accountNumber) != _last4(rec.accountNumber):
                continue
            va, vb = _value_of(kept), _value_of(rec)
            if va is not None and vb is not None and _close(va, vb):
                twin = kept
                break
        if twin is None:
            collapsed.append(rec)
            continue
        if not twin.selfDeclaredAmount and rec.selfDeclaredAmount:
            twin.selfDeclaredAmount = rec.selfDeclaredAmount
            twin.selfDeclaredSource = twin.selfDeclaredSource or rec.selfDeclaredSource
        elif not twin.selfDeclaredAmount and rec.currentBalance and twin.currentBalance != rec.currentBalance:
            twin.selfDeclaredAmount = rec.currentBalance
        for f in ("accountNumber", "incomeAmount", "interestType", "percentageOfOwnership", "dateReceived", "sourceName"):
            if not getattr(twin, f, None) and getattr(rec, f, None):
                setattr(twin, f, getattr(rec, f))
        logger.info(
            "Asset dedup: merged a near-duplicate %s record for %s (%s vs %s)",
            twin.accountType, twin.assetOwner, _value_of(twin), _value_of(rec),
        )
    return collapsed


def _reconstruct_orphan_paystub_sources(income) -> None:
    """Give a source that exists only as paystubs a verificationIncome record.

    Forty-three places across eight modules iterate verificationIncome —
    the consistency checks, the duplicate detector, the roster match, field
    scoring, the Cartograph payload. A source that exists only as a stack
    of stubs is invisible to every one of them, so the household's largest
    income can be audited by nothing while the calculation quietly knows
    about it. A blank employer verification with paystubs substituted is a
    documented path, and it also surfaces whenever extraction drops the
    entry.
    """
    vi_entries = income.sourceIncome.verificationIncome
    ps_entries = income.sourceIncome.payStub
    ps_map = match_paystubs_to_sources(ps_entries, vi_entries)
    matched_ps = {id(ps) for psl in ps_map.values() for ps in psl}
    unmatched_ps = [ps for ps in ps_entries if id(ps) not in matched_ps]
    if not unmatched_ps:
        return
    # Group by (source, member) — source alone would pool stubs from two
    # household members who share an employer into one record.
    by_source: dict[tuple[str, str], list] = {}
    for ps in unmatched_ps:
        key = ((ps.sourceName or "Unknown").lower(), (ps.memberName or "").lower())
        by_source.setdefault(key, []).append(ps)
    for source_ps in by_source.values():
        first = source_ps[0]
        vi_entries.append(VerificationIncomeEntry(
            sourceName=first.sourceName,
            memberName=first.memberName,
            frequencyOfPay=first.payInterval,
            # A paystub is employment income by definition; which kind of
            # wage depends on the employer and the program, neither knowable
            # from a stub, so the broadest wage term the vocabulary carries.
            incomeType="Non-Federal Wage",
            # The stubs are the verification; the scorer reads this and does
            # not expect the fields an employer's verification form carries.
            type_of_VOI="Pay Stubs",
            sourcePages=sorted({p for ps in source_ps for p in (ps.sourcePages or [])}),
            verificationStatus="verified",
            # selfDeclaredAmount and every verification field stay unset:
            # the stubs are the evidence, and the calculation reads them.
        ))
        logger.info(
            "Reconstructed income source '%s' for '%s' from %d unmatched "
            "paystub(s) — no verification entry was extracted for it",
            first.sourceName, first.memberName, len(source_ps),
        )


def _compute_income_calculations(income, certification_info, ctx) -> list:
    """Annual income per source, a pure function of the final income list."""
    vi_entries = income.sourceIncome.verificationIncome
    ps_entries = income.sourceIncome.payStub
    ps_map = match_paystubs_to_sources(ps_entries, vi_entries)
    # Effective date anchors the stale-wage guard: EIV / Work Number wage
    # history quarters years before the cert must not be annualized into
    # current income.
    from app.services.income_calculator import _parse_date as _parse_ic_date
    reference_date = _parse_ic_date(certification_info.effectiveDate if certification_info else None)
    out: list = []
    for i, vi in enumerate(vi_entries):
        out.extend(calculate_all_methods(
            vi, ps_map.get(i, []), ctx.funding_program, reference_date=reference_date,
        ))
    matched_ps = {id(ps) for psl in ps_map.values() for ps in psl}
    unmatched_ps = [ps for ps in ps_entries if id(ps) not in matched_ps]
    if unmatched_ps:
        by_source: dict[tuple[str, str], list] = {}
        for ps in unmatched_ps:
            key = ((ps.sourceName or "Unknown").lower(), (ps.memberName or "").lower())
            by_source.setdefault(key, []).append(ps)
        for source_ps in by_source.values():
            out.extend(calculate_all_methods(
                None, source_ps, ctx.funding_program, reference_date=reference_date,
            ))
    return out


_EVIDENCE_RANK = {"verified": 0, "verified_not_declared": 0, "self_certified": 1, "declared_only": 2}


def _resolve_duplicate_self_declarations(vi_entries: list) -> list:
    """Collapse records that are the same income read twice.

    Identity is (member, source, program): the same employer legitimately
    appears once per household member who works there, and one payer can
    pay two programs (retirement and SSI) to one person. Within a group the
    keeper is chosen by evidence — a third-party record over a declaration,
    a record with an amount over one without, a non-zero amount over $0.00
    — and only then by date. Keyed on (source, member) and tie-broken by
    date, a later-dated $0.00 SSI row deleted the verified $1,489.50
    retirement record beside it.
    """
    from collections import defaultdict

    def _amount(v) -> float:
        for f in ("rateOfPay", "selfDeclaredAmount", "ytdAmount"):
            val = getattr(v, f, None)
            if val:
                try:
                    return float(str(val).replace(",", ""))
                except ValueError:
                    continue
        rows = getattr(v, "paymentHistory", None) or []
        total = 0.0
        for row in rows:
            try:
                total += float(str(getattr(row, "amount", None) or 0).replace(",", ""))
            except ValueError:
                continue
        return total

    groups: dict[tuple[str, str, str], list] = defaultdict(list)
    for vi in vi_entries:
        key = (
            (vi.memberName or "").lower().strip(),
            (vi.sourceName or "").lower().strip(),
            (vi.incomeType or "").lower().strip(),
        )
        groups[key].append(vi)
    out: list = []
    for key, entries in groups.items():
        if len(entries) == 1 or not key[1]:
            out.extend(entries)
            continue
        entries.sort(key=lambda v: (
            _EVIDENCE_RANK.get(v.verificationStatus or "verified", 1),
            0 if _amount(v) > 0 else 1,
            -(len([f for f in ("rateOfPay", "selfDeclaredAmount", "ytdAmount", "hoursPerPayPeriod", "frequencyOfPay") if getattr(v, f, None)])),
            (v.dateReceived or v.hireDate or ""),
        ))
        keeper = entries[0]
        # Fill the keeper's gaps from the copies it displaces.
        for other in entries[1:]:
            for f in ("selfDeclaredAmount", "selfDeclaredSource", "declaredAnnualAmount",
                      "declaredSource", "dateReceived", "hireDate", "programName"):
                if not getattr(keeper, f, None) and getattr(other, f, None):
                    setattr(keeper, f, getattr(other, f))
        logger.info(
            "Income: collapsed %d records for %s / %s / %s into one (kept the %s record)",
            len(entries), key[0] or "?", key[1], key[2] or "?", keeper.verificationStatus or "verified",
        )
        out.append(keeper)
    return out


def _dated_history(vi) -> int:
    from app.services.income_calculator import _parse_history_date
    return sum(1 for row in (getattr(vi, "paymentHistory", None) or [])
               if _parse_history_date(getattr(row, "date", None)))


def _generic_source(vi) -> bool:
    """A source named after its income type or its statement, not a payer."""
    name = (vi.sourceName or "").lower().strip()
    itype = (vi.incomeType or "").lower().strip()
    return not name or name == itype or itype in name or any(
        w in name for w in ("worksheet", "calculation", "statement", "ledger", "agency", "support"))


def _merge_household_level_sources(vi_entries: list) -> list:
    """One member's records of one household-level income type are one source.

    A child-support file carries the agency's ledger and, beside it, the
    manager's worksheet that totals the same payments; read one document at
    a time they became two records — the worksheet took the certification's
    declared line and the ledger was reported as verified income the
    certification never declared. Child support, alimony and assistance
    reach a household from one order or one agency; two records of the
    type for one member are two documents about it unless both carry their
    own dated payment history under distinct payer names, when they stay
    two. The keeper is the record with the dated history (else the better-
    evidenced one); it takes the other's pages, declaration and payer name.
    """
    from collections import defaultdict
    from app.services.extractor import _HOUSEHOLD_LEVEL_INCOME_TYPES

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for vi in vi_entries:
        itype = (vi.incomeType or "").lower().strip()
        member = (vi.memberName or "").lower().strip()
        if member and any(t in itype for t in _HOUSEHOLD_LEVEL_INCOME_TYPES):
            groups[(member, itype)].append(vi)
    # A worksheet names no member; the household's one member with that
    # type is whom it is about.
    for vi in vi_entries:
        itype = (vi.incomeType or "").lower().strip()
        if (vi.memberName or "").strip() or not any(t in itype for t in _HOUSEHOLD_LEVEL_INCOME_TYPES):
            continue
        owners = [k for k in groups if k[1] == itype]
        if len(owners) == 1:
            groups[owners[0]].append(vi)

    drop: set[int] = set()
    findings: list = []
    for (member, itype), entries in groups.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda v: (
            -_dated_history(v),
            _EVIDENCE_RANK.get(v.verificationStatus or "verified", 1),
        ))
        keeper = entries[0]
        for other in entries[1:]:
            if (_dated_history(keeper) and _dated_history(other)
                    and not _generic_source(keeper) and not _generic_source(other)
                    and (keeper.sourceName or "").lower().strip() != (other.sourceName or "").lower().strip()):
                continue  # two payers, each with its own ledger
            keeper.sourcePages = sorted(set(keeper.sourcePages or []) | set(other.sourcePages or []))
            for f in ("selfDeclaredAmount", "selfDeclaredSource", "declaredAnnualAmount",
                      "declaredSource", "rateOfPay", "rateUnit", "frequencyOfPay", "programName", "dateReceived"):
                if not getattr(keeper, f, None) and getattr(other, f, None):
                    setattr(keeper, f, getattr(other, f))
            if _generic_source(keeper) and not _generic_source(other):
                keeper.sourceName = other.sourceName
            statuses = {keeper.verificationStatus, other.verificationStatus}
            if "verified" in statuses or "verified_not_declared" in statuses:
                keeper.verificationStatus = (
                    "verified" if keeper.declaredAnnualAmount or keeper.selfDeclaredAmount
                    else "verified_not_declared"
                )
            keeper.evidence = dict(keeper.evidence or {})
            keeper.evidence.setdefault(
                "alsoDocumented",
                f"{other.type_of_VOI or 'record'} on page(s) {', '.join(str(p) for p in (other.sourcePages or [])) or '?'}"
                f" ({other.sourceName or itype}) — same source",
            )
            # The folded record's undated rows are a manager's worksheet:
            # its total is the figure the file was certified on, and the
            # ledger's annualised figure is compared against it.
            _note_worksheet_total(keeper, other, findings)
            logger.info("Income: %s / %s on pages %s and %s — one source; kept the record with the payment history",
                        member, itype, keeper.sourcePages, other.sourcePages)
            drop.add(id(other))
    return [vi for vi in vi_entries if id(vi) not in drop], findings


def _note_worksheet_total(keeper, other, findings: list) -> None:
    from app.services.income_calculator import _money, annualize_history
    rows = getattr(other, "paymentHistory", None) or []
    undated = [r for r in rows if not getattr(r, "date", None)]
    if len(undated) < 4 or _dated_history(other):
        return
    total = sum(_money(getattr(r, "amount", None)) or 0.0 for r in undated)
    if total <= 0:
        return
    keeper.evidence["worksheetTotal"] = (
        f"{total:.2f} over {len(undated)} listed payments "
        f"(page(s) {', '.join(str(p) for p in (other.sourcePages or []))})"
    )
    ledger = annualize_history(keeper.paymentHistory or [])
    if ledger and abs(ledger - total) / max(total, ledger) > 0.10:
        findings.append(make_finding(
            "LEDGER_WORKSHEET_DIFFER",
            f"{keeper.memberName}: the {keeper.incomeType} worksheet totals ${total:,.2f} over {len(undated)} "
            f"payments, but the agency ledger's payments annualise to ${ledger:,.2f} — the two documents in the "
            f"file disagree; confirm which payments the certification counted (Section 9)",
            label="Manager's worksheet and agency ledger disagree",
            category=CATEGORY_INCOME,
            subject_type="income_record",
            subject_ref={"member_name": keeper.memberName, "source_name": keeper.sourceName},
            assignment=ASSIGN_INTERNAL,
            correction_required="Reconcile the worksheet's payments against the ledger",
            resolution_type=RESOLVE_PRESENCE,
            pages=sorted(set(keeper.sourcePages or [])),
        ))


def _collapse_declared_duplicates(vi_entries: list) -> tuple[list, list[str]]:
    """Two declared-only records for one member and income type are one
    income declared on two documents, not two incomes.

    The 50059 lists "Soc. Sec. 21,720" and the zero-income certification
    lists "Social Security 1,810" for the same person; kept as two records
    they doubled the calculated total and raised a false TIC mismatch. The
    certification form's line is kept. A second figure that is one twelfth
    of it is its monthly basis and is noted; any other disagreement becomes
    a finding, since the household stated two different amounts.
    """
    from app.services.doc_taxonomy import is_certification_form

    def _annual(v) -> float | None:
        for f in ("declaredAnnualAmount", "selfDeclaredAmount"):
            val = getattr(v, f, None)
            if val:
                try:
                    return float(str(val).replace(",", ""))
                except ValueError:
                    continue
        return None

    from app.services.extractor import _ss_family

    groups: dict[tuple[str, str], list] = {}
    for vi in vi_entries:
        if vi.verificationStatus not in ("declared_only", "self_certified"):
            continue
        itype = (vi.incomeType or "").lower().strip()
        # The certification writes "SS" for a benefit the questionnaire
        # calls SSI: one heading, one income.
        key = ((vi.memberName or "").lower().strip(), "social security" if _ss_family(itype) else itype)
        if key[0] and key[1]:
            groups.setdefault(key, []).append(vi)

    findings: list[str] = []
    drop: set[int] = set()
    for (member, itype), entries in groups.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda v: (
            0 if is_certification_form(v.declaredSource or v.selfDeclaredSource or "") else 1,
            -(_annual(v) or 0.0),
        ))
        keeper = entries[0]
        k_annual = _annual(keeper)
        for other in entries[1:]:
            o_annual = _annual(other)
            k_doc = keeper.declaredSource or keeper.selfDeclaredSource or "the certification"
            o_doc = other.declaredSource or other.selfDeclaredSource or "another declaration"
            # Two lines on ONE document are two incomes the form lists
            # separately — a 50059 prints Social Security $8,736 and SSI
            # $6,191 for one member, and they add up to its total. Only a
            # second document restating the income is a duplicate.
            if k_doc == o_doc or (_ss_family(keeper.incomeType) and _ss_family(other.incomeType)
                                  and (keeper.incomeType or "").lower() != (other.incomeType or "").lower()
                                  and k_doc == o_doc):
                continue
            if k_annual and o_annual:
                ratio = o_annual / k_annual if k_annual else 0
                if abs(o_annual - k_annual) <= 0.02 * k_annual:
                    note = "same figure"
                elif abs(ratio - 1 / 12) < 0.01 or abs(ratio - 12) < 0.12:
                    note = "monthly basis of the same figure"
                    if not keeper.rateOfPay and abs(ratio - 1 / 12) < 0.01:
                        keeper.rateOfPay = other.selfDeclaredAmount
                        keeper.rateUnit = "monthly"
                else:
                    note = "different figure"
                    findings.append(
                        f"{keeper.memberName}: {keeper.incomeType} is declared as "
                        f"${k_annual:,.2f}/year on the {k_doc} and ${o_annual:,.2f} on the "
                        f"{o_doc} — the certification figure is used; confirm which applies (Section 9)"
                    )
            else:
                note = "no comparable amount"
            keeper.evidence = dict(keeper.evidence or {})
            keeper.evidence.setdefault("alsoDeclared", f"{o_doc}: {other.selfDeclaredAmount} ({note})")
            logger.info("Income: %s / %s declared on %s and %s — one income (%s); the %s line is kept",
                        member, itype, k_doc, o_doc, note, k_doc)
            drop.add(id(other))
    return [vi for vi in vi_entries if id(vi) not in drop], findings


def _llm_fallback(label, func, groups, settings, *, default=None, **kwargs):
    """Run one extraction stage; a stage that fails fails the case.

    This used to swallow every error and return the empty default, which
    delivered an audit with no income (or assets, or members) as a finished
    result — a rate-limited call became a clean-looking packet with nothing
    in it. An extraction failure is now ExtractionUnavailableError, which
    the job layer classifies retryable, so the case is re-run whole on the
    next cycle instead. `default` is kept for callers that pass it; it is
    no longer returned.
    """
    from app.core.exceptions import ExtractionUnavailableError
    logger.info("  %s: LLM extraction", label)
    try:
        return func(groups, settings, **kwargs)
    except ExtractionUnavailableError as exc:
        logger.error("  %s: extraction unavailable — %s", label, exc)
        raise ExtractionUnavailableError(f"{label} extraction failed: {exc}") from exc
    except Exception as exc:
        logger.exception("  %s: extraction failed", label)
        raise ExtractionUnavailableError(
            f"{label} extraction failed: {type(exc).__name__}: {exc}"
        ) from exc


_EFFECTIVE_DATE_RE = re.compile(r"effective\s*date[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def _promote_prior_certification(document_groups: list, classification) -> str | None:
    """When no current certification form is in the packet, the most recent
    prior certification becomes the form of record. Returns the note for the
    finding, or None when nothing was promoted."""
    from app.services.doc_taxonomy import _PREVIOUS_MARKER
    from app.services.validation import normalize_date
    if any(is_current_certification_form(g.document_type) and g.category != "ignore" for g in document_groups):
        return None
    priors = [g for g in document_groups if is_previous_certification(g.document_type)
              and _PREVIOUS_MARKER.lower() in g.document_type.lower()]
    if not priors:
        return None

    def _dated(g) -> str:
        m = _EFFECTIVE_DATE_RE.search(g.combined_text or "")
        return normalize_date(m.group(1)) or "" if m else ""

    best = max(priors, key=lambda g: (_dated(g), len(g.pages)))
    base = best.document_type[: best.document_type.lower().find(_PREVIOUS_MARKER.lower())].strip()
    when = _dated(best)
    best.document_type = base
    best.category = category_of(base)
    best.notes = (best.notes + "; " if best.notes else "") + "prior certification read as the form of record: no current certification in the packet"
    for pc in classification.pages:
        if pc.page in best.pages:
            pc.document_type = base
            pc.category = best.category
    note = f"the prior {base} on page(s) {best.page_range}" + (f" (effective {when})" if when else "")
    logger.warning("No current certification form in the packet — %s promoted to the form of record", note)
    return note


def _document_certification_type(certification_info) -> str | None:
    """The type the certification form itself shows, or None.

    A move-in date equal to the effective date is a move-in certification;
    nothing else on a form decides the type without the checkbox the
    extractor already reads (and which the caller's value overrides).
    """
    move_in = (certification_info.moveInDate or "").strip()
    effective = (certification_info.effectiveDate or "").strip()
    if move_in and effective and move_in == effective:
        return "MI"
    return None


def _same_cert_type(a: str, b: str) -> bool:
    """MI and IC are the same event under two names."""
    initial = {"MI", "IC", "IN"}
    a, b = a.upper(), b.upper()
    return a == b or (a in initial and b in initial)


def _rent_identity_findings(certification_info, document_groups) -> list:
    """On a tax-credit certification, tenant rent + utility allowance is the
    gross rent by the form's own definition. A read that breaks the identity
    is a misread of one of the three, or an arithmetic error on the form —
    either way one of the figures is not what it should be, and the finding
    names all three so the score reflects it."""
    from app.services.doc_taxonomy import canonical_label
    if certification_info is None:
        return []
    if not any(
        canonical_label(g.document_type)[0] == "Tenant Income Certification (TIC)"
        and not is_previous_certification(g.document_type) and g.category != "ignore"
        for g in document_groups
    ):
        return []
    try:
        tenant = float(str(certification_info.tenantRent).replace(",", ""))
        allowance = float(str(certification_info.utilityAllowance).replace(",", ""))
        gross = float(str(certification_info.grossRent).replace(",", ""))
    except (TypeError, ValueError):
        return []
    if abs(tenant + allowance - gross) <= 1.0:
        return []
    return [make_finding(
        "RENT_IDENTITY_MISMATCH",
        f"Tenant rent ${tenant:,.2f} + utility allowance ${allowance:,.2f} = ${tenant + allowance:,.2f}, "
        f"but the certification's gross rent reads ${gross:,.2f} — one of the three figures was misread "
        f"(handwritten rent fields are the usual cause) or the form's arithmetic is wrong; confirm all three "
        f"against the certification (Section 5)",
        label="Rent fields do not add up on the certification",
        category=CATEGORY_UNIT_RENT,
        subject_type="certification",
        assignment=ASSIGN_INTERNAL,
        correction_required="Read tenant rent, utility allowance and gross rent from the form and correct the one that disagrees",
        resolution_type=RESOLVE_PRESENCE,
    )]


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

    # Group by normalized name key (first + last, lowered) — and, across
    # different name keys, by date of birth plus SSN last four: "Aridia
    # Perez Trinidad" on the certification and "Aridia Perez" on the
    # application share both, and are one person however the surname was
    # written. A name match alone or an identity match alone unites them.
    parent = list(range(len(members)))

    def _find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def _union(a: int, b: int) -> None:
        parent[_find(a)] = _find(b)

    by_name: dict[str, int] = {}
    by_identity: dict[tuple[str, str], int] = {}
    identity_merged: set[int] = set()
    for i, m in enumerate(members):
        first = (m.FirstName or "").lower().strip()
        last = (m.LastName or "").lower().strip()
        key = f"{first} {last}".strip()
        if key:
            if key in by_name:
                _union(i, by_name[key])
            else:
                by_name[key] = i
        dob = (m.DOB or "").strip()
        last4 = (m.socialSecurityNumber or "").strip()[-4:]
        if dob and len(last4) == 4 and last4.isdigit():
            ident = (dob, last4)
            if ident in by_identity:
                if _find(i) != _find(by_identity[ident]):
                    identity_merged.add(i)
                    identity_merged.add(by_identity[ident])
                _union(i, by_identity[ident])
            else:
                by_identity[ident] = i
    groups: dict[str, list[int]] = {}
    for i in range(len(members)):
        groups.setdefault(str(_find(i)), []).append(i)

    # Merge duplicates
    to_remove: set[int] = set()
    for key, indices in groups.items():
        if len(indices) < 2:
            continue

        # Score each copy: count non-null fields
        _MERGE_FIELDS = (
            "householdMemberNumber", "FirstName", "MiddleName", "LastName",
            "socialSecurityNumber", "DOB", "gender", "head", "disabled",
            "student", "email", "phone",
        )

        def _field_count(m) -> int:
            return sum(1 for f in _MERGE_FIELDS if getattr(m, f, None) is not None)

        # Richest record first; between equals, the fuller name (the
        # certification prints the whole surname, the application half of it)
        scored = sorted(
            indices,
            key=lambda i: (_field_count(members[i]),
                           len(f"{members[i].FirstName or ''} {members[i].LastName or ''}")),
            reverse=True,
        )
        primary_idx = scored[0]
        primary = members[primary_idx]

        for dup_idx in scored[1:]:
            dup = members[dup_idx]
            # A conflicting identity value on the copy is evidence, not
            # noise: report it rather than dropping it with the copy.
            for field in ("DOB", "socialSecurityNumber"):
                a_val, b_val = getattr(primary, field, None), getattr(dup, field, None)
                if a_val and b_val and a_val != b_val and not (
                    field == "socialSecurityNumber" and a_val[-4:] == b_val[-4:]
                ):
                    findings.append(make_finding(
                        "MEMBER_IDENTITY_CONFLICT",
                        f"{primary.FirstName or ''} {primary.LastName or ''}: two extracted "
                        f"records for this member disagree on {field} ({a_val} vs {b_val}); "
                        f"the more complete record's value is kept (Section 4)",
                        label=f"{field} differs between extracted copies of a member",
                        category=CATEGORY_MEMBER,
                        subject_type="household_member",
                        subject_ref={"member_name": f"{primary.FirstName or ''} {primary.LastName or ''}".strip(), "field": field},
                        assignment=ASSIGN_INTERNAL,
                        correction_required=f"Confirm the member's {field} against the certification form",
                        resolution_type=RESOLVE_PRESENCE,
                    ))
            # Fill gaps in primary from duplicate
            for field in _MERGE_FIELDS:
                if getattr(primary, field, None) is None and getattr(dup, field, None) is not None:
                    setattr(primary, field, getattr(dup, field))
            to_remove.add(dup_idx)
            dup_name = f"{dup.FirstName or ''} {dup.LastName or ''}".strip()
            primary_name = f"{primary.FirstName or ''} {primary.LastName or ''}".strip()
            basis = (" — same date of birth and SSN last four under a different spelling of the name"
                     if dup_idx in identity_merged or primary_idx in identity_merged else "")
            findings.append(make_finding(
                "MEMBER_MERGED",
                f"Merged duplicate household member '{dup_name}' "
                f"(member #{dup.householdMemberNumber or '?'} into #{primary.householdMemberNumber or '?'}){basis}",
                label="One member was extracted twice",
                category=CATEGORY_MEMBER,
                subject_type="household_member",
                subject_ref={"member_name": primary_name},
                assignment=ASSIGN_INTERNAL,
                correction_required="Confirm the household roster against the certification form",
                resolution_type=RESOLVE_PRESENCE,
            ))

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
        "Acknowledgement of Receipt": "Acknowledgement of Receipt of HUD Forms — signed by all adults",
    }
    assert_known(required_hud_forms, "pipeline.required_hud_forms")
    is_hud_property = any(
        canonical_label(g.document_type)[0] == "HUD 50059"
        and not is_previous_certification(g.document_type)
        for g in document_groups
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
    cert_form_present = any(
        is_current_certification_form(g.document_type)
        for g in document_groups
        if g.category != "ignore"
    )
    ocr_failed_pages = sorted(
        p for g in document_groups if g.document_type == "OCR Failed"
        for p in g.pages
    )
    if ctx and ctx.certification_is_prior:
        findings.append(make_finding(
            "CERT_FORM_IS_PRIOR",
            f"The packet holds no current certification form; {ctx.certification_is_prior} was read as "
            f"the form of record — the household, declared income and rent come from it, and the current "
            f"certification must be added to the file before the audit is final (Section 11)",
            label="Only the prior certification is in the packet",
            category=CATEGORY_FILE_REVIEW,
            subject_type="document",
            assignment=ASSIGN_CLIENT,
            correction_required="Add the executed current certification form to the packet",
            resolution_type=RESOLVE_PRESENCE,
        ))
    elif not cert_form_present:
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
            # Disability is not asked: the 50059's special-status column is
            # blank unless a code applies, so an all-null read is the normal
            # state, not a gap (reviewer verdict on J-VIV-06676).
            null_student = all(m.student is None for m in household.houseHold)
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
                    # The wording follows what the certification says. A
                    # zero-income worksheet is the remedy only when the
                    # household declared zero; when the certification
                    # declares income, the head's source is unread or sits
                    # under another member, which is an extraction gap.
                    declared_total = None
                    try:
                        if certification_info and certification_info.householdIncome:
                            declared_total = float(str(certification_info.householdIncome).replace(",", ""))
                    except ValueError:
                        declared_total = None
                    who = f"Head of household '{member.FirstName} {member.LastName}'"
                    if declared_total is not None and declared_total <= 0:
                        findings.append(
                            f"{who} has no income records and the certification declares "
                            f"zero household income — zero income worksheet / certification "
                            f"required per Section 9"
                        )
                    elif declared_total:
                        findings.append(
                            f"{who} has no income records but the certification declares "
                            f"${declared_total:,.2f} household income — the head's income "
                            f"source was not read or is attributed to another member; "
                            f"verify (Section 9)"
                        )
                    else:
                        findings.append(
                            f"{who} has no income records and the certification's income "
                            f"total was not read — verify whether a zero income "
                            f"certification is required (Section 9)"
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
    cert_type = (ctx.document_certification_type or ctx.certification_type) if ctx else None
    findings.extend(validate_cert_type_requirements(
        cert_type, document_groups, inventory_hud, household,
        funding_program=ctx.funding_program if ctx else None,
    ))

    # --- 17. Affirmative response cross-reference (Section 11) ---
    findings.extend(validate_affirmative_responses(
        questionnaire_disclosures, document_groups,
    ))
    findings.extend(validate_confirmation_reports(document_groups))

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

    # --- 20. Completeness against the certification's own account ---
    # Runs last: it compares the finished extraction against what the
    # certification says should be in it, which is the check that replaces
    # the MuleSoft reconciliation when Salesforce retires.
    findings.extend(check_completeness(ExtractionResult(
        classification=classification,
        document_groups=document_groups,
        household_demographics=household or HouseholdDemographics(),
        certification_info=certification_info,
        income=income or IncomeExtraction(),
        assets=assets or AssetExtraction(),
        document_inventory_financial=inventory_financial,
        document_inventory_hud=inventory_hud,
        income_calculations=income_calculations or [],
    )))

    # --- 21. Special scenarios (Section 19) ---
    findings.extend(check_special_scenarios(
        household, income, certification_info,
        document_groups, inventory_hud, ctx,
        assets=assets,
    ))

    return findings
