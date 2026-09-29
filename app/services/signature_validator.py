"""Signature and compliance validation — per-form checks (Section 11)."""

import logging
from datetime import date, datetime

from app.schemas.context import PipelineContext
from app.schemas.extraction import (
    CertificationInfo,
    DocumentGroup,
    DocumentInventory,
    Finding,
    HouseholdDemographics,
)
from app.services.findings import (
    ASSIGN_CLIENT,
    ASSIGN_INTERNAL,
    CATEGORY_FILE_REVIEW,
    RESOLVE_PRESENCE,
    make_finding,
)
from app.services.members import is_unborn

logger = logging.getLogger(__name__)


def validate_signatures(
    inventory_hud: DocumentInventory,
    inventory_financial: DocumentInventory,
    household: HouseholdDemographics,
    certification_info: CertificationInfo | None,
    document_groups: list[DocumentGroup],
    ctx: PipelineContext,
) -> list:
    """Check signature requirements for all forms per Section 11.

    Returns a mixed list: the signature/date agreement check emits structured
    Findings, the rest of this module still emits plain strings pending its
    migration. Both forms are handled by findings.render / findings.records.
    """
    findings: list = []
    findings.extend(_check_signature_date_agreement(certification_info))
    findings.extend(_check_signed_after_effective(certification_info))
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

    # --- 1-2. Certification form: signed and dated ---
    # One finding per form per defect. When the certification is already
    # known to be unsigned the pipeline reports that outright; a second
    # "could not verify a signature" note for the same form would
    # contradict it.
    if not (certification_info and certification_info.isSigned == "No"):
        _check_signed_dated(by_type, "Tenant Income Certification (TIC)",
                            findings, "TIC must be signed and dated on both pages (Section 11)")
        _check_signed_dated(by_type, "HUD 50059",
                            findings, "HUD 50059 must be signed and dated (Section 11)")

    # --- 3. Tenant Release and Consent: signed by all adults ---
    _check_all_adults_signed(by_type, "Tenant Release and Consent Form",
                             adult_count, findings,
                             "Tenant Release and Consent Form must be signed by all adult members (Section 11)")

    # --- 4. Student Status Certification: signed and dated ---
    _check_signed_dated(by_type, "Student Status Certification",
                        findings, "Student Status Certification must be signed and dated (Section 11)")

    # --- 5. Citizenship Declaration (Section 214): one per member, signed, dated ---
    # Only required for HUD/USDA properties
    funding = (ctx.funding_program or "").lower()
    is_hud_or_usda = any(p in funding for p in ("hud", "section", "usda")) or _has_hud_50059(group_types)
    if is_hud_or_usda:
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
    # A HUD form (HUD-27061-H); a tax-credit packet owes none, and this
    # rule fired on every LIHTC household until it was gated like the
    # citizenship rule above it.
    race_docs = by_type.get("HUD Race and Ethnic Data Form", [])
    if not is_hud_or_usda:
        pass
    elif member_count > 0 and not race_docs:
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
    _check_signed_dated(by_type, "HUD 92006",
                        findings, "HUD 92006 (Emergency Contact) must be completed, signed, and dated (Section 11)")

    # --- 8. HUD 9887: all adults sign, within 18 months ---
    # Completeness of the form (its final section present) is asserted on
    # the page text by special_scenarios; page counts said nothing once the
    # classifier split the 9887 package into cover, fact sheet, 9887 and
    # 9887-A groups.
    hud_9887_docs = by_type.get("HUD 9887", [])
    if hud_9887_docs:
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
            findings.append(_unverified(
                "HUD 9887 must be signed by all adult household members (Section 11)"
            ))

    # --- 9. HUD 9887-A: signed ---
    hud_9887a_docs = by_type.get("HUD 9887-A", [])
    if hud_9887a_docs:
        unsigned = [d for d in hud_9887a_docs if d.isSigned == "No"]
        if unsigned:
            findings.append(_unverified(
                f"HUD 9887-A must be signed by tenant and owner on each form "
                f"({len(unsigned)} form(s) show no signature in the text) (Section 11)"
            ))

    # --- 10. Acknowledgement of Receipt: signed by all adults ---
    _check_all_adults_signed(by_type, "Acknowledgement of Receipt",
                             adult_count, findings,
                             "Acknowledgement of Receipt of HUD Forms must be signed by all adult members (Section 11)")

    # (The Initial Notice of Recertification is a letter from management,
    # not a form the household signs; it carries no signature requirement.
    # Reviewer verdict on J-VIV-06676.)

    # --- 12. HUD Model Lease ---
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
            findings.append(_unverified(message))
            return  # One finding per doc type is enough


def _unverified(message: str) -> str:
    """The requirement, stated with what the text can prove.

    isSigned comes from the OCR text, and handwritten signatures are not
    machine-readable: "No" means no signature could be found in the text,
    not that the form is unsigned. The finding keeps the requirement and
    asks for a visual check instead of asserting a failure."""
    base = message.replace(" (Section 11)", "").rstrip(".")
    return (f"{base}; a signature could not be verified from the document text "
            f"(handwritten signatures are not machine-readable) — verify visually (Section 11)")


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
        findings.append(_unverified(message))


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
        if is_unborn(member):
            continue
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


# A certification is executed on or before its effective date; programs
# allow a few days for signatures to be collected. Beyond this many days
# the household certified figures for a period that was already running.
SIGNATURE_LAG_DAYS = 14
# How far ahead of the effective date a signature can plausibly be dated.
SIGNATURE_LEAD_DAYS_MAX = 365


def _check_signed_after_effective(
    certification_info: CertificationInfo | None,
) -> list[Finding]:
    """A certification signed well after its effective date.

    Two move-in certifications effective 05/29 were signed on 09/10; the
    household lived in the unit for three and a half months on figures no
    one had certified. The date pair is read from the form, so the finding
    states the gap and leaves the program's tolerance to the reviewer.
    """
    if certification_info is None:
        return []
    signed = _parse_date(certification_info.signatureDate)
    effective = _parse_date(certification_info.effectiveDate)
    if not signed or not effective:
        return []
    lag = (signed - effective).days
    if lag < -SIGNATURE_LEAD_DAYS_MAX:
        # A certification is not executed a year or more before it takes
        # effect. A read of 2024-08-28 on a form effective 2026-05-29 is a
        # misread digit, and the date stays on the record only as doubted.
        return [make_finding(
            "CERT_SIGNATURE_DATE_IMPLAUSIBLE",
            f"Certification effective {effective.isoformat()} carries a signature date of "
            f"{signed.isoformat()}, {-lag} days earlier — a certification is not signed that far "
            f"ahead of its effective date; the date is probably misread, confirm it on the form (Section 11)",
            label="Signature date long before the effective date",
            category=CATEGORY_FILE_REVIEW,
            subject_type="certification",
            subject_ref={"field": "signatureDate"},
            result="na",
            assignment=ASSIGN_INTERNAL,
            correction_required="Read the signature date from the form's signature block",
            resolution_type=RESOLVE_PRESENCE,
        )]
    if lag <= SIGNATURE_LAG_DAYS:
        return []
    return [make_finding(
        "CERT_SIGNED_AFTER_EFFECTIVE_DATE",
        f"Certification effective {effective.isoformat()} was signed on {signed.isoformat()}, "
        f"{lag} days later — the household certified its income after the certification "
        f"period began; confirm the dates and whether the program permits the delay (Section 11)",
        label="Certification signed after its effective date",
        category=CATEGORY_FILE_REVIEW,
        subject_type="certification",
        subject_ref={"field": "signatureDate"},
        assignment=ASSIGN_CLIENT,
        correction_required="Confirm the signature and effective dates; obtain a timely-signed certification if the program requires one",
        resolution_type=RESOLVE_PRESENCE,
    )]


def _check_signature_date_agreement(
    certification_info: CertificationInfo | None,
) -> list[Finding]:
    """The signature verdict and its date have to describe the same document.

    OCR cannot see handwriting, so the verdict comes from a vision check while
    the date is read as text — two different reads that can disagree, and
    nothing was comparing them.

    Both directions have been seen on real packets. One certification came
    back isSigned=No carrying signatureDate 2026-08-12, a date printed on the
    applicant certification, the move-in application and the VAWA
    acknowledgement filed beside it — a date lifted off a neighbouring
    document and attached to a form whose signature block is blank. Another
    came back isSigned=Yes with no date at all.

    Neither is a statement about the household. Both say the engine's two
    reads of the same signature block do not agree, and a reviewer deciding
    whether to demand a resubmission needs to know that before acting on
    either value.
    """
    if certification_info is None:
        return []

    signed = (certification_info.isSigned or "").strip().lower()
    signed_date = (certification_info.signatureDate or "").strip()

    if signed == "no" and signed_date:
        return [make_finding(
            "SIGNATURE_VERDICT_CONFLICTS_WITH_DATE",
            f"Certification is recorded as NOT signed but carries a signature "
            f"date of {signed_date} — the date may have been read from another "
            f"document in the packet. Confirm against the certification's own "
            f"signature block before requiring a resubmission (Section 11)",
            label="Unsigned certification carries a signature date",
            category=CATEGORY_FILE_REVIEW,
            subject_ref={"field": "isSigned"},
            assignment=ASSIGN_INTERNAL,
            correction_required=(
                "Check the certification's signature block directly and correct "
                "whichever of the two readings is wrong"
            ),
            resolution_type=RESOLVE_PRESENCE,
        )]

    if signed == "yes" and not signed_date:
        return [make_finding(
            "SIGNATURE_DATE_MISSING",
            "Certification is recorded as signed but no signature date was "
            "found — a certification must be both signed AND dated, and an "
            "undated signature cannot be placed in the certification period "
            "(Section 11)",
            label="Signed certification with no date",
            category=CATEGORY_FILE_REVIEW,
            assignment=ASSIGN_INTERNAL,
            correction_required=(
                "Read the date beside the signature, or obtain a dated "
                "certification if the signature is genuinely undated"
            ),
            resolution_type=RESOLVE_PRESENCE,
        )]

    return []
