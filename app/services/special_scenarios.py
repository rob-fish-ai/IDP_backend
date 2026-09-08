"""Special scenario checks (Section 19)."""

import logging
from datetime import date, datetime

from app.schemas.context import PipelineContext
from app.schemas.extraction import (
    AssetExtraction,
    CertificationInfo,
    DocumentGroup,
    DocumentInventory,
    Finding,
    HouseholdDemographics,
    IncomeExtraction,
)
from app.services.findings import (
    ASSIGN_CLIENT,
    ASSIGN_INTERNAL,
    CATEGORY_ASSET,
    CATEGORY_FILE_REVIEW,
    CATEGORY_INCOME,
    CATEGORY_MEMBER,
    RESOLVE_PRESENCE,
    RESOLVE_RECALC,
    make_finding,
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
) -> list[Finding]:
    """Check for special scenarios per Section 19."""
    findings: list[Finding] = []

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
) -> list[Finding]:
    """Members over age 6 must have SSN. Zeros = finding."""
    findings: list[Finding] = []
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
            findings.append(make_finding(
                "MEMBER_SSN_MISSING",
                f"Household member '{name}' (age {int(age)}) has no SSN on file — "
                f"all members over age 6 must have a Social Security number (Section 19)",
                label="Household member over 6 with no SSN",
                category=CATEGORY_MEMBER,
                subject_type="household_member",
                subject_ref={"member_name": name},
                assignment=ASSIGN_CLIENT,
                correction_required=(
                    "Obtain the member's Social Security number, or the "
                    "documentation supporting an exemption"
                ),
                resolution_type=RESOLVE_PRESENCE,
            ))
        elif ssn in (
            "***-**-0000", "***-**-9999", "000-00-0000", "999-99-9999",
        ):
            from app.services.validation import mask_ssn
            findings.append(make_finding(
                "MEMBER_SSN_PLACEHOLDER",
                f"Household member '{name}' has placeholder SSN ({mask_ssn(ssn)}) — "
                f"zeros entered instead of actual SSN = finding (Section 19)",
                label="Household member has a placeholder SSN",
                category=CATEGORY_MEMBER,
                subject_type="household_member",
                subject_ref={"member_name": name},
                assignment=ASSIGN_CLIENT,
                correction_required=(
                    "Replace the placeholder with the member's actual Social "
                    "Security number"
                ),
                resolution_type=RESOLVE_PRESENCE,
            ))

    return findings


def _check_student_contradictions(
    household: HouseholdDemographics | None,
    document_groups: list[DocumentGroup],
) -> list[Finding]:
    """Student status contradictions and missing verification."""
    findings: list[Finding] = []
    if not household or not household.houseHold:
        return findings

    has_student_cert = any(
        "student" in g.document_type.lower() and g.category != "ignore"
        for g in document_groups
    )

    students = [m for m in household.houseHold if m.student == "Y"]
    if not students or has_student_cert:
        return findings

    # One finding per student rather than one naming them all. The old
    # wording joined every name into a single string, which has no subject
    # to key on: the identity would change whenever the roster did, so a
    # reviewer's resolution would not survive a member being added, and
    # nothing could attach the finding to the member it concerns.
    for member in students:
        name = f"{member.FirstName or ''} {member.LastName or ''}".strip()
        findings.append(make_finding(
            "STUDENT_STATUS_UNVERIFIED",
            f"Student status 'Y' for {name} but no Student Status Certification "
            f"found — verification required (Section 19)",
            label="Student status declared with no certification on file",
            category=CATEGORY_MEMBER,
            subject_type="household_member",
            subject_ref={"member_name": name},
            assignment=ASSIGN_CLIENT,
            correction_required=(
                "Obtain a Student Status Certification for this member"
            ),
            resolution_type=RESOLVE_PRESENCE,
        ))

    return findings


def _check_ssa_overpayment(
    document_groups: list[DocumentGroup],
) -> list[Finding]:
    """Check SSA benefit letter text for overpayment indicators."""
    findings: list[Finding] = []
    overpayment_keywords = ("overpayment", "adjusted amount", "withholding", "offset")

    for g in document_groups:
        dt = g.document_type.lower()
        if "ssa" in dt or "ssi" in dt or "ssdi" in dt or "social security" in dt:
            text_lower = g.combined_text.lower()
            if any(kw in text_lower for kw in overpayment_keywords):
                findings.append(make_finding(
                    "SSA_POSSIBLE_OVERPAYMENT",
                    f"Pages {g.page_range}: SSA benefit letter indicates possible overpayment "
                    f"or adjustment — obtain verification of overpayment balance (Section 19)",
                    label="Benefit letter suggests an overpayment or adjustment",
                    category=CATEGORY_INCOME,
                    subject_type="income_record",
                    # Keyed on the document, which is all this check knows —
                    # it reads the letter's text, not the income record it
                    # belongs to. Page range distinguishes two letters in one
                    # packet without inventing a source name.
                    subject_ref={
                        "document_type": g.document_type,
                        "page_range": g.page_range,
                    },
                    assignment=ASSIGN_INTERNAL,
                    correction_required=(
                        "Obtain verification of the overpayment balance and "
                        "recompute the benefit income if it is being withheld"
                    ),
                    # The gross benefit is not the amount received while an
                    # overpayment is recovered, so the figure changes.
                    resolution_type=RESOLVE_RECALC,
                    pages=g.pages,
                ))

    return findings


def _check_hud_9887_pages(
    inventory_hud: DocumentInventory | None,
    household: HouseholdDemographics | None,
    certification_info: CertificationInfo | None,
) -> list[Finding]:
    """9887-A must be 2 pages each.

    Per document, which is the half of the rule this module can check from
    the inventory alone. signature_validator holds the other half — total
    pages against two per adult — and that is why `adult_count` used to be
    computed here and thrown away. See the duplicate-finding note: one
    incomplete 9887-A is currently reported by both modules.
    """
    findings: list[Finding] = []
    if not inventory_hud:
        return findings

    for doc in inventory_hud.documents:
        dt = (doc.documentType or "").strip()

        # HUD 9887-A page count check
        if "9887-A" in dt or "9887A" in dt:
            # Each 9887-A should be 2 pages
            if doc.pageCount > 0 and doc.pageCount < 2:
                findings.append(make_finding(
                    "HUD_9887A_INCOMPLETE",
                    f"HUD 9887-A for '{doc.personName or 'Unknown'}' has {doc.pageCount} page(s) — "
                    f"should be 2 pages. Missing pages = finding (Section 19)",
                    label="HUD 9887-A is missing pages",
                    category=CATEGORY_FILE_REVIEW,
                    subject_type="household_member",
                    subject_ref={"member_name": doc.personName},
                    assignment=ASSIGN_CLIENT,
                    correction_required=(
                        "Obtain the complete two-page HUD 9887-A for this member"
                    ),
                    resolution_type=RESOLVE_PRESENCE,
                ))

    return findings


def _check_cryptocurrency(assets: AssetExtraction | None) -> list[Finding]:
    """Section 7: Cryptocurrency has no standardized verification — auto-flag."""
    findings: list[Finding] = []
    if not assets:
        return findings

    for asset in assets.assetInformation:
        acct_type = (asset.accountType or "").lower()
        doc_type = (asset.documentType or "").lower()
        if "crypto" in acct_type or "crypto" in doc_type:
            findings.append(make_finding(
                "CRYPTO_ASSET_UNVERIFIABLE",
                f"Cryptocurrency asset for '{asset.assetOwner or 'Unknown'}' — "
                f"self-declared only, no standardized verification procedure. "
                f"Manual review required (Section 7)",
                label="Cryptocurrency asset has no standard verification",
                category=CATEGORY_ASSET,
                subject_type="asset_record",
                subject_ref={
                    "member_name": asset.assetOwner,
                    "source_name": asset.sourceName,
                    "account_type": asset.accountType,
                },
                assignment=ASSIGN_INTERNAL,
                correction_required=(
                    "Review the declared holding and decide what evidence the "
                    "program will accept for it"
                ),
                resolution_type=RESOLVE_RECALC,
            ))

    return findings


def _check_homeless_applicant(
    document_groups: list[DocumentGroup],
) -> list[Finding]:
    """Detect possible homeless applicant — blank rent/own fields."""
    findings: list[Finding] = []
    homeless_indicators = ("homeless", "no fixed address", "shelter", "unhoused")

    for g in document_groups:
        dt = g.document_type.lower()
        if "application" in dt or "questionnaire" in dt:
            text_lower = g.combined_text.lower()
            if any(kw in text_lower for kw in homeless_indicators):
                findings.append(make_finding(
                    "HOMELESS_APPLICANT_INDICATED",
                    f"Pages {g.page_range}: Application indicates possible homeless applicant — "
                    f"additional verification required for housing status (Section 19)",
                    label="Application suggests the applicant was homeless",
                    category=CATEGORY_FILE_REVIEW,
                    subject_ref={
                        "document_type": g.document_type,
                        "page_range": g.page_range,
                    },
                    assignment=ASSIGN_INTERNAL,
                    correction_required=(
                        "Verify the housing status at application and attach "
                        "the supporting documentation the program requires"
                    ),
                    resolution_type=RESOLVE_PRESENCE,
                    pages=g.pages,
                ))

    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
