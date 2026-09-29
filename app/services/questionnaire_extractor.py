"""Questionnaire disclosure extractor — LLM extraction of yes/no disclosures (Section 11)."""

import logging

from app.services.doc_taxonomy import confirmed_report_of

from app.core.config import Settings
from app.schemas.extraction import DocumentGroup, Finding, QuestionnaireDisclosures
from app.services.findings import (
    ASSIGN_CLIENT,
    CATEGORY_ASSET,
    CATEGORY_FILE_REVIEW,
    CATEGORY_INCOME,
    RESOLVE_PRESENCE,
    RESOLVE_RECALC,
    make_finding,
)
from app.services.llm_service import call_llm_json

logger = logging.getLogger(__name__)

QUESTIONNAIRE_DISCLOSURE_PROMPT = """\
You are an expert data extractor for HUD/Affordable Housing application forms.

Extract YES/NO disclosures from the provided application, questionnaire, or \
self-certification form. These are questions the applicant answered about their \
income sources, assets, and status.

FIELDS TO EXTRACT (all boolean — true if disclosed/affirmed, false if denied, null if not asked):
- has_employment: Does the applicant report having a job or employment income?
- employers: List of employer names mentioned (empty list if none). Title Case.
- employment: the application's employment section, one object per block that
  names an employer: {"employer": "...", "start_date": "YYYY-MM-DD" or null}.
  start_date is the "Starting Date" / "Date of Hire" the applicant wrote, as
  written (a two-digit year is the 2000s). Empty list when no employer is named.
- has_student_status: Does any household member report being a student?
- has_ssa_benefits: Does the applicant report receiving Social Security (SSA/SSI/SSDI)?
- has_checking_account: Does the applicant report having a checking account?
- has_savings_account: Does the applicant report having a savings account?
- has_child_support: Does the applicant report receiving or paying child support/alimony?
- has_pension: Does the applicant report receiving pension or retirement income?
- has_self_employment: Does the applicant report self-employment income?
- has_other_income: Does the applicant report other income (tips, gifts, TANF, etc.)?
- has_real_estate: Does the applicant report owning real estate?
- has_life_insurance: Does the applicant report having life insurance (whole life)?

RULES:
- Look for checkboxes, yes/no answers, circled responses, listed amounts
- If a dollar amount > 0 is listed next to a source, that counts as "true"
- If a field is left blank or the question wasn't asked, set to null
- "Do you have a checking account? Yes, 1 account, $500" → has_checking_account = true
- "Employment: None" or "N/A" → has_employment = false
- Extract employer names exactly as written, in Title Case

OUTPUT SHAPE:
- Return a single JSON object covering the whole household.
- If multiple applicants are listed, set each boolean to true when ANY \
applicant discloses it (these flags drive household-level verification, \
not per-applicant tracking).
- Do NOT return a top-level JSON list, even if the form has separate \
sections per applicant.

Return ONLY valid JSON matching the schema above."""


# Boolean fields on QuestionnaireDisclosures, used when merging a
# list-shaped LLM response into a single household record.
_QUESTIONNAIRE_BOOL_FIELDS: tuple[str, ...] = (
    "has_employment",
    "has_student_status",
    "has_ssa_benefits",
    "has_checking_account",
    "has_savings_account",
    "has_child_support",
    "has_pension",
    "has_self_employment",
    "has_other_income",
    "has_real_estate",
    "has_life_insurance",
)


def _merge_applicant_disclosures(items: list) -> dict:
    """Merge per-applicant disclosure entries into a single household record.

    Some LLM responses return a JSON list with one entry per applicant
    instead of the single object the schema expects. For each boolean
    field, True wins over False over None — verification triggers
    (validate_affirmative_responses) operate at household scope, so the
    household "has employment" if any applicant disclosed it.
    """
    merged: dict = {}
    for field in _QUESTIONNAIRE_BOOL_FIELDS:
        values = [
            item.get(field) for item in items if isinstance(item, dict)
        ]
        if any(v is True for v in values):
            merged[field] = True
        elif any(v is False for v in values):
            merged[field] = False
        else:
            merged[field] = None
    employers: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for e in (item.get("employers") or []):
            if e and e not in employers:
                employers.append(e)
    merged["employers"] = employers
    employment: list = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for block in (item.get("employment") or []):
            if isinstance(block, dict) and block not in employment:
                employment.append(block)
    merged["employment"] = employment
    return merged


def _coerce_to_disclosures(result) -> QuestionnaireDisclosures:
    """Validate LLM result, tolerating both single-object and list shapes.

    When the LLM returns a top-level list (one entry per applicant), merge
    into a single household record before validation. This avoids silently
    dropping disclosures on multi-member households when the LLM drifts
    off the requested object shape.
    """
    if isinstance(result, list):
        logger.info(
            "Questionnaire LLM returned a list (%d entries) — merging into "
            "household record", len(result),
        )
        result = _merge_applicant_disclosures(result)
    return QuestionnaireDisclosures.model_validate(result)


def extract_questionnaire_disclosures(
    groups: list[DocumentGroup],
    settings: Settings,
) -> QuestionnaireDisclosures | None:
    """Extract yes/no disclosures from application/questionnaire documents.

    Returns QuestionnaireDisclosures or None if no questionnaire found.
    """
    relevant_texts = []
    for g in groups:
        if g.category == "ignore":
            continue
        dt = g.document_type.lower()
        if "application" in dt or "questionnaire" in dt or "self-certification" in dt:
            relevant_texts.append(
                f"[Document: {g.document_type}, Pages: {g.page_range}]\n{g.combined_text}"
            )

    if not relevant_texts:
        logger.info("No questionnaire documents found for disclosure extraction")
        return None

    user_prompt = (
        "Extract yes/no disclosures from these application/questionnaire documents:\n\n"
        + "\n\n---\n\n".join(relevant_texts)
    )

    result = call_llm_json(QUESTIONNAIRE_DISCLOSURE_PROMPT, user_prompt, settings)
    logger.info("Extracted questionnaire disclosures")
    return _coerce_to_disclosures(result)


def _unverified(
    code: str,
    text: str,
    *,
    label: str,
    category: str,
    subject_type: str | None,
    correction: str,
    resolution: str,
) -> Finding:
    """A questionnaire disclosure with nothing in the file to back it.

    The category and subject are what the reviewer's next step keys on: a
    disclosed asset with no record becomes an `asset` finding whose subject
    is an `asset_record`, so the consumer can offer "add an asset record"
    rather than a note to read. The subject has no ref because there is no
    record yet — that absence is the finding — so the key is per case.
    """
    return make_finding(
        code,
        text,
        label=label,
        category=category,
        subject_type=subject_type,
        assignment=ASSIGN_CLIENT,
        correction_required=correction,
        resolution_type=resolution,
    )


def validate_affirmative_responses(
    disclosures: QuestionnaireDisclosures | None,
    document_groups: list[DocumentGroup],
    income_doc_types: set[str] | None = None,
    asset_doc_types: set[str] | None = None,
) -> list[Finding]:
    """Cross-reference disclosures against documents present (Affirmative Response Rule).

    Per Section 11: any affirmative response requires independent verification.

    Each finding is structured with the category of the thing disclosed and
    the kind of record that is missing, so the consumer can turn it into an
    action (add an asset record, add an income record) rather than a note.
    The wording is unchanged from the plain-string form these replaced.
    """
    if not disclosures:
        return []

    findings: list[Finding] = []

    # Build sets of document types present (non-ignore)
    doc_types = {g.document_type for g in document_groups if g.category != "ignore"}
    doc_types_lower = {dt.lower() for dt in doc_types}

    # Employment → verified by an employer verification, a Work Number
    # report or pay stubs. Any one of them is verification; demanding a
    # VOI beside the stubs called a stub-verified job unverified, and the
    # stub count has its own finding when the file holds fewer than three.
    if disclosures.has_employment is True:
        has_voi = any("voi" in dt or "verification of income" in dt or "employment verification" in dt
                      for dt in doc_types_lower)
        has_paystub = any("paystub" in dt or "pay stub" in dt or "pay-slip" in dt for dt in doc_types_lower)
        has_work_number = any("work number" in dt or "equifax" in dt for dt in doc_types_lower)
        if not (has_voi or has_paystub or has_work_number):
            findings.append(_unverified(
                "QUESTIONNAIRE_EMPLOYMENT_UNVERIFIED",
                "Employment disclosed on questionnaire but no Verification of Income (VOI), "
                "pay stubs or Work Number report found — independent verification required (Section 11)",
                label="Employment disclosed with no income verification in the file",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                correction="Add an income record for the disclosed employment: obtain a VOI, "
                           "a Work Number report or pay stubs",
                resolution=RESOLVE_RECALC,
            ))

    # Student status → Student Status Certification required. A file
    # review item, not a record to add: the certification is a document
    # about the household, and its absence changes no figure.
    if disclosures.has_student_status is True:
        has_student_cert = any("student" in dt for dt in doc_types_lower)
        if not has_student_cert:
            findings.append(_unverified(
                "QUESTIONNAIRE_STUDENT_UNVERIFIED",
                "Student status disclosed on questionnaire but no Student Status Certification "
                "found — verification required (Section 11)",
                label="Student status disclosed with no Student Status Certification",
                category=CATEGORY_FILE_REVIEW,
                subject_type=None,
                correction="Obtain a Student Status Certification for each member disclosed as a student",
                resolution=RESOLVE_PRESENCE,
            ))

    # SSA benefits → SSA Benefit Letter required
    # SSA → a benefit letter, or HUD's own EIV income report, which is the
    # third-party verification of Social Security and SSI on a HUD file.
    if disclosures.has_ssa_benefits is True:
        # A benefit letter, HUD's EIV report, or a countersigned sheet that
        # stands in for that report (its `confirms` in the taxonomy).
        has_ssa = any(
            "ssa" in dt.lower() or "ssi" in dt.lower() or "ssdi" in dt.lower()
            or "social security" in dt.lower() or dt.lower() == "eiv income report"
            or (confirmed_report_of(dt) or "").lower() == "eiv income report"
            for dt in doc_types
        )
        if not has_ssa:
            findings.append(_unverified(
                "QUESTIONNAIRE_SSA_UNVERIFIED",
                "SSA/SSI/SSDI benefits disclosed on questionnaire but no benefit letter "
                "or EIV report found — independent verification required (Section 11)",
                label="Social Security benefits disclosed with no benefit letter or EIV report",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                correction="Add an income record for the disclosed Social Security benefit: "
                           "obtain the benefit letter or the EIV income report",
                resolution=RESOLVE_RECALC,
            ))

    # Checking / savings account → a bank statement or VOA, or the household's
    # asset self-certification, which HOTMA lets stand for net assets under
    # the threshold and which the zero-asset rule already accepts.
    has_bank = any("bank statement" in dt or "voa" in dt or "verification of asset" in dt
                   or "asset self-certification" in dt for dt in doc_types_lower)
    if disclosures.has_checking_account is True and not has_bank:
        findings.append(_unverified(
            "QUESTIONNAIRE_CHECKING_UNVERIFIED",
            "Checking account disclosed on questionnaire but no bank statement, VOA or asset "
            "self-certification found — bank verification required (Section 11)",
            label="Checking account disclosed with no bank verification",
            category=CATEGORY_ASSET,
            subject_type="asset_record",
            correction="Add an asset record for the disclosed checking account: obtain a bank "
                       "statement, a VOA or the asset self-certification",
            resolution=RESOLVE_RECALC,
        ))
    if disclosures.has_savings_account is True and not has_bank:
        findings.append(_unverified(
            "QUESTIONNAIRE_SAVINGS_UNVERIFIED",
            "Savings account disclosed on questionnaire but no bank statement, VOA or asset "
            "self-certification found — bank verification required (Section 11)",
            label="Savings account disclosed with no bank verification",
            category=CATEGORY_ASSET,
            subject_type="asset_record",
            correction="Add an asset record for the disclosed savings account: obtain a bank "
                       "statement, a VOA or the asset self-certification",
            resolution=RESOLVE_RECALC,
        ))

    # Child support → verification required
    if disclosures.has_child_support is True:
        has_cs = any("child support" in dt for dt in doc_types_lower)
        if not has_cs:
            findings.append(_unverified(
                "QUESTIONNAIRE_CHILD_SUPPORT_UNVERIFIED",
                "Child support disclosed on questionnaire but no child support verification "
                "found — independent verification required (Section 11)",
                label="Child support disclosed with no verification",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                correction="Add an income record for the disclosed child support: obtain the "
                           "court order, agency printout or payment history",
                resolution=RESOLVE_RECALC,
            ))

    # Pension → pension statement required
    if disclosures.has_pension is True:
        has_pension = any("pension" in dt for dt in doc_types_lower)
        if not has_pension:
            findings.append(_unverified(
                "QUESTIONNAIRE_PENSION_UNVERIFIED",
                "Pension income disclosed on questionnaire but no pension statement "
                "found — independent verification required (Section 11)",
                label="Pension disclosed with no pension statement",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                correction="Add an income record for the disclosed pension: obtain the pension "
                           "or annuity statement",
                resolution=RESOLVE_RECALC,
            ))

    # Real estate → documentation required
    if disclosures.has_real_estate is True:
        has_real_estate = any("real estate" in dt for dt in doc_types_lower)
        if not has_real_estate:
            findings.append(_unverified(
                "QUESTIONNAIRE_REAL_ESTATE_UNVERIFIED",
                "Real estate ownership disclosed on questionnaire but no real estate "
                "documentation found — verification required (Section 11)",
                label="Real estate disclosed with no documentation",
                category=CATEGORY_ASSET,
                subject_type="asset_record",
                correction="Add an asset record for the disclosed real estate: obtain the deed, "
                           "tax assessment or appraisal and any mortgage statement",
                resolution=RESOLVE_RECALC,
            ))

    # Life insurance → verification required
    if disclosures.has_life_insurance is True:
        has_life = any("life insurance" in dt for dt in doc_types_lower)
        if not has_life:
            findings.append(_unverified(
                "QUESTIONNAIRE_LIFE_INSURANCE_UNVERIFIED",
                "Life insurance disclosed on questionnaire but no life insurance "
                "documentation found — verification required (Section 11)",
                label="Life insurance disclosed with no documentation",
                category=CATEGORY_ASSET,
                subject_type="asset_record",
                correction="Add an asset record for the disclosed life insurance: obtain the "
                           "policy statement showing its cash value",
                resolution=RESOLVE_RECALC,
            ))

    return findings
