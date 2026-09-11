"""Construction and identity for structured audit findings.

Findings are emitted from many places in the pipeline. This module gives them
one shape and one rule for identity, so that:

  - a re-audit of the same packet produces the same finding_key for the same
    problem, letting a consumer update in place rather than duplicating, and
    letting a reviewer's resolution survive;
  - each finding carries the subject it concerns (a member, an income source,
    an asset) rather than only naming it inside prose.

Identity is derived, never random: same code + same subject = same key. That
means finding text may be reworded without breaking the link, but changing the
subject correctly produces a new finding.
"""

import re

from app.schemas.extraction import Finding

# Categories mirror the consuming checklist's own grouping.
CATEGORY_UNIT_RENT = "unit_rent"
CATEGORY_MEMBER = "household_member"
CATEGORY_INCOME = "income"
CATEGORY_ASSET = "asset"
CATEGORY_EXPENSE = "expense"
CATEGORY_FILE_REVIEW = "file_review"

VALID_CATEGORIES = frozenset({
    CATEGORY_UNIT_RENT, CATEGORY_MEMBER, CATEGORY_INCOME,
    CATEGORY_ASSET, CATEGORY_EXPENSE, CATEGORY_FILE_REVIEW,
})

# Who acts on the finding: internal staff, the client, or neither because the
# issue is procedural.
ASSIGN_INTERNAL = "internal"
ASSIGN_CLIENT = "client"
ASSIGN_PROCEDURAL = "procedural_issue"

# Whether resolving the finding is satisfied by a document arriving, or
# requires the affected figure to be recomputed once it does.
RESOLVE_PRESENCE = "presence_only"
RESOLVE_RECALC = "recalculation"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Findings that report the extraction contradicting the document's own account
# of itself: a declared total the extracted sources do not sum to, a figure
# printed on the certification that matches no record, two calculation methods
# that disagree, a source attributed to nobody on the roster, the same income
# read twice.
#
# These are evidence about the RELIABILITY of the values involved, not only
# about the household, which is why the scorer reads them. A missing-document
# finding is deliberately absent: the file is incomplete, but nothing about it
# says the engine misread what is there.
#
# Kept here, keyed by code, rather than passed at each of the seventeen
# emission sites. A flag repeated per call site drifts — one emitter gets it,
# the next one added does not, and the omission is silent because a finding
# that fails to lower confidence looks exactly like a finding about a
# household rather than about an extraction.
_DISPUTES_EXTRACTION = frozenset({
    # The certification's own totals versus what was extracted
    "TIC_TOTAL_MISMATCH",
    "TIC_TOTAL_MINOR_DIFF",
    "CERT_SUMMARY_INCOME_MISMATCH",
    "CERT_AMOUNT_UNACCOUNTED",
    "HH_SIZE_MISMATCH",
    # Calculations that disagree with each other
    "INCOME_METHOD_OUTLIER",
    "INCOME_METHODS_DISAGREE",
    # A record attributed to someone the roster does not contain
    "INCOME_MEMBER_NOT_IN_ROSTER",
    "PAYSTUB_MEMBER_NOT_IN_ROSTER",
    "ASSET_OWNER_NOT_IN_ROSTER",
    # The same thing read more than once
    "DUPLICATE_INCOME_RECORD",
    "NEAR_DUPLICATE_INCOME",
    "DUPLICATE_EMPLOYER",
    "DUPLICATE_MEMBER",
    "POSSIBLE_DUPLICATE_MEMBER_DOB",
    "POSSIBLE_DUPLICATE_MEMBER_SSN",
    # A value that cannot be what the record says it is
    "PAYSTUB_AMOUNT_SUSPECT",
    "SSA_AS_PAYSTUB_AND_VOI",
    # An amount the packet does not contain anywhere the audit may read
    "INCOME_AMOUNT_NOT_IN_SOURCE",
    # The certification declares income the extraction produced nothing for.
    # This is the strongest dispute of all and was missing: it fires exactly
    # when extraction found NOTHING, which is when the score most needs to
    # fall and — because a record that does not exist cannot be marked down —
    # is precisely when it used to rise.
    "TIC_TOTAL_NO_CALCULATIONS",
    "TIC_TOTAL_NOT_EXTRACTED",
    # Two reads of the engine's own that do not agree
    "SIGNATURE_VERDICT_CONFLICTS_WITH_DATE",
    "ASSET_SELF_DECLARED_VS_VERIFIED",
    # A document read as something it is not
    "CALC_WORKSHEET_AS_VOI",
    "FIXED_INCOME_PAYSTUB",
    # Two documents state different identity values for one member
    "MEMBER_IDENTITY_CONFLICT",
})


def disputes_extraction(code: str) -> bool:
    """Whether a finding code reports the extraction contradicting the source."""
    return code in _DISPUTES_EXTRACTION


def slug(value: str | None) -> str:
    """Normalize a name into a stable key fragment.

    Deliberately lossy and case-insensitive: 'Teksystems, Inc.' and
    'TEKSYSTEMS INC' collapse to the same fragment, so trivial extraction
    variation between runs does not mint a new finding.
    """
    if not value:
        return ""
    return _SLUG_RE.sub("_", str(value).strip().lower()).strip("_")


def build_finding_key(code: str, subject_ref: dict | None) -> str:
    """Deterministic identity for a finding.

    Subject values are sorted by key name so the fragment does not depend on
    dict insertion order, which varies with the call site.
    """
    if not subject_ref:
        return f"{code}:case"
    parts = [slug(subject_ref[k]) for k in sorted(subject_ref) if subject_ref.get(k)]
    parts = [p for p in parts if p]
    return f"{code}:{':'.join(parts)}" if parts else f"{code}:case"


def make_finding(
    code: str,
    text: str,
    *,
    category: str = CATEGORY_FILE_REVIEW,
    label: str | None = None,
    subject_type: str | None = None,
    subject_ref: dict | None = None,
    result: str = "non_compliant",
    assignment: str | None = None,
    correction_required: str | None = None,
    resolution_type: str | None = None,
    confidence: float | None = None,
    pages: list[int] | None = None,
) -> Finding:
    """Build a Finding with a derived key.

    `text` is the full wording and stays the canonical string, so migrating an
    emitter to this constructor does not change what existing consumers read.
    """
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Unknown finding category: {category!r}")
    ref = {k: v for k, v in (subject_ref or {}).items() if v}
    return Finding(
        code=code,
        text=text,
        category=category,
        label=label,
        subject_type=subject_type,
        subject_ref=ref,
        result=result,
        assignment=assignment,
        correction_required=correction_required,
        resolution_type=resolution_type,
        confidence=confidence,
        pages=pages or [],
        disputes_extraction=disputes_extraction(code),
        finding_key=build_finding_key(code, ref),
    )


def text_of(finding) -> str:
    """Wording of a finding, whether it is structured or a legacy string.

    Emitters are migrating module by module, so the working list holds both
    forms. Any code that inspects finding wording must go through this.
    """
    return finding.text if isinstance(finding, Finding) else str(finding)


def dedupe(findings: list) -> list:
    """Drop repeat findings, preserving first-seen order.

    Several detectors iterate per source document rather than per record, so a
    member with six paystubs from one employer produced the same finding six
    times. Identity is the finding_key where there is one, otherwise the exact
    wording — two genuinely different findings about the same record are both
    kept, while exact repeats collapse.
    """
    seen: set = set()
    out: list = []
    for f in findings:
        ident = f.finding_key if isinstance(f, Finding) and f.finding_key else text_of(f)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(f)
    return out


def render(findings: list) -> list[str]:
    """Flatten a mixed list of Finding objects and legacy strings to strings.

    Emitters are being migrated module by module, so both forms coexist. Every
    consumer that predates the migration reads this rendering.
    """
    out: list[str] = []
    for f in findings:
        out.append(f.text if isinstance(f, Finding) else str(f))
    return out


def records(findings: list) -> list[Finding]:
    """Return only the structured findings from a mixed list."""
    return [f for f in findings if isinstance(f, Finding)]
