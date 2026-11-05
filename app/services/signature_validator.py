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
from app.services.program import is_hud_property

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
    findings.extend(_check_signature_date_agreement(
        certification_info, _certification_date_state(inventory_hud, inventory_financial),
    ))
    findings.extend(_check_signed_after_effective(certification_info))
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

    # The supporting forms (tenant release, student certification, 92006,
    # the 9887 package, the acknowledgement of HUD forms, the model lease,
    # lead paint) carry no signature finding. Their isSigned comes from
    # the OCR text, and a handwritten signature is not in the text, so
    # "could not verify a signature" fired on signed forms; reviewers
    # rejected every one of those notes and confirmed none (the 47-row
    # export of 2026-11-05). Whether such a form is present is checked
    # elsewhere; only the certification form keeps a signature finding.
    # The Initial Notice of Recertification is a letter from management,
    # not a form the household signs (reviewer verdict on J-VIV-06676).

    # --- 5-6. Citizenship Declaration (Section 214) and Race and Ethnic
    # Data Form: one per member. HUD forms; a tax-credit packet owes none.
    is_hud_or_usda = is_hud_property(ctx.funding_program, group_types)
    if is_hud_or_usda and member_count > 0:
        if not by_type.get("Citizenship Declaration"):
            findings.append(
                "Missing required compliance document: Citizenship "
                "Declaration (Section 214) — one per household member "
                "(Section 11)"
            )
        if not by_type.get("HUD Race and Ethnic Data Form"):
            findings.append(
                "Missing required compliance document: Race and Ethnic Data "
                "Form — one per household member (Section 11)"
            )

    # --- 8. HUD 9887: within 18 months of the effective date ---
    # Completeness of the form (its final section present) is asserted on
    # the page text by special_scenarios.
    effective = _parse_date(certification_info.effectiveDate if certification_info else None)
    for doc in by_type.get("HUD 9887", []):
        sig_date = _parse_date(doc.signatureDate)
        if effective and sig_date:
            months_diff = (effective.year - sig_date.year) * 12 + (effective.month - sig_date.month)
            if abs(months_diff) > 18:
                findings.append(
                    f"HUD 9887 signature date {doc.signatureDate} is more than 18 months "
                    f"from effective date {certification_info.effectiveDate} — Section 11"
                )

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


def _certification_date_state(*inventories: DocumentInventory | None) -> str | None:
    """What the inventory saw in the date slot of the certification form:
    "read", "unclear", "blank" or None when it has no such form."""
    from app.services.doc_taxonomy import is_current_certification_form
    for inventory in inventories:
        for doc in (inventory.documents if inventory else []):
            if is_current_certification_form(doc.documentType or ""):
                return getattr(doc, "signatureDateState", None)
    return None


def _check_signature_date_agreement(
    certification_info: CertificationInfo | None,
    date_state: str | None = None,
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
        # Two different things are reported as what they are: a date that
        # is written but could not be read is the engine's to re-read; a
        # blank date slot is the property's undated certification.
        if date_state == "unclear":
            return [make_finding(
                "SIGNATURE_DATE_MISSING",
                "Certification is signed and a date is written beside the "
                "signature, but it could not be read — confirm the date from "
                "the page; a certification must be both signed AND dated "
                "(Section 11)",
                label="Signed certification, date unclear",
                category=CATEGORY_FILE_REVIEW,
                assignment=ASSIGN_INTERNAL,
                correction_required="Read the date beside the signature from the page image",
                resolution_type=RESOLVE_PRESENCE,
            )]
        if date_state == "blank":
            return [make_finding(
                "SIGNATURE_DATE_MISSING",
                "Certification is signed but the date beside the signature is "
                "blank — a certification must be both signed AND dated, and an "
                "undated signature cannot be placed in the certification period "
                "(Section 11)",
                label="Signed certification, undated",
                category=CATEGORY_FILE_REVIEW,
                assignment=ASSIGN_CLIENT,
                correction_required="Obtain a dated signature on the certification",
                resolution_type=RESOLVE_PRESENCE,
            )]
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
