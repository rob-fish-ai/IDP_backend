"""Known IDP bug detection (Section 17)."""

import logging

from app.schemas.extraction import (
    ClassificationResult,
    DocumentGroup,
    IncomeExtraction,
)

logger = logging.getLogger(__name__)


def _ssn_last4(ssn: str | None) -> str | None:
    """Last 4 SSN digits regardless of form (full, masked, partial)."""
    if not ssn:
        return None
    digits = [c for c in str(ssn) if c.isdigit()]
    return "".join(digits[-4:]) if len(digits) >= 4 else None


def detect_known_bugs(
    classification: ClassificationResult,
    document_groups: list[DocumentGroup],
    income: IncomeExtraction | None,
    household=None,
    certification_info=None,
) -> list[str]:
    """Detect known IDP bugs from Section 17 + Salesforce validation rules.

    Returns list of finding strings for detected issues.
    """
    findings: list[str] = []

    if income:
        findings.extend(_check_ssa_as_paystub_and_voi(income))
        findings.extend(_check_ssa_ytd(income))
        findings.extend(_check_calc_worksheet_as_voi(income))
        findings.extend(_check_duplicate_employers(income))
        findings.extend(_check_fixed_income_paystubs(income))
        findings.extend(_check_erroneous_paystub_amounts(income))

    if household:
        findings.extend(_check_duplicate_members(household))

    if certification_info:
        findings.extend(_check_arsc_source_of_truth(certification_info, income))

    return findings


def _check_ssa_as_paystub_and_voi(income: IncomeExtraction) -> list[str]:
    """Bug 1: SSA Benefit Letter imported as both paystub AND VOI.

    Correct: Only VOI should exist for SSA income.
    """
    findings: list[str] = []

    ssa_types = {"social security", "supplemental security income", "social security disability"}

    # Find SSA in verification income
    ssa_vi_sources: set[str] = set()
    for vi in income.sourceIncome.verificationIncome:
        it = (vi.incomeType or "").lower()
        if it in ssa_types:
            ssa_vi_sources.add((vi.memberName or "").lower())

    # Check if any paystub matches SSA member
    for ps in income.sourceIncome.payStub:
        source = (ps.sourceName or "").lower()
        member = (ps.memberName or "").lower()
        is_ssa_source = any(kw in source for kw in ("ssa", "social security", "ssi", "ssdi"))

        if is_ssa_source or (member in ssa_vi_sources and member):
            findings.append(
                f"Bug 1 (Section 17): SSA income for '{ps.memberName}' appears as both "
                f"a paystub and VOI record — delete the paystub entry. "
                f"SSA should only be recorded as VOI with monthly rate of pay"
            )

    return findings


def _check_ssa_ytd(income: IncomeExtraction) -> list[str]:
    """Bug 2: SSA Benefit Letter with YTD amount.

    Correct: SSA should NOT have YTD. (Validation already clears it, but flag for awareness.)
    """
    findings: list[str] = []

    ssa_types = {"social security", "supplemental security income", "social security disability"}

    for vi in income.sourceIncome.verificationIncome:
        it = (vi.incomeType or "").lower()
        # Note: validation.py already nulls ytdAmount for SSA, so this catches
        # cases where the LLM extracted it before validation cleared it.
        # We check the type to flag it regardless.
        if it in ssa_types and vi.ytdAmount:
            findings.append(
                f"Bug 2 (Section 17): SSA benefit for '{vi.memberName}' has YTD amount "
                f"${vi.ytdAmount} — SSA benefit letters should NOT have YTD. Amount will be cleared"
            )

    return findings


def _check_calc_worksheet_as_voi(income: IncomeExtraction) -> list[str]:
    """Bug 4: Income Calculation Worksheet misclassified as VOI."""
    findings: list[str] = []

    calc_keywords = ("calculation", "worksheet", "calc sheet", "pcap", "cf-51", "lihtc calc")

    for vi in income.sourceIncome.verificationIncome:
        source = (vi.sourceName or "").lower()
        if any(kw in source for kw in calc_keywords):
            findings.append(
                f"Bug 4 (Section 17): Income calculation worksheet detected as VOI "
                f"(source: '{vi.sourceName}') — this record should be deleted. "
                f"Calculation worksheets must always be ignored"
            )

    return findings


def _check_duplicate_employers(income: IncomeExtraction) -> list[str]:
    """Bug 10: Duplicate employer records due to OCR/spelling variations."""
    findings: list[str] = []

    # Group VI entries by member
    by_member: dict[str, list[tuple[int, str]]] = {}
    for i, vi in enumerate(income.sourceIncome.verificationIncome):
        member = (vi.memberName or "Unknown").lower()
        source = (vi.sourceName or "").strip()
        if source:
            by_member.setdefault(member, []).append((i, source))

    for member, sources in by_member.items():
        if len(sources) < 2:
            continue

        # Check for similar source names
        for i in range(len(sources)):
            for j in range(i + 1, len(sources)):
                idx_a, name_a = sources[i]
                idx_b, name_b = sources[j]
                if _is_similar_employer(name_a, name_b):
                    findings.append(
                        f"Bug 10 (Section 17): Possible duplicate employer for '{member}': "
                        f"'{name_a}' and '{name_b}' — keep the record with more pay stubs, "
                        f"delete the other, verify completeness"
                    )

    return findings


def _check_fixed_income_paystubs(income: IncomeExtraction) -> list[str]:
    """IDP creates paystubs for fixed income (SSA, pension) — these should be deleted.

    Per Salesforce rules: "IDP will create Paystubs for fixed income types like
    Social Security - these paystubs just need to be deleted, these will also
    commonly be paired with odd values that greatly inflate income, like $30k a month"
    """
    findings: list[str] = []
    fixed_keywords = ("social security", "ssa", "ssi", "ssdi", "pension", "retirement",
                      "disability", "veteran", "tanf", "public assistance")

    for ps in income.sourceIncome.payStub:
        source = (ps.sourceName or "").lower()
        if any(kw in source for kw in fixed_keywords):
            gross = None
            try:
                gross = float(ps.grossPay) if ps.grossPay else None
            except ValueError:
                pass
            note = ""
            if gross and gross > 10000:
                note = f" — grossPay ${gross:,.2f} appears inflated"
            findings.append(
                f"Fixed income paystub detected: '{ps.sourceName}' for '{ps.memberName}'{note} — "
                f"delete this paystub. Fixed income should only be a VOI record with monthly amount x 12"
            )

    return findings


def _check_erroneous_paystub_amounts(income: IncomeExtraction) -> list[str]:
    """Check for paystub amounts that look like they came from elsewhere in the file.

    Per Salesforce rules: "Check that an amount stated elsewhere in the file is not
    erroneously entered as a paystub"
    """
    findings: list[str] = []

    # Collect all known non-paystub amounts
    known_amounts: set[str] = set()
    for vi in income.sourceIncome.verificationIncome:
        for field in (vi.ytdAmount, vi.selfDeclaredAmount):
            if field:
                known_amounts.add(field)

    for ps in income.sourceIncome.payStub:
        if ps.grossPay and ps.grossPay in known_amounts:
            findings.append(
                f"Paystub gross pay ${ps.grossPay} for '{ps.memberName}' matches a YTD or "
                f"self-declared amount — verify this is not erroneously entered from another field"
            )

    return findings


def _check_duplicate_members(household) -> list[str]:
    """Detect duplicate household members with variant names but same DOB/SSN.

    Per Salesforce rules: "IDP often creates duplicates where household members
    have variant names, like Marie Jones and Marie Ann Jones, both with the same
    DOB and Same SSN"
    """
    findings: list[str] = []
    members = household.houseHold if household else []
    if len(members) < 2:
        return findings

    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            a, b = members[i], members[j]
            a_name = f"{a.FirstName or ''} {a.LastName or ''}".strip()
            b_name = f"{b.FirstName or ''} {b.LastName or ''}".strip()

            if not a_name or not b_name:
                continue

            # Same DOB
            same_dob = a.DOB and b.DOB and a.DOB == b.DOB
            # Same SSN last 4 — compare digits, not raw strings: the same
            # person can be captured full from one document and masked
            # from another ("530-38-7514" vs "***-**-7514").
            a_ssn4 = _ssn_last4(a.socialSecurityNumber)
            b_ssn4 = _ssn_last4(b.socialSecurityNumber)
            same_ssn = bool(a_ssn4 and a_ssn4 == b_ssn4 and a_ssn4 != "0000")
            # Similar name (one contains the other, or share last name + first initial)
            a_lower = a_name.lower()
            b_lower = b_name.lower()
            similar_name = (a_lower in b_lower or b_lower in a_lower
                           or (a.LastName and b.LastName
                               and a.LastName.lower() == b.LastName.lower()
                               and a.FirstName and b.FirstName
                               and a.FirstName[0] == b.FirstName[0]))

            if same_dob and same_ssn:
                findings.append(
                    f"Duplicate household member: '{a_name}' and '{b_name}' have same "
                    f"DOB ({a.DOB}) and SSN ({a.socialSecurityNumber}) — delete one and "
                    f"reassign any orphaned child records (income, assets) to the remaining member"
                )
            elif same_dob and similar_name:
                findings.append(
                    f"Possible duplicate member: '{a_name}' and '{b_name}' have same "
                    f"DOB ({a.DOB}) with similar names — verify and merge if duplicate"
                )
            elif same_ssn and similar_name:
                findings.append(
                    f"Possible duplicate member: '{a_name}' and '{b_name}' have same "
                    f"SSN ({a.socialSecurityNumber}) with similar names — verify and merge if duplicate"
                )

    return findings


def _check_arsc_source_of_truth(certification_info, income) -> list[str]:
    """AR-SC cert type: TIC form IS the source of truth.

    Per Salesforce rules: "If the certification type = AR-SC, this indicates that
    the form is a 'self cert' in which case, the TIC form is the source of truth
    and the values on assets and income can be included as the self-declared value"
    """
    findings: list[str] = []
    cert_type = (certification_info.certificationType or "").upper()

    if cert_type == "AR-SC":
        findings.append(
            "AR-SC certification detected — TIC form is the source of truth for this file. "
            "Income and asset values from the TIC should be used as self-declared amounts. "
            "Independent verification documents may not be present."
        )
        # Check if income records have selfDeclaredSource set
        if income:
            for vi in income.sourceIncome.verificationIncome:
                if vi.selfDeclaredAmount and not vi.selfDeclaredSource:
                    vi.selfDeclaredSource = "Self-Certification TIC"

    return findings


def _is_similar_employer(a: str, b: str) -> bool:
    """Check if two employer names are likely the same (OCR/spelling variation)."""
    a_lower = a.lower().strip()
    b_lower = b.lower().strip()

    if a_lower == b_lower:
        return False  # Exact match = same record, not a duplicate issue

    # One contains the other
    if a_lower in b_lower or b_lower in a_lower:
        return True

    # Token overlap >= 60%
    tokens_a = set(a_lower.split())
    tokens_b = set(b_lower.split())
    if not tokens_a or not tokens_b:
        return False

    overlap = len(tokens_a & tokens_b)
    ratio = overlap / min(len(tokens_a), len(tokens_b))
    return ratio >= 0.6
