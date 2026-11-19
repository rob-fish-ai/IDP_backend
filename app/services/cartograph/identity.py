"""Identity documents per household member, for the DOB and SSN checklist rows.

A checklist row asks whether a member's date of birth or Social Security
number was verified. Which documents count depends on the funding
program: a HUD file wants the SSN card, a birth certificate, naturalization
papers or a passport; a tax-credit file accepts the household's own
declaration on the application, or a driver's license. Cartograph sends
the accepted list for the case's program with each scan
(``verification_methods``); without one the engine falls back to those
defaults by program.

For each row the engine finds the identity pages that belong to the
member — by the name the classifier read on the page, or by the member's
own number or birth date printed there — and answers with the kind of
proof it found, whether that proof is on the program's list, the request
document the page came from, and the pages. A proof that is not accepted
is still reported, so staff see what the file holds. An identity page
that belongs to nobody the engine can name is mentioned on the rows it
could have satisfied, rather than dropped.
"""
from __future__ import annotations

import re
from datetime import date

from app.services.doc_taxonomy import FAMILY_DECLARATION, _split_suffix, family_of, is_current_certification_form
from app.services.program import is_hud_property

# Cartograph's vocabulary for a proof of identity, by the engine's label.
# The license/state-ID label is split by the card's own words.
METHOD_OF_LABEL = {
    "Social Security Card": "ssn_card",
    "Birth Certificate": "birth_certificate",
    "Passport": "passport",
    "Naturalization / Immigration Document": "naturalization_papers",
    "Driver License / State ID": "drivers_license",
    "Identity Document": "other_identity_document",
}

# What each proof establishes.
PROVES = {
    "ssn_card": frozenset({"ssn"}),
    "birth_certificate": frozenset({"dob"}),
    "passport": frozenset({"dob"}),
    "naturalization_papers": frozenset({"dob"}),
    "drivers_license": frozenset({"dob"}),
    "state_id": frozenset({"dob"}),
    "other_identity_document": frozenset({"dob"}),
    "self_declaration": frozenset({"dob", "ssn"}),
}

# Scott's lists, used when the request carries none. A stronger proof is
# accepted wherever a declaration is.
DEFAULT_METHODS = {
    "hud": {"dob": ("birth_certificate", "passport", "naturalization_papers"),
            "ssn": ("ssn_card",)},
    "other": {"dob": ("self_declaration", "drivers_license", "state_id", "birth_certificate", "passport",
                      "naturalization_papers"),
              "ssn": ("self_declaration", "ssn_card")},
}

_METHOD_SPELLINGS = {
    "ssn card": "ssn_card", "social security card": "ssn_card", "ssn_card": "ssn_card",
    "birth certificate": "birth_certificate", "birth_certificate": "birth_certificate",
    "naturalization": "naturalization_papers", "naturalization papers": "naturalization_papers",
    "naturalization_papers": "naturalization_papers",
    "passport": "passport",
    "drivers license": "drivers_license", "driver's license": "drivers_license", "driver license": "drivers_license",
    "drivers_license": "drivers_license", "driver_license": "drivers_license",
    "state id": "state_id", "state_id": "state_id", "id card": "state_id",
    "self declaration": "self_declaration", "self-declaration": "self_declaration",
    "self_declaration": "self_declaration", "application": "self_declaration",
}

_SSN_WORDS = re.compile(r"\bssn\b|social security (?:number|card|#|no\.?)|\bss#", re.IGNORECASE)
_DOB_WORDS = re.compile(r"date of birth|\bdob\b|\bbirth\b|\bage\b", re.IGNORECASE)
_NOT_IDENTITY = re.compile(r"benefit|award|income|letter|statement|verification of|\bvoi\b", re.IGNORECASE)
_LICENSE = re.compile(r"driver", re.IGNORECASE)
_ID_CARD = re.compile(r"identification card|\bid card\b|non-?driver", re.IGNORECASE)

NOTE_SOURCE = "scan"

# HOME takes HUD's proofs of identity without being a HUD file for any
# other rule (no 50059, no 9887 package), so it is named here, not in
# program.is_hud_property.
_HOME = re.compile(r"\bhome\b", re.IGNORECASE)


def _wants_hud_proofs(program: str | None) -> bool:
    return is_hud_property(program) or bool(program and _HOME.search(program))


def concepts_of(row: dict) -> frozenset:
    """Which of dob / ssn a checklist row asks about; empty for other rows."""
    text = f"{row.get('label') or ''} {row.get('item_code') or ''}".replace("_", " ")
    if _NOT_IDENTITY.search(text):
        return frozenset()
    out = set()
    if _SSN_WORDS.search(text):
        out.add("ssn")
    if _DOB_WORDS.search(text):
        out.add("dob")
    return frozenset(out)


def normalise_method(value) -> str | None:
    """One of the method tokens for any spelling Cartograph may send."""
    if not value:
        return None
    t = re.sub(r"\s+", " ", str(value).strip().lower().replace("-", " ").replace("_", " "))
    return _METHOD_SPELLINGS.get(t) or _METHOD_SPELLINGS.get(t.replace(" ", "_")) or (
        t.replace(" ", "_") if t.replace(" ", "_") in PROVES else None)


def accepted_methods(verification_methods, program: str | None) -> dict[str, tuple[str, ...]]:
    """The proofs each concept accepts for this case: the request's lists
    when it sends them, else the defaults for the program."""
    out: dict[str, tuple[str, ...]] = {}
    if isinstance(verification_methods, dict):
        for concept in ("dob", "ssn"):
            raw = verification_methods.get(concept)
            if isinstance(raw, (list, tuple)):
                methods = tuple(m for m in (normalise_method(v) for v in raw) if m)
                if methods:
                    out[concept] = methods
    defaults = DEFAULT_METHODS["hud" if _wants_hud_proofs(program) else "other"]
    for concept in ("dob", "ssn"):
        out.setdefault(concept, defaults[concept])
    return out


def method_of_group(group) -> str | None:
    label = _split_suffix(group.document_type or "")[0]
    method = METHOD_OF_LABEL.get(label)
    if method == "drivers_license":
        text = group.combined_text or ""
        if _ID_CARD.search(text) and not _LICENSE.search(text):
            return "state_id"
    return method


def _identity_groups(extraction) -> list:
    return [g for g in (getattr(extraction, "document_groups", None) or [])
            if g.category != "ignore" and method_of_group(g)]


def _name_words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z]{2,}", (text or "").lower())}


def _names_match(a: str | None, b: str | None) -> bool:
    """Two names are one person when both parts agree, or the only part
    given agrees (a household shares surnames)."""
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return False
    shared = wa & wb
    return len(shared) >= 2 or (bool(shared) and (len(wa) == 1 or len(wb) == 1))


def _dob_patterns(iso: str | None) -> list[re.Pattern]:
    if not iso:
        return []
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return []
    months = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
              "october", "november", "december")
    forms = [
        rf"\b0?{d.month}[/\-.]0?{d.day}[/\-.]{d.year}\b",
        rf"\b0?{d.month}[/\-.]0?{d.day}[/\-.]{d.year % 100:02d}\b",
        rf"\b{d.year}-{d.month:02d}-{d.day:02d}\b",
        rf"\b{months[d.month - 1][:3]}[a-z]*\.?\s+0?{d.day},?\s+{d.year}\b",
        rf"\b0?{d.day}\s+{months[d.month - 1][:3]}[a-z]*\.?\s+{d.year}\b",
    ]
    return [re.compile(f, re.IGNORECASE) for f in forms]


def _member_name(member: dict | None) -> str:
    if not member:
        return ""
    return " ".join(p for p in (member.get("first_name"), member.get("last_name")) if p)


def _member_for(row: dict, members: list[dict]) -> dict | None:
    label = row.get("subject_label")
    if not label:
        return None
    best, best_shared = None, 0
    for m in members:
        shared = len(_name_words(label) & _name_words(_member_name(m)))
        if shared > best_shared and _names_match(label, _member_name(m)):
            best, best_shared = m, shared
    return best


def _attribution(group, member: dict | None, subject_label: str | None) -> tuple[str, float] | None:
    """How a group belongs to a member: by name, by the member's number or
    birth date printed on the page, or not at all."""
    name = _member_name(member) or subject_label
    text = group.combined_text or ""
    last4 = (member or {}).get("ssn_last4")
    by_number = bool(last4) and re.search(rf"\b\d{{3}}[-\s]?\d{{2}}[-\s]?{re.escape(str(last4))}\b", text) is not None
    by_dob = any(p.search(text) for p in _dob_patterns((member or {}).get("date_of_birth")))
    by_name = _names_match(group.person_name, name)
    if group.person_name and not by_name and not (by_number or by_dob):
        return None
    if by_name and (by_number or by_dob):
        return "name and number", 0.95
    if by_name:
        return "name", 0.85
    if by_number or by_dob:
        return "number", 0.8
    return None


def _document_id_for(page: int | None, spans: list[dict] | None):
    for span in spans or []:
        first, last = span.get("first_page"), span.get("last_page")
        if page is not None and first and last and first <= page <= last:
            return span.get("job_document_id")
    return None


def _declaration_pages(extraction) -> list[int]:
    """Where the household itself stated who it is: the application or
    questionnaire, else the certification form."""
    groups = [g for g in (getattr(extraction, "document_groups", None) or []) if g.category != "ignore"]
    declared = [g for g in groups if family_of(g.document_type) == FAMILY_DECLARATION]
    if not declared:
        declared = [g for g in groups if is_current_certification_form(g.document_type)]
    return sorted({p for g in declared for p in g.pages})


def _page_span(pages: list[int]) -> str:
    """Pages as runs: 6-11, 18."""
    if not pages:
        return ""
    runs: list[list[int]] = [[pages[0], pages[0]]]
    for p in pages[1:]:
        if p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in runs)


_PRETTY = {
    "ssn_card": "Social Security card", "birth_certificate": "birth certificate", "passport": "passport",
    "naturalization_papers": "naturalization papers", "drivers_license": "driver's license", "state_id": "state ID",
    "other_identity_document": "identity document", "self_declaration": "self-declaration",
}
_CONCEPT = {"dob": "DOB", "ssn": "SSN"}


def identity_rows(rows: list[dict], extraction, members: list[dict] | None, document_spans: list[dict] | None,
                  verification_methods=None, program: str | None = None) -> list[dict]:
    """One entry per DOB / SSN checklist row, judged per member."""
    members = members or []
    id_groups = _identity_groups(extraction)
    declaration_pages = _declaration_pages(extraction)
    out: list[dict] = []

    # Pages that belong to nobody the engine can name, mentioned on the
    # rows they might have satisfied.
    def attributed_to_someone(group) -> bool:
        return any(_attribution(group, m, None) for m in members)
    orphans = [g for g in id_groups if not attributed_to_someone(g)]

    for row in rows:
        concepts = concepts_of(row)
        if not concepts:
            continue
        program_name = (row.get("funding_program") or program or "").strip() or None
        accepted = accepted_methods(verification_methods, program_name)
        member = _member_for(row, members)
        subject = row.get("subject_label")
        targets = [member] if member or subject else (members or [None])
        proofs: list[dict] = []
        notes: list[str] = []
        for concept in sorted(concepts):
            best = None
            for target in targets:
                for g in id_groups:
                    method = method_of_group(g)
                    if concept not in PROVES[method]:
                        continue
                    how = _attribution(g, target, subject)
                    if not how:
                        continue
                    ok = method in accepted[concept]
                    cand = (ok, how[1], g, method, how[0])
                    if best is None or cand[:2] > best[:2]:
                        best = cand
            if best:
                ok, conf, g, method, how = best
                pages = sorted(g.pages)
                proofs.append({"concept": concept, "document_type": method, "accepted": ok,
                               "document_id": _document_id_for(pages[0] if pages else None, document_spans),
                               "pages": pages, "confidence": round(conf, 2)})
                notes.append(f"{_CONCEPT[concept]}: {_PRETTY[method]} page {_page_span(pages)}, matched by {how}"
                             + ("." if ok else f" (not an accepted proof for {program_name or 'this program'})."))
                continue
            declared = any(((t or {}).get("date_of_birth") if concept == "dob" else (t or {}).get("ssn_last4"))
                           for t in targets if t)
            if declared and "self_declaration" in accepted[concept]:
                proofs.append({"concept": concept, "document_type": "self_declaration", "accepted": True,
                               "document_id": _document_id_for(declaration_pages[0] if declaration_pages else None, document_spans),
                               "pages": declaration_pages, "confidence": 0.6})
                notes.append(f"{_CONCEPT[concept]}: self-declared on the application"
                             + (f", page {_page_span(declaration_pages)}." if declaration_pages else "."))
                continue
            hint = next((g for g in orphans if concept in PROVES[method_of_group(g)]), None)
            notes.append(f"{_CONCEPT[concept]}: no accepted proof in the packet"
                         + (f" (for {subject})" if subject else "")
                         + (f"; page {_page_span(sorted(hint.pages))} shows a {_PRETTY[method_of_group(hint)]} "
                            f"that could not be matched to a member" if hint else "") + ".")
        found = len(proofs) == len(concepts)
        pages = sorted({p for pr in proofs for p in pr["pages"]})
        confidence = min((pr["confidence"] for pr in proofs), default=0.8)
        out.append({
            "finding_id": row["finding_id"],
            "household_member_id": row.get("subject_id"),
            "member_ref": (member or {}).get("ref"),
            "found": found,
            "accepted": all(pr["accepted"] for pr in proofs) if found else False,
            "document_type": proofs[0]["document_type"] if len(proofs) == 1 and len(concepts) == 1 else None,
            "document_id": proofs[0]["document_id"] if proofs else None,
            "pages": pages,
            "proofs": proofs,
            "confidence": round(confidence, 2),
            "note": " ".join(notes),
            "note_source": NOTE_SOURCE,
        })
    return out
