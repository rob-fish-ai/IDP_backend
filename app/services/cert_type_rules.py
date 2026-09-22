"""Certification type-specific processing rules (Section 12)."""

import logging

from app.schemas.extraction import (
    DocumentGroup,
    DocumentInventory,
    Finding,
    HouseholdDemographics,
)
from app.services.doc_taxonomy import is_current_certification_form
from app.services.findings import (
    ASSIGN_CLIENT,
    CATEGORY_FILE_REVIEW,
    RESOLVE_PRESENCE,
    make_finding,
)

logger = logging.getLogger(__name__)


def validate_cert_type_requirements(
    cert_type: str | None,
    document_groups: list[DocumentGroup],
    inventory_hud: DocumentInventory | None,
    household: HouseholdDemographics | None,
    funding_program: str | None = None,
) -> list[Finding]:
    """Check certification-type-specific document requirements per Section 12.

    Every finding here is "a document the certification type requires is not
    in the packet", so they share a shape: the file-review category, the
    client to act on it, and presence of the document as the resolution.
    """
    if not cert_type:
        return []

    findings: list[Finding] = []
    doc_types = {g.document_type for g in document_groups if g.category != "ignore"}
    member_count = len(household.houseHold) if household and household.houseHold else 0

    hud_doc_types = set()
    if inventory_hud:
        for doc in inventory_hud.documents:
            hud_doc_types.add(doc.documentType or "")

    ct = cert_type.upper()

    # The move-in forms below the application are HUD's (citizenship,
    # race/ethnic, 92006, the TRACS summaries); a tax-credit move-in owes
    # none of them. HUD is shown by a current 50059 in the packet or by the
    # caller's funding program.
    funding = (funding_program or "").lower()
    hud_property = any(p in funding for p in ("hud", "section", "usda", "rad", "public housing")) or any(
        "HUD 50059" in dt and "(Previous)" not in dt for dt in (doc_types | hud_doc_types)
    )

    if ct in ("MI", "IC"):
        findings.extend(_check_mi_requirements(doc_types, hud_doc_types, member_count, hud_property))
    elif ct == "AR":
        findings.extend(_check_ar_requirements(doc_types, document_groups))
    elif ct == "AR-SC":
        findings.extend(_check_arsc_requirements(document_groups))
    elif ct == "IR":
        findings.extend(_check_ir_requirements())

    return findings


def _missing_document(code: str, label: str, text: str, obtain: str) -> Finding:
    """One required document absent from the packet.

    Subject is the case rather than a member: the finding says the packet
    lacks a document, and a per-member key would mint a separate finding for
    every member of a household that is missing one form.
    """
    return make_finding(
        code,
        text,
        label=label,
        category=CATEGORY_FILE_REVIEW,
        assignment=ASSIGN_CLIENT,
        correction_required=obtain,
        resolution_type=RESOLVE_PRESENCE,
    )


def _check_mi_requirements(
    doc_types: set[str],
    hud_doc_types: set[str],
    member_count: int,
    hud_property: bool = True,
) -> list[Finding]:
    """Move-In / Initial Certification requires additional documents."""
    findings: list[Finding] = []
    all_types = doc_types | hud_doc_types
    if not hud_property:
        return _check_mi_application(all_types)

    # Citizenship Declaration (Section 214) — one per member
    has_citizenship = any("Citizenship" in dt or "Section 214" in dt for dt in all_types)
    if not has_citizenship:
        findings.append(_missing_document(
            "MI_CITIZENSHIP_DECLARATION_MISSING",
            "Move-in packet has no Citizenship Declaration",
            "MI/IC certification requires Citizenship Declaration (Section 214) "
            "for each household member — not found (Section 12)",
            "Obtain a signed Citizenship Declaration for each household member",
        ))

    # Race and Ethnic Data Form — one per member
    has_race = any("Race" in dt and "Ethnic" in dt for dt in all_types)
    if not has_race:
        findings.append(_missing_document(
            "MI_RACE_ETHNIC_FORM_MISSING",
            "Move-in packet has no Race and Ethnic Data Form",
            "MI/IC certification requires Race and Ethnic Data Form "
            "for each household member — not found (Section 12)",
            "Obtain a Race and Ethnic Data Form for each household member",
        ))

    # Owner Summary Sheet
    has_owner_summary = any("Owner Summary" in dt for dt in doc_types)
    if not has_owner_summary:
        findings.append(_missing_document(
            "MI_OWNER_SUMMARY_MISSING",
            "Move-in packet has no Owner Summary Sheet",
            "MI/IC certification requires Owner Summary Sheet — not found (Section 12)",
            "Obtain the Owner Summary Sheet for the unit",
        ))

    # Family Summary Sheet
    has_family_summary = any("Family Summary" in dt for dt in doc_types)
    if not has_family_summary:
        findings.append(_missing_document(
            "MI_FAMILY_SUMMARY_MISSING",
            "Move-in packet has no Family Summary Sheet",
            "MI/IC certification requires Family Summary Sheet — not found (Section 12)",
            "Obtain the Family Summary Sheet for the household",
        ))

    findings.extend(_check_mi_application(all_types))

    # HUD 92006 (Emergency Contact)
    has_92006 = any("92006" in dt for dt in all_types)
    if not has_92006:
        findings.append(_missing_document(
            "MI_HUD_92006_MISSING",
            "Move-in packet has no HUD 92006",
            "MI/IC certification requires HUD 92006 (Emergency Contact) — not found (Section 12)",
            "Obtain a completed HUD 92006 (Supplement to Application for "
            "Federally Assisted Housing)",
        ))

    return findings


def _check_ar_requirements(
    doc_types: set[str],
    document_groups: list[DocumentGroup],
) -> list[Finding]:
    """Annual Recertification — previous cert must exist for comparison."""
    findings: list[Finding] = []

    # Previous certification should exist
    has_previous = any("(Previous)" in g.document_type for g in document_groups)
    if not has_previous:
        findings.append(_missing_document(
            "AR_PREVIOUS_CERT_MISSING",
            "Annual recertification with nothing to compare against",
            "AR certification but no previous certification found for comparison — "
            "previous cert is expected for annual recertification (Section 12)",
            "Supply the prior year's certification so the annual comparison "
            "can be made",
        ))

    return findings


def _check_arsc_requirements(document_groups: list[DocumentGroup]) -> list[Finding]:
    """AR-SC — the certification form must exist; it is the source of truth."""
    findings: list[Finding] = []

    # Recognition by label, not substring. "TIC" appears inside unrelated
    # labels and "Tenant Income Certification" is a prefix of the
    # questionnaire named after it — a questionnaire standing in for the
    # certification is precisely the failure this test used to allow. The
    # program also decides which form carries the certification: a 50059 or
    # an RD 3560-8 is as much the source of truth as a TIC, and demanding
    # the LIHTC form flags a HUD or Rural Development file that is complete.
    has_certification = any(
        is_current_certification_form(g.document_type)
        for g in document_groups if g.category != "ignore"
    )
    if not has_certification:
        findings.append(_missing_document(
            "ARSC_CERT_FORM_MISSING",
            "Self-certification with no certification form",
            "AR-SC certification requires the certification form (source of truth "
            "for self-certification) — not found (Section 12/13)",
            "Supply the signed certification form the self-certification rests on",
        ))

    return findings


def _check_ir_requirements() -> list[Finding]:
    """Interim Recertification — informational finding."""
    return [
        make_finding(
            "IR_SCOPE_NOTICE",
            "IR (Interim Recertification) detected — review is scoped to the specific change "
            "that triggered the interim (e.g., new employment, loss of income, new household member) "
            "(Section 12)",
            label="Interim recertification: review is scoped to the triggering change",
            category=CATEGORY_FILE_REVIEW,
            # Nothing is wrong and nobody has to act; this tells a reviewer
            # how to read the rest of the findings. Sending it as
            # non-compliant would put a clean interim on the exception list.
            result="na",
        )
    ]


def _check_mi_application(all_types: set[str]) -> list[Finding]:
    """The application is owed on every move-in, whatever the program."""
    findings: list[Finding] = []
    # Application for Housing
    has_application = any("Application" in dt or "Questionnaire" in dt for dt in all_types)
    if not has_application:
        findings.append(_missing_document(
            "MI_APPLICATION_MISSING",
            "Move-in packet has no Application for Housing",
            "MI/IC certification requires Application for Housing — not found (Section 12)",
            "Obtain the signed Application for Housing / household questionnaire",
        ))
    return findings
