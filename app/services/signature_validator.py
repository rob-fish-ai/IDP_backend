"""Signature and compliance validation — per-form checks (Section 11)."""

import logging
from datetime import date, datetime

from app.schemas.context import PipelineContext
from app.schemas.extraction import (
    CertificationInfo,
    DocumentGroup,
    DocumentInventory,
    HouseholdDemographics,
)

logger = logging.getLogger(__name__)


def validate_signatures(
    inventory_hud: DocumentInventory,
    inventory_financial: DocumentInventory,
    household: HouseholdDemographics,
    certification_info: CertificationInfo | None,
    document_groups: list[DocumentGroup],
    ctx: PipelineContext,
) -> list[str]:
    """Check signature requirements for all forms per Section 11.

    Returns a list of compliance finding strings.
    """
    findings: list[str] = []
    adult_count = _count_adults(household, certification_info)
    member_count = len(household.houseHold) if household else 0

    all_docs = (
        (inventory_financial.documents if inventory_financial else [])
        + (inventory_hud.documents if inventory_hud else [])
    )

    # Build lookup: doc_type -> list of inventory entries
    by_type: dict[str, list] = {}
    for doc in all_docs:
        dt = (doc.documentType or "").strip()
        by_type.setdefault(dt, []).append(doc)

    # Also check doc_groups for doc types present
    group_types = {g.document_type for g in document_groups}

    # --- 1. TIC: signed and dated ---
    _check_signed_dated(by_type, "Tenant Income Certification (TIC)",
                        findings, "TIC must be signed and dated on both pages (Section 11)")

    # --- 2. HUD 50059: signed and dated ---
    _check_signed_dated(by_type, "HUD 50059",
                        findings, "HUD 50059 must be signed and dated (Section 11)")

    # --- 3. Tenant Release and Consent: signed by all adults ---
    _check_all_adults_signed(by_type, "Tenant Release and Consent",
                             adult_count, findings,
                             "Tenant Release and Consent Form must be signed by all adult members (Section 11)")

    # --- 4. Student Status Certification: signed and dated ---
    _check_signed_dated(by_type, "Student Status Affidavit / Certification",
                        findings, "Student Status Certification must be signed and dated (Section 11)")
    # Also check under alternate name
    _check_signed_dated(by_type, "Student Status Certification",
                        findings, "Student Status Certification must be signed and dated (Section 11)")

    # --- 5. Citizenship Declaration (Section 214): one per member, signed, dated ---
    # Only required for HUD/USDA properties
    funding = (ctx.funding_program or "").lower()
    is_hud_or_usda = any(p in funding for p in ("hud", "section", "usda")) or _has_hud_50059(group_types)
    if is_hud_or_usda:
        cit_docs = by_type.get("Citizenship Declaration (Section 214)", [])
        if not cit_docs:
            # Also check alternate names
            cit_docs = by_type.get("Citizenship Declaration", [])
        # Form absent entirely = hard compliance gap. Form present but
        # "unsigned" = unverifiable, not proven-missing: handwritten
        # signatures never survive OCR, so text-level signed-counts fired
        # on 99% of cases with zero correlation to reviewer rejections.
        if member_count > 0 and not cit_docs:
            findings.append(
                "Missing required compliance document: Citizenship "
                "Declaration (Section 214) — one per household member "
                "(Section 11)"
            )
        elif member_count > 0 and sum(1 for d in cit_docs if d.isSigned == "Yes") < member_count:
            findings.append(
                "Citizenship Declaration (Section 214) present but "
                "signatures could not be verified from document text "
                "(handwritten signatures are not machine-readable) — "
                "verify signatures visually (Section 11)"
            )

    # --- 6. Race and Ethnic Data Form: one per member, signed, dated ---
    race_docs = by_type.get("Race and Ethnic Data Reporting Form", [])
    if not race_docs:
        race_docs = by_type.get("Race and Ethnic Data Form", [])
    if member_count > 0 and not race_docs:
        findings.append(
            "Missing required compliance document: Race and Ethnic Data "
            "Form — one per household member (Section 11)"
        )
    elif member_count > 0 and sum(1 for d in race_docs if d.isSigned == "Yes") < member_count:
        findings.append(
            "Race and Ethnic Data Form present but signatures could not "
            "be verified from document text (handwritten signatures are "
            "not machine-readable) — verify signatures visually (Section 11)"
        )

    # --- 7. HUD 92006: completed, signed, dated ---
    _check_signed_dated(by_type, "HUD-92006",
                        findings, "HUD 92006 (Emergency Contact) must be completed, signed, and dated (Section 11)")
    _check_signed_dated(by_type, "HUD 92006",
                        findings, "HUD 92006 (Emergency Contact) must be completed, signed, and dated (Section 11)")

    # --- 8. HUD 9887: 4 pages, all adults sign, within 18 months ---
    hud_9887_docs = by_type.get("HUD-9887", []) or by_type.get("HUD 9887", [])
    if hud_9887_docs:
        total_pages = sum(d.pageCount for d in hud_9887_docs)
        if total_pages < 4:
            findings.append(
                f"HUD 9887 has {total_pages} page(s) — should be 4 pages. "
                f"Missing pages = finding (Section 19)"
            )

        # Check 18-month rule
        effective = _parse_date(
            certification_info.effectiveDate if certification_info else None
        )
        for doc in hud_9887_docs:
            sig_date = _parse_date(doc.signatureDate)
            if effective and sig_date:
                months_diff = (effective.year - sig_date.year) * 12 + (effective.month - sig_date.month)
                if abs(months_diff) > 18:
                    findings.append(
                        f"HUD 9887 signature date {doc.signatureDate} is more than 18 months "
                        f"from effective date {certification_info.effectiveDate} — Section 11"
                    )

        signed_count = sum(1 for d in hud_9887_docs if d.isSigned == "Yes")
        if adult_count > 0 and signed_count < 1:
            findings.append(
                "HUD 9887 must be signed by all adult household members (Section 11)"
            )

    # --- 9. HUD 9887-A: 2 pages per adult, signed ---
    hud_9887a_docs = by_type.get("HUD-9887-A", []) or by_type.get("HUD 9887-A", [])
    if hud_9887a_docs:
        total_pages = sum(d.pageCount for d in hud_9887a_docs)
        expected_pages = adult_count * 2
        if expected_pages > 0 and total_pages < expected_pages:
            findings.append(
                f"HUD 9887-A has {total_pages} page(s) — expected {expected_pages} "
                f"(2 pages per adult × {adult_count} adults). Missing pages = finding (Section 19)"
            )
        unsigned = [d for d in hud_9887a_docs if d.isSigned == "No"]
        if unsigned:
            findings.append(
                f"HUD 9887-A: {len(unsigned)} unsigned form(s) — "
                f"each must be signed by tenant and owner (Section 11)"
            )

    # --- 10. Acknowledgement of Receipt: signed by all adults ---
    _check_all_adults_signed(by_type, "Acknowledgement of Receipt of HUD Forms",
                             adult_count, findings,
                             "Acknowledgement of Receipt of HUD Forms must be signed by all adult members (Section 11)")

    # --- 11. Initial Notice of Recertification: signed, dated, witnessed ---
    initial_docs = by_type.get("Initial Notice of Recertification", [])
    for doc in initial_docs:
        if doc.isSigned == "No":
            findings.append(
                "Initial Notice of Recertification must be signed, dated, and witnessed "
                "by all adult members (Section 11)"
            )
            break

    # --- 12. HUD Model Lease ---
    _check_signed_dated(by_type, "HUD Model Lease (Signature Page)",
                        findings, "HUD Model Lease must be signed and dated (Section 11)")
    _check_signed_dated(by_type, "HUD Model Lease",
                        findings, "HUD Model Lease must be signed and dated (Section 11)")

    # --- 13. Lead-Based Paint ---
    _check_signed_dated(by_type, "Lead-Based Paint Certification",
                        findings, "Lead-Based Paint Certification must be signed (Section 11)")

    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_signed_dated(
    by_type: dict[str, list],
    doc_type: str,
    findings: list[str],
    message: str,
) -> None:
    """Add finding if document exists but is not signed."""
    docs = by_type.get(doc_type, [])
    for doc in docs:
        if doc.isSigned == "No":
            findings.append(message)
            return  # One finding per doc type is enough


def _check_all_adults_signed(
    by_type: dict[str, list],
    doc_type: str,
    adult_count: int,
    findings: list[str],
    message: str,
) -> None:
    """Add finding if signed count < adult count."""
    docs = by_type.get(doc_type, [])
    signed_count = sum(1 for d in docs if d.isSigned == "Yes")
    if adult_count > 0 and docs and signed_count < adult_count:
        findings.append(message)


def _count_adults(
    household: HouseholdDemographics | None,
    certification_info: CertificationInfo | None,
) -> int:
    """Count household members age >= 18 as of effective date."""
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
            # If no DOB, assume adult (conservative)
            count += 1

    return count


def _has_hud_50059(group_types: set[str]) -> bool:
    """Check if HUD 50059 exists in document groups."""
    return any("HUD 50059" in t for t in group_types if "(Previous)" not in t)


def _parse_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD date string."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
