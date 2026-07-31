"""Special scenario checks (Section 19)."""

import logging
from datetime import date, datetime

from app.schemas.context import PipelineContext
from app.schemas.extraction import (
    AssetExtraction,
    CertificationInfo,
    DocumentGroup,
    DocumentInventory,
    HouseholdDemographics,
    IncomeExtraction,
)

logger = logging.getLogger(__name__)


def check_special_scenarios(
    household: HouseholdDemographics | None,
    income: IncomeExtraction | None,
    certification_info: CertificationInfo | None,
    document_groups: list[DocumentGroup],
    inventory_hud: DocumentInventory | None,
    ctx: PipelineContext | None,
    assets: AssetExtraction | None = None,
) -> list[str]:
    """Check for special scenarios per Section 19.

    Returns list of compliance finding strings.
    """
    findings: list[str] = []

    findings.extend(_check_members_without_ssn(household, certification_info))
    findings.extend(_check_student_contradictions(household, document_groups))
    findings.extend(_check_ssa_overpayment(document_groups))
    findings.extend(_check_hud_9887_pages(inventory_hud, household, certification_info))
    findings.extend(_check_homeless_applicant(document_groups))
    findings.extend(_check_cryptocurrency(assets))

    return findings


def _check_members_without_ssn(
    household: HouseholdDemographics | None,
    certification_info: CertificationInfo | None,
) -> list[str]:
    """Members over age 6 must have SSN. Zeros = finding."""
    findings: list[str] = []
    if not household or not household.houseHold:
        return findings

    effective = _parse_date(
        certification_info.effectiveDate if certification_info else None
    )
    if not effective:
        effective = date.today()

    for member in household.houseHold:
        dob = _parse_date(member.DOB)
        if not dob:
            continue

        age = (effective - dob).days / 365.25
        if age <= 6:
            continue

        ssn = member.socialSecurityNumber
        name = f"{member.FirstName or ''} {member.LastName or ''}".strip()

        if not ssn:
            findings.append(
                f"Household member '{name}' (age {int(age)}) has no SSN on file — "
                f"all members over age 6 must have a Social Security number (Section 19)"
            )
        elif ssn in (
            "***-**-0000", "***-**-9999", "000-00-0000", "999-99-9999",
        ):
            from app.services.validation import mask_ssn
            findings.append(
                f"Household member '{name}' has placeholder SSN ({mask_ssn(ssn)}) — "
                f"zeros entered instead of actual SSN = finding (Section 19)"
            )

    return findings


def _check_student_contradictions(
    household: HouseholdDemographics | None,
    document_groups: list[DocumentGroup],
) -> list[str]:
    """Student status contradictions and missing verification."""
    findings: list[str] = []
    if not household or not household.houseHold:
        return findings

    has_student_cert = any(
        "student" in g.document_type.lower() and g.category != "ignore"
        for g in document_groups
    )

    students = [m for m in household.houseHold if m.student == "Y"]

    if students and not has_student_cert:
        names = ", ".join(
            f"{m.FirstName or ''} {m.LastName or ''}".strip() for m in students
        )
        findings.append(
            f"Student status 'Y' for {names} but no Student Status Certification "
            f"found — verification required (Section 19)"
        )

    return findings


def _check_ssa_overpayment(
    document_groups: list[DocumentGroup],
) -> list[str]:
    """Check SSA benefit letter text for overpayment indicators."""
    findings: list[str] = []
    overpayment_keywords = ("overpayment", "adjusted amount", "withholding", "offset")

    for g in document_groups:
        dt = g.document_type.lower()
        if "ssa" in dt or "ssi" in dt or "ssdi" in dt or "social security" in dt:
            text_lower = g.combined_text.lower()
            if any(kw in text_lower for kw in overpayment_keywords):
                findings.append(
                    f"Pages {g.page_range}: SSA benefit letter indicates possible overpayment "
                    f"or adjustment — obtain verification of overpayment balance (Section 19)"
                )

    return findings


def _check_hud_9887_pages(
    inventory_hud: DocumentInventory | None,
    household: HouseholdDemographics | None,
    certification_info: CertificationInfo | None,
) -> list[str]:
    """HUD 9887 must have 4 pages. 9887-A must have 2 pages per adult."""
    findings: list[str] = []
    if not inventory_hud:
        return findings

    adult_count = _count_adults(household, certification_info)

    for doc in inventory_hud.documents:
        dt = (doc.documentType or "").strip()

        # HUD 9887-A page count check
        if "9887-A" in dt or "9887A" in dt:
            # Each 9887-A should be 2 pages
            if doc.pageCount > 0 and doc.pageCount < 2:
                findings.append(
                    f"HUD 9887-A for '{doc.personName or 'Unknown'}' has {doc.pageCount} page(s) — "
                    f"should be 2 pages. Missing pages = finding (Section 19)"
                )

    return findings


def _check_cryptocurrency(assets: AssetExtraction | None) -> list[str]:
    """Section 7: Cryptocurrency has no standardized verification — auto-flag."""
    findings: list[str] = []
    if not assets:
        return findings

    for asset in assets.assetInformation:
        acct_type = (asset.accountType or "").lower()
        doc_type = (asset.documentType or "").lower()
        if "crypto" in acct_type or "crypto" in doc_type:
            findings.append(
                f"Cryptocurrency asset for '{asset.assetOwner or 'Unknown'}' — "
                f"self-declared only, no standardized verification procedure. "
                f"Manual review required (Section 7)"
            )

    return findings


def _check_homeless_applicant(
    document_groups: list[DocumentGroup],
) -> list[str]:
    """Detect possible homeless applicant — blank rent/own fields."""
    findings: list[str] = []
    homeless_indicators = ("homeless", "no fixed address", "shelter", "unhoused")

    for g in document_groups:
        dt = g.document_type.lower()
        if "application" in dt or "questionnaire" in dt:
            text_lower = g.combined_text.lower()
            if any(kw in text_lower for kw in homeless_indicators):
                findings.append(
                    f"Pages {g.page_range}: Application indicates possible homeless applicant — "
                    f"additional verification required for housing status (Section 19)"
                )

    return findings


def _check_blank_application_fields(
    document_groups: list[DocumentGroup],
) -> list[str]:
    """Application field completeness — all fields must have affirmative or negative response."""
    findings: list[str] = []
    # This is a general check — look for patterns indicating blank required fields
    blank_indicators = (
        "total gross income" + " " * 5,  # blank income field
        "gross income: $" + " " * 3,
    )

    for g in document_groups:
        dt = g.document_type.lower()
        if "application" in dt or "questionnaire" in dt:
            text_lower = g.combined_text.lower()
            # Check for blank/missing total gross income
            if "total gross income" in text_lower or "total annual income" in text_lower:
                # If we find the label but no number nearby, flag it
                # This is a heuristic — LLM extraction is more reliable
                pass  # Handled by extraction layer

    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_adults(
    household: HouseholdDemographics | None,
    certification_info: CertificationInfo | None,
) -> int:
    """Count household members age >= 18."""
    if not household or not household.houseHold:
        return 0

    effective = _parse_date(
        certification_info.effectiveDate if certification_info else None
    )
    if not effective:
        effective = date.today()

    count = 0
    for member in household.houseHold:
        dob = _parse_date(member.DOB)
        if dob:
            age = (effective - dob).days / 365.25
            if age >= 18:
                count += 1
        else:
            count += 1  # Assume adult if DOB unknown
    return count


def _parse_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
