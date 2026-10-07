"""Identity fields resolved by document authority, with conflicts reported.

A household member's SSN, date of birth and relationship appear on several
documents of a packet: the certification form prints them (the SSN usually
masked to its last four), an identity document prints them, the application
or questionnaire carries them in the applicant's handwriting, and benefit
letters and statements repeat one or the other. The extractor used to pick
whichever it read first, so the same packet shipped a different last-four
on different runs — the certification's 8882 once, the questionnaire's
handwritten 8888 the next — and nothing compared the two.

This module collects every identity claim the packet makes about each
member, page by page, ranks the documents by authority, keeps the value the
highest-ranked document states, and raises a finding when documents
disagree. Handwriting never overrides a printed certification value.
"""
from __future__ import annotations

import logging
import re

from app.schemas.extraction import Finding
from app.services.doc_taxonomy import is_current_certification_form
from app.services.findings import (
    ASSIGN_CLIENT,
    CATEGORY_MEMBER,
    RESOLVE_PRESENCE,
    make_finding,
)
from app.services.validation import normalize_date

logger = logging.getLogger(__name__)

# Lower is more authoritative.
AUTHORITY_CERT = 0
AUTHORITY_IDENTITY_DOC = 1
AUTHORITY_APPLICATION = 2
AUTHORITY_SOURCE_DOC = 3
AUTHORITY_OTHER = 4

_AUTHORITY_NAMES = {
    AUTHORITY_CERT: "certification form",
    AUTHORITY_IDENTITY_DOC: "identity document",
    AUTHORITY_APPLICATION: "application / questionnaire",
    AUTHORITY_SOURCE_DOC: "source document",
    AUTHORITY_OTHER: "document",
}

_SSN_RE = re.compile(
    r"(?<![\d*X])(\d{3}|\*{3}|X{3}|x{3})\s?-\s?(\d{2}|\*{2}|X{2}|x{2})\s?-\s?(\d{4})(?!\d)"
)
_DATE_RE = re.compile(r"(?<![\d/])(\d{1,3})[/-](\d{1,3})[/-](\d{2,4})(?![\d/])|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DOB_LABEL_RE = re.compile(r"date\s*of\s*birth|\bd\.?o\.?b\.?\b|birth\s*date|\bbirthdate\b", re.IGNORECASE)
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_NAME_WINDOW = 400


def document_authority(document_type: str | None, category: str | None) -> int:
    label = (document_type or "").lower()
    if is_current_certification_form(document_type):
        return AUTHORITY_CERT
    if "identity" in label:
        return AUTHORITY_IDENTITY_DOC
    if "application" in label or "questionnaire" in label:
        return AUTHORITY_APPLICATION
    if category == "include":
        return AUTHORITY_SOURCE_DOC
    return AUTHORITY_OTHER


def _last4(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits[-4:] if len(digits) >= 4 else None


def _is_full_ssn(value: str | None) -> bool:
    return bool(re.fullmatch(r"\d{3}-\d{2}-\d{4}", value or ""))


def _member_key(m) -> str:
    first = (getattr(m, "FirstName", None) or "").strip().lower().split(" ")[0] if getattr(m, "FirstName", None) else ""
    last = (getattr(m, "LastName", None) or "").strip().lower()
    return f"{first} {last}".strip()


_SURNAME_PARTICLES = frozenset({"de", "la", "del", "las", "los", "da", "do", "dos", "van", "von", "der", "y", "e"})


def _surname_tokens(last: str) -> list[str]:
    """The name-bearing parts of a surname: "garcia de nava" → garcia, nava;
    "bribiesca-soto" → bribiesca, soto. A shorter form prints any one."""
    return [t for t in re.split(r"[-\s]+", (last or "").strip()) if t and t not in _SURNAME_PARTICLES]


def _issuable(value: str | None) -> bool:
    """False for a full number the Social Security Administration never
    issues — 999-99-9999, 000-00-0000, 123-45-0000. A form's own
    instruction ("if you do not have a SSN please enter 999-99-9999") and
    a placeholder typed in its place are not evidence about a person. A
    masked number or a last-four cannot be judged and is kept."""
    from app.services.validation import _ssn_parts_plausible
    m = re.fullmatch(r"(\d{3})\D?(\d{2})\D?(\d{4})", (value or "").strip())
    return True if not m else _ssn_parts_plausible(*m.groups())


def _base_surname(last: str) -> str:
    """The first name-bearing part of a compound surname."""
    parts = _surname_tokens(last)
    return parts[0] if parts else (last or "")


def _member_tokens(m) -> tuple[str, str]:
    first = ((getattr(m, "FirstName", None) or "").strip().lower().split(" ") or [""])[0]
    last = (getattr(m, "LastName", None) or "").strip().lower()
    return first, last


def _nearest_member(text_before: str, members, person_name: str | None):
    """The member whose name occurs last in the text before a match, else the
    member the document is about."""
    best, best_pos = None, -1
    low = text_before.lower()
    for m in members:
        first, last = _member_tokens(m)
        if not last:
            continue
        pos = low.rfind(last)
        if pos < 0:
            continue
        # A last name shared by several members needs the first name too.
        shared = sum(1 for o in members if _member_tokens(o)[1] == last) > 1
        if shared and first and low.rfind(first) < 0:
            continue
        if shared and first:
            pos = max(pos, low.rfind(first))
        if pos > best_pos:
            best, best_pos = m, pos
    if best is not None:
        return best
    if person_name:
        pl = person_name.lower()
        for m in members:
            first, last = _member_tokens(m)
            if last and last in pl and (not first or first in pl):
                return m
    return None


def _normalize_dob(raw: str) -> str | None:
    """A date as printed, tolerating one OCR-inserted digit in a part
    ("071/15/1949" for 07/15/1949)."""
    iso = normalize_date(raw)
    if iso:
        return iso
    m = re.fullmatch(r"(\d{1,3})[/-](\d{1,3})[/-](\d{2,4})", raw.strip())
    if not m:
        return None
    parts = list(m.groups())
    # Two-digit year: a birth date is in the past, so pivot on the current
    # year ("7/15/49" is 1949, "3/2/12" is 2012).
    if len(parts[2]) == 2:
        from datetime import date
        yy = int(parts[2]); cur = date.today().year % 100
        parts[2] = str(1900 + yy) if yy > cur else str(2000 + yy)
        iso = normalize_date("/".join(parts))
        if iso:
            return iso
    for i in (0, 1):
        if len(parts[i]) == 3:
            for cand in (parts[i][:2], parts[i][1:]):
                trial = parts[:]
                trial[i] = cand
                iso = normalize_date("/".join(trial))
                if iso:
                    return iso
    return None


def _dates_in(text: str) -> list[str]:
    out = []
    for m in _DATE_RE.finditer(text):
        iso = _normalize_dob(m.group(0))
        if iso:
            out.append(iso)
    return out


# Header cells arrive as <th> from the OCR's table markup; reading only
# <td> left every household-composition table headerless, so the TIC's
# own "Last 4 Digits of Social Security No." column was never a claim and
# a handwritten questionnaire outranked the certification by default.
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_SSN_HEADER_RE = re.compile(r"social security|\bssn\b|\bss ?#|ss no", re.IGNORECASE)
_DOB_HEADER_RE = re.compile(r"birth", re.IGNORECASE)
_LAST_HEADER_RE = re.compile(r"last name", re.IGNORECASE)
_FIRST_HEADER_RE = re.compile(r"first name", re.IGNORECASE)
# A single name column ("NAME", "Member Name", "Full Name") holding the
# whole name; matched by last name plus first name when the last is shared.
_NAME_HEADER_RE = re.compile(r"^(?:full |member |household member |applicant )?name(?:s)?$", re.IGNORECASE)


def _ssn_from_cell(cell: str) -> str | None:
    """The SSN a table cell prints: full with or without dashes, or the
    last four alone ("8882 Y" — the TIC prints the student flag beside it)."""
    plain = re.sub(r"<[^>]+>", " ", cell)
    m = _SSN_RE.search(plain)
    if m:
        value = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return re.sub(r"[Xx]{3}-[Xx]{2}", "***-**", value)
    m = re.search(r"(?<!\d)(\d{9})(?!\d)", plain)
    if m:
        d = m.group(1)
        return f"{d[:3]}-{d[3:5]}-{d[5:]}"
    m = re.search(r"(?<!\d)(\d{4})(?!\d)", plain)
    if m:
        return f"***-**-{m.group(1)}"
    return None


def _table_claims(text: str, members, claims, pn: int, authority: int, document_type: str) -> set[str]:
    """Claims from the household-composition table: a header row names the
    SSN and date-of-birth columns, each member row is read by column.
    Returns the SSN values claimed by row, so the free-text pass does not
    attribute them again to whichever name happens to precede them."""
    ssn_col = dob_col = last_col = first_col = name_col = None
    claimed: set[str] = set()
    claimed_dobs: set[str] = set()
    for row in _ROW_RE.finditer(text):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in _CELL_RE.findall(row.group(0))]
        if not cells:
            continue
        header_hits = [i for i, c in enumerate(cells) if _SSN_HEADER_RE.search(c) or _DOB_HEADER_RE.search(c) or _LAST_HEADER_RE.search(c)]
        if header_hits and not any(re.search(r"\d{4}", c) for c in cells):
            ssn_col = next((i for i, c in enumerate(cells) if _SSN_HEADER_RE.search(c)), None)
            dob_col = next((i for i, c in enumerate(cells) if _DOB_HEADER_RE.search(c)), None)
            last_col = next((i for i, c in enumerate(cells) if _LAST_HEADER_RE.search(c)), None)
            first_col = next((i for i, c in enumerate(cells) if _FIRST_HEADER_RE.search(c)), None)
            name_col = next((i for i, c in enumerate(cells) if _NAME_HEADER_RE.search(c.strip())), None) if last_col is None else None
            continue
        if ssn_col is None and dob_col is None:
            continue
        low = [c.lower() for c in cells]
        who = None
        for m in members:
            first, last = _member_tokens(m)
            if not last:
                continue
            if name_col is not None and name_col < len(low):
                # The row's name cell must carry the last name; the first
                # name too when several members share the last name.
                cell = low[name_col]
                if last in cell or any(re.search(rf"\b{re.escape(t)}\b", cell) for t in _surname_tokens(last)):
                    # Any part of the surname in the cell; the first name too
                    # when another member shares a part of it.
                    shared = any(o is not m and set(_surname_tokens(_member_tokens(o)[1])) & set(_surname_tokens(last))
                                 for o in members)
                    if not shared or not first or first in cell:
                        who = m
                        break
                continue
            if last_col is not None and last_col < len(low) and low[last_col] == last:
                if first_col is not None and first_col < len(low) and first and not low[first_col].startswith(first):
                    continue
                who = m
                break
            if last_col is None and any(c == last for c in low) and (not first or any(c.startswith(first) for c in low)):
                who = m
                break
        if who is None:
            continue
        key = _member_key(who)
        if ssn_col is not None and ssn_col < len(cells):
            value = _ssn_from_cell(cells[ssn_col])
            if value and _issuable(value):
                claims[key]["ssn"].append({"value": value, "page": pn, "authority": authority, "document_type": document_type})
                claimed.add(value)
        if dob_col is not None:
            # The mapped cell first; when a merged header cell ("Rel. Sex")
            # has shifted the columns, any birth-year date in the row.
            candidates = _dates_in(cells[dob_col]) if dob_col < len(cells) else []
            if not candidates:
                candidates = [d for c in cells for d in _dates_in(c)]
            for d in candidates:
                if 1900 < int(d[:4]) < 2031:
                    claims[key]["dob"].append({"value": d, "page": pn, "authority": authority, "document_type": document_type})
                    claimed_dobs.add(d)
                    break
    return claimed, claimed_dobs
def collect_identity_claims(members, document_groups, page_text: dict[int, str]) -> dict[str, dict[str, list[dict]]]:
    """Every SSN and date-of-birth the packet states, attributed to a member.

    Returns {member_key: {"ssn": [claim...], "dob": [claim...]}} where a
    claim is {"value", "page", "authority", "document_type"}. SSN claims
    carry the value as printed (full or masked). Household-composition
    tables are read by column from their header row; elsewhere a match is
    attributed to the nearest member name before it on the page, falling
    back to the member the document group is about.
    """
    claims: dict[str, dict[str, list[dict]]] = {_member_key(m): {"ssn": [], "dob": []} for m in members}
    for g in document_groups:
        if g.category == "ignore":
            continue
        authority = document_authority(g.document_type, g.category)
        for pn in g.pages:
            text = page_text.get(pn) or ""
            if not text:
                continue
            by_row, by_row_dobs = _table_claims(text, members, claims, pn, authority, g.document_type)
            # Free text: SSNs anywhere, dates of birth beside their label.
            for m in _SSN_RE.finditer(text):
                value = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                value = re.sub(r"[Xx]{3}-[Xx]{2}", "***-**", value)
                if value in by_row or not _issuable(value):
                    continue
                who = _nearest_member(text[max(0, m.start() - _NAME_WINDOW):m.start()], members, g.person_name)
                if who is None:
                    continue
                claims[_member_key(who)]["ssn"].append({
                    "value": value, "page": pn, "authority": authority, "document_type": g.document_type,
                })
            for m in _DOB_LABEL_RE.finditer(text):
                # Stop at the next labelled field so a signature date two
                # fields on is not read as a birth date.
                window = text[m.end(): m.end() + 60]
                cut = re.search(r"(?:date|sign|phone|ssn)\s*[:#]", window, re.IGNORECASE)
                if cut:
                    window = window[:cut.start()]
                dates = _dates_in(window)
                if not dates or not (1900 < int(dates[0][:4]) < 2031):
                    continue
                if dates[0] in by_row_dobs:
                    continue
                who = _nearest_member(text[max(0, m.start() - _NAME_WINDOW):m.start()], members, g.person_name)
                if who is None:
                    continue
                claims[_member_key(who)]["dob"].append({
                    "value": dates[0], "page": pn, "authority": authority, "document_type": g.document_type,
                })
    # A claim repeated on one page is one claim.
    for c in claims.values():
        for kind in ("ssn", "dob"):
            seen = set(); uniq = []
            for x in c[kind]:
                k = (x["value"], x["page"])
                if k in seen:
                    continue
                seen.add(k); uniq.append(x)
            c[kind] = uniq
    return claims


_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")


def _one_edit_apart(a: str, b: str) -> bool:
    """Levenshtein distance of exactly one (substitution, insertion, deletion)."""
    if a == b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1:]


def _snap_names_to_certification(members, document_groups, page_text: dict[int, str]) -> None:
    """A member surname not printed on the certification, when the form
    prints a name one edit away, takes the form's spelling — the same way
    a date of birth takes the certification's value. "Bribiesca-Solo" read
    from a form printing "Bribiesca-Soto" would otherwise reach the
    consumer as a new person."""
    cert_pages = [pn for g in document_groups if g.category != "ignore"
                  and document_authority(g.document_type, g.category) == AUTHORITY_CERT for pn in g.pages]
    if not cert_pages:
        return
    text = " ".join(page_text.get(pn) or "" for pn in cert_pages)
    text = re.sub(r"<[^>]+>", " ", text)
    tokens = {t for t in _NAME_TOKEN_RE.findall(text)}
    low = {t.lower(): t for t in tokens}
    for m in members:
        last = (getattr(m, "LastName", None) or "").strip()
        if not last or last.lower() in low:
            continue
        near = sorted({low[t] for t in low if _one_edit_apart(t, last.lower())})
        if len(near) != 1:
            continue
        logger.info("Identity: %s %s surname '%s' is not on the certification, which prints '%s' one edit away — using the form's spelling",
                    getattr(m, "FirstName", "") or "", last, last, near[0])
        m.LastName = near[0]


def resolve_identities(household, document_groups, page_text: dict[int, str]) -> list[Finding]:
    """Set each member's SSN and DOB from the most authoritative document that
    states them, and report every disagreement between documents.

    SSN: the certification prints the last four; a full SSN from any document
    is kept only when its last four agrees with the authoritative value.
    DOB: the authoritative document's date, normalised.
    """
    findings: list[Finding] = []
    members = list(getattr(household, "houseHold", None) or [])
    if not members or not document_groups:
        return findings
    _snap_names_to_certification(members, document_groups, page_text)
    claims = collect_identity_claims(members, document_groups, page_text)

    # Each member's authoritative last four, so a value attributed to the
    # wrong member on a page listing the whole household is recognised as
    # another member's SSN rather than reported as a conflict.
    authoritative: dict[str, str] = {}
    authoritative_dob: dict[str, str] = {}
    for m in members:
        c = claims.get(_member_key(m)) or {"ssn": [], "dob": []}
        top = sorted((x for x in c["ssn"] if _last4(x["value"])), key=lambda x: (x["authority"], x["page"]))
        if top:
            authoritative[_member_key(m)] = _last4(top[0]["value"])
        top_dob = sorted((x for x in c.get("dob", []) if x["value"]), key=lambda x: (x["authority"], x["page"]))
        if top_dob:
            authoritative_dob[_member_key(m)] = top_dob[0]["value"]

    for m in members:
        key = _member_key(m)
        name = f"{m.FirstName or ''} {m.LastName or ''}".strip() or "a household member"
        c = claims.get(key) or {"ssn": [], "dob": []}

        # --- SSN ---
        ssn_claims = [x for x in c["ssn"] if _last4(x["value"])]
        if ssn_claims:
            ssn_claims.sort(key=lambda x: (x["authority"], x["page"]))
            top = ssn_claims[0]
            auth_last4 = _last4(top["value"])
            others = {v for k, v in authoritative.items() if k != key}
            agreeing = [x for x in ssn_claims if _last4(x["value"]) == auth_last4]
            disagreeing = [x for x in ssn_claims if _last4(x["value"]) != auth_last4 and _last4(x["value"]) not in others]
            full = next((x["value"] for x in agreeing if _is_full_ssn(x["value"])), None)
            chosen = full or top["value"]
            current = m.socialSecurityNumber
            if _last4(current) != auth_last4 or (full and current != full and not _is_full_ssn(current)):
                if current and _last4(current) != auth_last4:
                    logger.info(
                        "Identity: %s SSN %s replaced by the %s's %s (page %s)",
                        name, current, _AUTHORITY_NAMES[top["authority"]], chosen, top["page"],
                    )
                m.socialSecurityNumber = chosen
            if disagreeing:
                seen: set[str] = set()
                parts = []
                for x in disagreeing:
                    if _last4(x["value"]) in seen:
                        continue
                    seen.add(_last4(x["value"]))
                    parts.append(f"{x['document_type']} p{x['page']} prints ...{_last4(x['value'])}")
                findings.append(make_finding(
                    "MEMBER_IDENTITY_CONFLICT",
                    f"{name}: the {_AUTHORITY_NAMES[top['authority']]} (p{top['page']}) prints SSN "
                    f"ending {auth_last4}, but {'; '.join(parts)} — the packet carries two SSNs "
                    f"for one person; the certification's value is used (Section 4)",
                    label=f"SSN differs between documents for {name}",
                    category=CATEGORY_MEMBER,
                    subject_type="household_member",
                    subject_ref={"member_name": name, "field": "socialSecurityNumber"},
                    result="non_compliant",
                    assignment=ASSIGN_CLIENT,
                    correction_required=f"Confirm {name}'s SSN against the Social Security card and correct the document that is wrong",
                    resolution_type=RESOLVE_PRESENCE,
                    pages=sorted({top["page"]} | {x["page"] for x in disagreeing}),
                ))

        # --- DOB ---
        dob_claims = [x for x in c["dob"] if x["value"]]
        if dob_claims:
            dob_claims.sort(key=lambda x: (x["authority"], x["page"]))
            top = dob_claims[0]
            auth = top["value"]
            others = {x["value"] for x in dob_claims if x["value"] != auth}
            # Two-digit-year readings that agree on month and day are the
            # same date; do not report a scanner's century as a conflict.
            others = {v for v in others if v[5:] != auth[5:]}
            # Another member's date, attributed here by a name that happened
            # to precede it, is not this member's conflict.
            others = {v for v in others if v not in {d for k, d in authoritative_dob.items() if k != key}}
            if m.DOB != auth:
                if m.DOB:
                    logger.info(
                        "Identity: %s DOB %s replaced by the %s's %s (page %s)",
                        name, m.DOB, _AUTHORITY_NAMES[top["authority"]], auth, top["page"],
                    )
                m.DOB = auth
            if others:
                where = [f"{x['document_type']} p{x['page']} prints {x['value']}" for x in dob_claims if x["value"] in others]
                findings.append(make_finding(
                    "MEMBER_IDENTITY_CONFLICT",
                    f"{name}: the {_AUTHORITY_NAMES[top['authority']]} (p{top['page']}) prints date of "
                    f"birth {auth}, but {'; '.join(dict.fromkeys(where))} — the certification's value "
                    f"is used (Section 4)",
                    label=f"Date of birth differs between documents for {name}",
                    category=CATEGORY_MEMBER,
                    subject_type="household_member",
                    subject_ref={"member_name": name, "field": "DOB"},
                    result="non_compliant",
                    assignment=ASSIGN_CLIENT,
                    correction_required=f"Confirm {name}'s date of birth against an identity document and correct the document that is wrong",
                    resolution_type=RESOLVE_PRESENCE,
                    pages=sorted({top["page"]} | {x["page"] for x in dob_claims if x["value"] in others}),
                ))
    return findings
