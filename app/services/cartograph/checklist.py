"""Cartograph's checklist rows, and what the packet says about each.

A certification review in Cartograph carries a checklist built from the
community's template before the case reaches the engine. Each row has a
finding id of its own, a stable item code ("HUDS8-FORM-50059"), a label
that names the form ("HUD 50059 Completed and Accurate") and, on the flat
shape, the household member it is about. The engine reads the rows off
the case request, matches each to the documents it classified, and sends
back one entry per row it could judge: found or not, the pages, a note
for the row's note field, and how sure it is. Rows it cannot map to a
document type it knows are left out, and Cartograph leaves those alone.

Two request shapes are read. Today's nests the rows under
requirements[].community_defaults.checklist_items[], one entry per item
with a list of this case's finding ids (per-member items bundled). The
flat checklist_rows[] that replaces it has one row per finding id with a
subject. Both produce the same normalised rows.
"""
from __future__ import annotations

import logging
import re

from app.services.doc_taxonomy import (
    COMPLIANCE, INCLUDE, TAXONOMY, _split_suffix, canonical_label, is_previous_certification,
)


def _base(document_type: str | None) -> str:
    """The label without its "(Previous)" / "(Superseded)" suffix."""
    return _split_suffix(document_type or "")[0]

logger = logging.getLogger(__name__)

_FORM_TOKEN_RE = re.compile(r"\b(\d{3,5}(?:[-/]?[a-z0-9]{1,2})?)\b", re.IGNORECASE)
_STOP = frozenset({
    "and", "the", "of", "for", "a", "an", "to", "on", "in", "is", "are", "form", "forms", "hud",
    "completed", "accurate", "signed", "dated", "present", "required", "copy", "current", "all",
    "each", "member", "members", "adult", "adults", "household", "verification", "verified",
    "item", "checklist", "review", "file", "documentation", "document", "documents", "provided",
})


def _form_tokens(text: str) -> set[str]:
    """Form numbers as a form prints them: 50059, 9887, 9887-a, 92006, 3560-8."""
    out = set()
    for tok in _FORM_TOKEN_RE.findall(text or ""):
        t = tok.lower().replace("/", "-")
        out.add(t)
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z']{2,}", (text or "").lower()) if w not in _STOP}


def _one_edit_apart(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1:]


def _shared_words(row_words: set[str], label_words: set[str]) -> int:
    """Words in common, a misspelling one edit away counting as the word
    ("Appliication" is "Application")."""
    return sum(1 for w in row_words if w in label_words or any(len(w) > 4 and _one_edit_apart(w, l) for l in label_words))


# A row that asks about a property of a form — whether it includes the
# right limits, is dated within 120 days, was calculated properly — is not
# answered by the form being present. The engine reports those through its
# findings; such rows are left untouched here.
_ATTRIBUTE_WORDS = frozenset({
    "includes", "include", "correct", "within", "days", "consecutive", "calculation", "calculated",
    "properly", "limits", "limit", "allowance", "rate", "balance", "value", "accurately", "changes",
    "terminated", "closed", "reminder", "reminders", "counted", "treated", "determined", "used",
})


def is_attribute_row(row: dict) -> bool:
    return bool(_words(row.get("label") or "") & _ATTRIBUTE_WORDS)


def wants_previous(row: dict) -> bool:
    return bool({"previous", "prior"} & set(re.findall(r"[a-z]+", (row.get("label") or "").lower())))


def normalise_rows(payload: dict) -> list[dict]:
    """The checklist rows a case request carries, one per finding id."""
    rows: list[dict] = []
    seen: set = set()
    for r in payload.get("checklist_rows") or []:
        if not isinstance(r, dict):
            continue
        fid = r.get("finding_id") or r.get("case_finding_id") or r.get("id")
        if fid is None or fid in seen:
            continue
        seen.add(fid)
        rows.append({
            "finding_id": fid,
            "checklist_item_id": r.get("checklist_item_id"),
            "item_code": r.get("item_code"),
            "label": r.get("label") or r.get("name"),
            "subject_type": r.get("subject_type"),
            "subject_id": r.get("subject_id"),
            "subject_label": r.get("subject_label"),
        })
    for req in payload.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        defaults = req.get("community_defaults") or {}
        for item in (defaults.get("checklist_items") if isinstance(defaults, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            for fid in item.get("case_finding_ids") or []:
                if fid is None or fid in seen:
                    continue
                seen.add(fid)
                rows.append({
                    "finding_id": fid,
                    "checklist_item_id": item.get("checklist_item_id"),
                    "item_code": item.get("item_code"),
                    "label": item.get("label"),
                    "subject_type": None, "subject_id": None, "subject_label": None,
                })
    return rows


def _candidate_labels() -> list[tuple[str, set[str], set[str]]]:
    """(label, form tokens, words) for every document type a checklist can name."""
    out = []
    for label, spec in TAXONOMY.items():
        if spec["category"] not in (INCLUDE, COMPLIANCE):
            continue
        names = [label, *spec["aliases"]]
        out.append((label, set().union(*(_form_tokens(n) for n in names)), set().union(*(_words(n) for n in names))))
    return out


def document_type_for(row: dict) -> tuple[str | None, str | None]:
    """The taxonomy label a checklist row names, and how it was matched:
    'form_number' (the row and one label share a form number exactly),
    'title' (a label or alias is printed in the row's label), or
    'words' (most content words in common, at least half of the row's)."""
    text = f"{row.get('label') or ''} {row.get('item_code') or ''}".replace("_", " ").replace("-", " - ")
    row_tokens = _form_tokens(row.get("label") or "") | _form_tokens((row.get("item_code") or "").split("-")[-1] if row.get("item_code") else "")
    candidates = _candidate_labels()
    if row_tokens:
        hits = [(label, toks, words) for label, toks, words in candidates if toks and (toks & row_tokens)]
        # The form number must match exactly: "9887" is not "9887-a", and a
        # label carrying extra numbers ("HUD 9887/A Fact Sheet") loses to
        # one carrying only the number named.
        exact = [(label, words) for label, toks, words in hits if toks == (toks & row_tokens)]
        if len(exact) == 1:
            return exact[0][0], "form_number"
        if len(exact) > 1:
            # Several labels share the number (the 9887 and its package
            # cover): the one whose words, aliases included, the row also
            # names; on a tie the plain form over a derivative of it.
            rw = _words(text)
            best = max(exact, key=lambda lw: (len(lw[1] & rw), -len(lw[0])))
            return best[0], "form_number"
    low = (row.get("label") or "").lower()
    titled = [label for label, _, _ in candidates
              for n in (label, re.sub(r"\s*\([^)]*\)\s*$", "", label), *TAXONOMY[label]["aliases"])
              if len(n) >= 8 and n.lower() in low]
    if titled:
        return max(titled, key=len), "title"
    rw = _words(row.get("label") or "")
    if rw:
        scored = [(_shared_words(rw, w) / len(rw), label) for label, _, w in candidates]
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 0.5:
            return scored[0][1], "words"
    return None, None


_BASE_CONFIDENCE = {"form_number": 0.9, "title": 0.8, "words": 0.6}


def _member_matches(person_name: str | None, subject_label: str | None) -> bool:
    if not subject_label:
        return True
    if not person_name:
        return False
    a = {w for w in re.findall(r"[a-z]{2,}", person_name.lower())}
    b = {w for w in re.findall(r"[a-z]{2,}", subject_label.lower())}
    shared = a & b
    # A surname alone is not a person when the household shares it: both
    # parts must agree when both names carry them.
    return len(shared) >= 2 or (bool(shared) and (len(a) == 1 or len(b) == 1))


def match_checklist(rows: list[dict], extraction) -> list[dict]:
    """One entry per checklist row the packet can answer."""
    all_groups = list(getattr(extraction, "document_groups", None) or [])
    groups = [g for g in all_groups if g.category != "ignore" and not is_previous_certification(g.document_type)]
    previous = [g for g in all_groups if is_previous_certification(g.document_type)]
    incomplete = {}
    for f in getattr(extraction, "finding_records", None) or []:
        if f.code.endswith("_INCOMPLETE") and isinstance(f.subject_ref, dict) and f.subject_ref.get("document_type"):
            incomplete[f.subject_ref["document_type"]] = f.text
    inventory = list((getattr(extraction, "document_inventory_hud", None) or type("x", (), {"documents": []})).documents or []) + \
                list((getattr(extraction, "document_inventory_financial", None) or type("x", (), {"documents": []})).documents or [])
    cert = getattr(extraction, "certification_info", None)
    out: list[dict] = []
    for row in rows:
        if is_attribute_row(row):
            continue
        label, how = document_type_for(row)
        if not label:
            continue
        if wants_previous(row):
            # "Previous HUD 50059": the prior certification the packet
            # carries, which the audit otherwise sets aside.
            prev = [g for g in previous if _base(g.document_type) == label]
            if prev:
                pages = sorted({p for g in prev for p in g.pages})
                when = next((g.notes for g in prev if g.notes), None)
                out.append({"finding_id": row["finding_id"], "found": True, "pages": pages,
                            "confidence": round(_BASE_CONFIDENCE[how], 2),
                            "note": f"Previous {label} present, page{'s' if len(pages) > 1 else ''} {_page_span(pages)}." + (f" {when}." if when else "")})
            else:
                out.append({"finding_id": row["finding_id"], "found": False, "pages": [],
                            "confidence": round(_BASE_CONFIDENCE[how], 2), "note": f"No previous {label} in the packet."})
            continue
        mine = [g for g in groups if canonical_label(g.document_type)[0] == label]
        if row.get("subject_label"):
            named = [g for g in mine if _member_matches(g.person_name, row["subject_label"])]
            mine = named or mine
        confidence = _BASE_CONFIDENCE[how]
        if not mine:
            out.append({
                "finding_id": row["finding_id"], "found": False, "pages": [], "confidence": round(confidence, 2),
                "note": f"No {label} in the packet." + (f" (for {row['subject_label']})" if row.get("subject_label") else ""),
            })
            continue
        pages = sorted({p for g in mine for p in g.pages})
        placed_approx = any("nearest match" in (g.notes or "") or "relabelled" in (g.notes or "") for g in mine)
        if placed_approx:
            confidence -= 0.15
        entries = [e for e in inventory if (e.documentType or "") == label and _member_matches(e.personName, row.get("subject_label"))]
        note = f"{label} present, page{'s' if len(pages) > 1 else ''} {_page_span(pages)}."
        signed = next((e for e in entries if e.isSigned == "Yes"), None)
        unsigned = [e for e in entries if e.isSigned == "No"]
        if cert is not None and any(canonical_label(g.document_type)[0] == label for g in mine) and \
                getattr(cert, "isSigned", None) == "No" and label in _CERT_FORMS:
            note += " Not signed: the signature lines are blank."
            confidence = min(confidence, 0.85)
        elif signed:
            who = f" by {signed.signedBy}" if signed.signedBy else ""
            when = f" on {signed.signatureDate}" if signed.signatureDate else ", date not read"
            note += f" Signed{who}{when}."
        elif unsigned:
            note += " Signature not verified from text, check visually."
            confidence = min(confidence, 0.6)
        if label in incomplete:
            missing = re.search(r"is missing its (.+?) — ", incomplete[label])
            note += f" Incomplete: missing its {missing.group(1)}." if missing else " Incomplete."
        out.append({"finding_id": row["finding_id"], "found": True, "pages": pages,
                    "confidence": round(max(0.0, min(1.0, confidence)), 2), "note": note})
    return out


_CERT_FORMS = {"HUD 50059", "Tenant Income Certification (TIC)", "HUD 3560 Form"}


def _page_span(pages: list[int]) -> str:
    if not pages:
        return ""
    runs: list[list[int]] = [[pages[0], pages[0]]]
    for p in pages[1:]:
        if p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in runs)
