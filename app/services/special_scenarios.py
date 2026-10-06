"""Special scenario checks (Section 19)."""

import logging
import re
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
    findings.extend(_check_hud_9887_content(document_groups))
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


# What a complete HUD 9887 / 9887-A carries in its text. The identity
# marker says the group really is the form. Each required section is a
# page's distinctive wording (the HUD form text is fixed), satisfied by any
# of its patterns; a form missing a section is missing that page.
_HUD_FORM_CONTENT: dict[str, tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]] = {
    "HUD 9887": (
        (r"notice\s+and\s+consent",),
        (
            ("signature page", (r"signatures?\s*:", r"form\s+hud-?\s*9887\b", r"other\s+family\s+members\s+18")),
            ("agencies / expiry page", (r"expires\s+15\s+months", r"agencies\s+to\s+provide\s+information",
                                        r"privacy\s+act\s+statement")),
        ),
    ),
    "HUD 9887-A": (
        (r"applicant'?s?\s*/?\s*tenant'?s?\s+consent", r"consent\s+to\s+the\s+release\s+of\s+information"),
        (
            ("instructions page", (r"instructions\s+to\s+the\s+owner", r"persons\s+who\s+apply\s+for",
                                   r"applicant'?s?\s*/?\s*tenant'?s?\s+consent")),
            ("conditions / failure-to-sign page", (r"failure\s+to\s+sign", r"unauthorized\s+disclosure",
                                                   r"^\s*conditions\s*$")),
        ),
    ),
}


def _check_hud_9887_content(document_groups: list[DocumentGroup]) -> list[Finding]:
    """A HUD 9887 or 9887-A is complete when its text carries every page's
    distinctive section. This is the single owner of 9887 completeness: the
    page counts the signature validator and this module used to compare
    (four pages, two per adult) fired on every packet where the classifier
    split the consent package into its cover, fact sheet, 9887 and 9887-A."""
    import re as _re
    from app.services.doc_taxonomy import canonical_label
    from app.services.text_sanitizer import strip_html

    findings: list[Finding] = []
    for group in document_groups:
        base = canonical_label(group.document_type)[0]
        spec = _HUD_FORM_CONTENT.get(base)
        if not spec:
            continue
        text = strip_html(group.combined_text or "").lower()
        identity, sections = spec
        if not any(_re.search(rx, text) for rx in identity):
            continue  # not the form's own text; classification owns that
        missing = [
            name for name, patterns in sections
            if not any(_re.search(rx, text, _re.MULTILINE) for rx in patterns)
        ]
        if not missing:
            continue
        pages = ", ".join(str(p) for p in group.pages)
        code = "HUD_9887A_INCOMPLETE" if base == "HUD 9887-A" else "HUD_9887_INCOMPLETE"
        findings.append(make_finding(
            code,
            f"{base} (page{'s' if len(group.pages) > 1 else ''} {pages}) is missing its "
            f"{' and '.join(missing)} — the form is incomplete. Missing pages = finding (Section 19)",
            label=f"{base} is missing pages",
            category=CATEGORY_FILE_REVIEW,
            subject_type="document",
            subject_ref={"document_type": base, "pages": list(group.pages)},
            assignment=ASSIGN_CLIENT,
            correction_required=f"Obtain the complete {base}",
            resolution_type=RESOLVE_PRESENCE,
            pages=list(group.pages),
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


# A marked "yes" near an indicator word: "[X] Yes", "☒ Yes", "(X) Yes",
# "Yes X", "Yes ☒". An unmarked box (☐, [ ], □) or a bare "Yes No" pair
# is the question, not the answer.
# A marker after "yes" that is itself followed by "no" belongs to the "no"
# box ("yes ☒ no" in a box-before-label layout), so it does not mark yes.
_MARKED_YES_RE = re.compile(
    r"(?:\[\s*x\s*\]|☒|\(\s*x\s*\)|\bx)\s*yes\b"
    r"|\byes\s*(?:\[\s*x\s*\]|☒|\(\s*x\s*\)|\bx\b)(?!\s*(?:\[\s*\]|☐|□)?\s*no\b)",
    re.IGNORECASE,
)


def _indicated(text_lower: str, indicators: tuple[str, ...]) -> bool:
    """True when an indicator word is followed, within a form line, by a
    marked yes — or appears in prose with no yes/no choice at all (a
    narrative "currently homeless"). A form asking "Are you homeless?
    Yes No" is a question on every copy of that form, not an indication."""
    for kw in indicators:
        for m in re.finditer(re.escape(kw), text_lower):
            window = text_lower[m.end(): m.end() + 120]
            if _MARKED_YES_RE.search(window):
                return True
            if not re.search(r"\byes\b|\bno\b|\?", window) and not re.search(r"\?", text_lower[max(0, m.start() - 40): m.start()]):
                return True
    return False


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
            if _indicated(text_lower, homeless_indicators):
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
