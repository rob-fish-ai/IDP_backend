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


def _table_claims(text: str, members, claims, pn: int, authority: int, document_type: str) -> None:
    """Claims from the household-composition table: a header row names the
    SSN and date-of-birth columns, each member row is read by column."""
    ssn_col = dob_col = last_col = first_col = None
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
            continue
        if ssn_col is None and dob_col is None:
            continue
        low = [c.lower() for c in cells]
        who = None
        for m in members:
            first, last = _member_tokens(m)
            if not last:
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
            if value:
                claims[key]["ssn"].append({"value": value, "page": pn, "authority": authority, "document_type": document_type})
        if dob_col is not None:
            # The mapped cell first; when a merged header cell ("Rel. Sex")
            # has shifted the columns, any birth-year date in the row.
            candidates = _dates_in(cells[dob_col]) if dob_col < len(cells) else []
            if not candidates:
                candidates = [d for c in cells for d in _dates_in(c)]
            for d in candidates:
                if 1900 < int(d[:4]) < 2031:
                    claims[key]["dob"].append({"value": d, "page": pn, "authority": authority, "document_type": document_type})
                    break


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
            _table_claims(text, members, claims, pn, authority, g.document_type)
            # Free text: SSNs anywhere, dates of birth beside their label.
            for m in _SSN_RE.finditer(text):
                who = _nearest_member(text[max(0, m.start() - _NAME_WINDOW):m.start()], members, g.person_name)
                if who is None:
                    continue
                value = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                value = re.sub(r"[Xx]{3}-[Xx]{2}", "***-**", value)
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
    claims = collect_identity_claims(members, document_groups, page_text)

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
            agreeing = [x for x in ssn_claims if _last4(x["value"]) == auth_last4]
            disagreeing = [x for x in ssn_claims if _last4(x["value"]) != auth_last4]
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
