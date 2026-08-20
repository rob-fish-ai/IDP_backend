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
