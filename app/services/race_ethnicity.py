"""Race and ethnicity per household member, from the packet's own forms.

Two sources state them. The HUD Race and Ethnic Data Reporting Form
(HUD-27061-H, one per member, or a state variant such as Nevada's Exhibit
G that lists the members as columns of one grid) is the household's own
self-certification: ticked boxes, usually by hand, which OCR drops as
often as it keeps. The HUD 50059's Section C prints what the owner
transcribed from those forms as codes in fields 39 "Race" and 40
"Ethnic". The form is read from the page image when there is one and
wins over the 50059 columns; the 50059 stands when the packet has no
form, or the form is blank.

Nothing is ever inferred. A box that is not ticked is unknown, and the
consumer receives null, not a guess — the values are reported to HUD and
"locked" once staff verify them.

The vocabulary is the consumer's: the atomic categories below are the
exact strings Cartograph stores, and `race_out` folds a member's ticks
into the one string its race column takes (a single category, a listed
pair, or "Other Multi-Racial").
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from app.core.config import Settings
from app.schemas.extraction import DocumentGroup, HouseholdDemographics, HouseholdMember
from app.services.name_reconciler import _name_similarity

logger = logging.getLogger(__name__)

RACE_FORM_LABEL = "HUD Race and Ethnic Data Form"

# --- the consumer's vocabulary --------------------------------------------

WHITE = "White"
BLACK = "Black/African American"
ASIAN = "Asian"
AIAN = "American Indian/Alaska Native"
NHPI = "Native Hawaiian/Other Pacific Islander"
OTHER = "Other"
DECLINED = "Decline to Answer"
MULTI = "Other Multi-Racial"

ASIAN_DETAILS = (
    "Asian - Asian Indian", "Asian - Chinese", "Asian - Filipino", "Asian - Japanese",
    "Asian - Korean", "Asian - Vietnamese", "Asian - Other Asian",
)
NHPI_DETAILS = (
    "Native Hawaiian/Other Pacific Islander - Native Hawaiian",
    "Native Hawaiian/Other Pacific Islander - Guamanian or Chamorro",
    "Native Hawaiian/Other Pacific Islander - Samoan",
    "Native Hawaiian/Other Pacific Islander - Other Pacific Islander",
)
RACES = (WHITE, BLACK, ASIAN, *ASIAN_DETAILS, AIAN, NHPI, *NHPI_DETAILS, OTHER, DECLINED)

# The pairs Cartograph names; any other mix of two or more groups is MULTI.
_PAIRS = {
    frozenset((AIAN, WHITE)): "American Indian/Alaska Native & White",
    frozenset((ASIAN, WHITE)): "Asian & White",
    frozenset((BLACK, WHITE)): "Black/African American & White",
    frozenset((AIAN, BLACK)): "American Indian/Alaska Native & Black/African American",
}

HISPANIC = "Hispanic or Latino"
NOT_HISPANIC = "Not Hispanic or Latino"
ETHNICITIES = (HISPANIC, NOT_HISPANIC, DECLINED)

# Printed labels, in the order they must be tried: a detail before its
# group, "not hispanic" before "hispanic", "other asian" before "other".
# Matching is on a lower-cased, punctuation-free form of the label.
_RACE_ALIASES: tuple[tuple[str, str], ...] = (
    ("asian indian", "Asian - Asian Indian"),
    ("chinese", "Asian - Chinese"),
    ("filipino", "Asian - Filipino"),
    ("japanese", "Asian - Japanese"),
    ("korean", "Asian - Korean"),
    ("vietnamese", "Asian - Vietnamese"),
    ("other asian", "Asian - Other Asian"),
    ("native hawaiian or other pacific islander", NHPI),
    ("native hawaiian other pacific islander", NHPI),
    ("native hawaiian pacific islander", NHPI),
    ("other pacific islander", NHPI_DETAILS[3]),
    ("guamanian", NHPI_DETAILS[1]),
    ("chamorro", NHPI_DETAILS[1]),
    ("samoan", NHPI_DETAILS[2]),
    ("native hawaiian", NHPI_DETAILS[0]),
    ("pacific islander", NHPI),
    ("hawaiian", NHPI_DETAILS[0]),
    ("american indian", AIAN),
    ("alaska native", AIAN),
    ("alaskan native", AIAN),
    ("native american", AIAN),
    ("african american", BLACK),
    ("black", BLACK),
    ("white", WHITE),
    ("caucasian", WHITE),
    ("asian", ASIAN),
    ("choose not to respond", DECLINED),
    ("chose not to respond", DECLINED),
    ("decline", DECLINED),
    ("declined", DECLINED),
    ("prefer not", DECLINED),
    ("did not respond", DECLINED),
    ("no response", DECLINED),
    ("multi", MULTI),
    ("two or more", MULTI),
    ("other", OTHER),
)
# HUD 50059 field 39 codes; some software prints letters.
_RACE_CODES = {"1": AIAN, "2": ASIAN, "3": BLACK, "4": NHPI, "5": WHITE,
               "W": WHITE, "B": BLACK, "A": ASIAN, "I": AIAN, "P": NHPI}
_ETHNICITY_CODES = {"1": HISPANIC, "2": NOT_HISPANIC}

_PLAIN_RE = re.compile(r"[^a-z0-9 ]+")


def _plain(value) -> str:
    return re.sub(r"\s+", " ", _PLAIN_RE.sub(" ", str(value or "").lower())).strip()


def normalize_race(value) -> str | None:
    """One printed race label, or a 50059 code, as the consumer's string.

    A value that is none of them is dropped (None), never passed through:
    the consumer validates the column and a stray label would fail the
    whole member row.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if raw in RACES or raw == MULTI:
        return raw
    if raw.upper() in _RACE_CODES and len(raw) == 1:
        return _RACE_CODES[raw.upper()]
    plain = _plain(raw)
    if not plain:
        return None
    for alias, label in _RACE_ALIASES:
        if alias in plain:
            return label
    return None


def normalize_races(values) -> list[str] | None:
    """A member's race ticks as a list of distinct consumer strings, or
    None when nothing usable was stated. Accepts a list, a single label,
    a 50059 multi-digit code ("15" = two races) or a string joining
    several labels with "&", "and", ";" or ",". The slash is never a
    delimiter: it sits inside "Black/African American"."""
    if values is None:
        return None
    if isinstance(values, str):
        raw = values.strip()
        if re.fullmatch(r"[1-5]{2,}", raw):
            items = list(raw)
        else:
            items = [p for p in re.split(r"\s*[;,|]\s*|\s*&\s*|\s+and\s+", raw) if p]
    elif isinstance(values, (list, tuple, set)):
        items = list(values)
    else:
        items = [values]
    out: list[str] = []
    for item in items:
        label = normalize_race(item)
        if label and label not in out:
            out.append(label)
    return out or None


def normalize_ethnicity(value) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw in ETHNICITIES:
        return raw
    if raw in _ETHNICITY_CODES:
        return _ETHNICITY_CODES[raw]
    plain = _plain(raw)
    if not plain:
        return None
    if any(w in plain for w in ("decline", "choose not", "chose not", "prefer not", "no response", "did not respond")):
        return DECLINED
    if re.search(r"\b(?:not|non)\b.*(?:hispanic|latino)|(?:hispanic|latino).*\bnot\b", plain):
        return NOT_HISPANIC
    if "hispanic" in plain or "latino" in plain or "latina" in plain:
        return HISPANIC
    return None


def _group_of(label: str) -> str:
    """The top-level category a tick belongs to."""
    if label in ASIAN_DETAILS:
        return ASIAN
    if label in NHPI_DETAILS:
        return NHPI
    return label


def race_out(values) -> str | None:
    """The one string Cartograph's race column takes for a member's ticks.

    One tick is itself (a detail tick stays a detail; a detail with its
    own group ticked too is the detail). Several ticks within one group
    are that group. Two groups that Cartograph names as a pair are that
    pair; anything else across groups is "Other Multi-Racial". "Decline to
    Answer" counts only on its own.
    """
    ticks = normalize_races(values) or []
    if not ticks:
        return None
    if MULTI in ticks:
        return MULTI
    if len(ticks) > 1:
        ticks = [t for t in ticks if t != DECLINED] or ticks
    by_group: dict[str, list[str]] = {}
    for t in ticks:
        by_group.setdefault(_group_of(t), []).append(t)
    if len(by_group) == 1:
        group, members = next(iter(by_group.items()))
        details = [m for m in members if m != group]
        if len(details) == 1:
            return details[0]
        return group
    if len(by_group) == 2:
        pair = _PAIRS.get(frozenset(by_group))
        if pair:
            return pair
    return MULTI


def ethnicity_out(value) -> str | None:
    return normalize_ethnicity(value)


# --- reading the forms -----------------------------------------------------

RACE_FORM_PROMPT = """\
You read HUD Race and Ethnic Data Reporting Forms (form HUD-27061-H) and the state
variants of it (for example Nevada's "Exhibit G Race and Ethnic Data Reporting Form",
which lists the household members as the columns of one grid, one row per category).

Report, for every person the page is about, exactly which boxes are ticked. A tick is
an X, a check mark, a filled or shaded box, a circled option, or handwriting in the
box. An empty box is NOT ticked. Never infer anything from a name, a photo, a
language, or the other members.

Return ONLY JSON:
{"people": [
  {"name": "name exactly as written on the form, or null",
   "isHeadOfHousehold": true|false,
   "ethnicity": "the ticked ethnicity label as printed, or null when none is ticked",
   "races": ["each ticked race label as printed", ...],
   "declined": true|false,
   "gender": "M" | "F" | null,
   "disabled": "Y" | "N" | null}
]}

Rules:
- HUD-27061-H names one person per page: "Name of Household Member". When that line is
  blank the page is about the "Name of Head of Household" person; set isHeadOfHousehold
  true and name to the head's name.
- A grid form (Nevada Exhibit G) names each person in a column heading under
  "Household Members Name(s)"; return one entry per named column, with that column's
  ticks only. Skip empty columns.
- "declined" is true when a "Choose not to respond" / "Decline to answer" box is ticked.
- "races" holds every ticked race box, including a sub-category such as "Asian -
  Chinese" when the form prints one. Empty list when none is ticked.
- gender and disabled only when the form itself asks ("Is Head of Household a woman",
  "Is Head of Household disabled", a Sex box): "F" when the woman box is ticked Yes,
  "M" when ticked No; otherwise null. Likewise disabled "Y"/"N".
- A page with no ticks at all still lists its person with ethnicity null, races [],
  declined false.
"""

_MAX_FORM_PAGES = 12
_NAME_THRESHOLD = 0.72


def _member_names(m: HouseholdMember) -> list[str]:
    full = " ".join(p for p in (m.FirstName, m.LastName) if p).strip()
    with_middle = " ".join(p for p in (m.FirstName, m.MiddleName, m.LastName) if p).strip()
    return [n for n in {full, with_middle} if n]


def _match_member(person: dict, members: list[HouseholdMember]) -> HouseholdMember | None:
    name = (person.get("name") or "").strip()
    head = next((m for m in members if (m.head or "").strip().upper() == "H"), None)
    if not name:
        return head if person.get("isHeadOfHousehold") else None
    best, best_score = None, 0.0
    for m in members:
        score = max((_name_similarity(name, n) for n in _member_names(m)), default=0.0)
        if score > best_score:
            best, best_score = m, score
    if best is not None and best_score >= _NAME_THRESHOLD:
        return best
    if person.get("isHeadOfHousehold") and head is not None:
        return head
    return None


def _read_page(page: int, text: str, image_path: str | None, settings: Settings) -> list[dict]:
    from app.services.llm_service import call_llm_json, call_llm_vision_json

    user = f"Packet page {page}. Report the ticked boxes for each person on this form."
    if image_path:
        result = call_llm_vision_json(RACE_FORM_PROMPT, user, [str(image_path)], settings, label="Race form")
    else:
        result = call_llm_json(RACE_FORM_PROMPT, f"{user}\n\nOCR text of the page:\n{text}", settings)
    people = result.get("people") if isinstance(result, dict) else None
    out = []
    for p in people or []:
        if isinstance(p, dict):
            p["page"] = page
            out.append(p)
    return out


def read_race_and_ethnicity(
    household: HouseholdDemographics | None,
    groups: list[DocumentGroup],
    page_texts: list[dict],
    settings: Settings,
) -> list[dict]:
    """Read every race form in the packet and set race / ethnicity (and,
    where the form asks, gender and disability) on the matching members.

    Returns the notes a reviewer needs, as {"code", "text", "name",
    "pages"}: a person on a form who matches no member, or two forms
    disagreeing on one member. Never raises; a page that cannot be read
    is skipped.
    """
    if not household or not household.houseHold:
        return []
    members = household.houseHold
    pages: list[int] = []
    for g in groups:
        if g.document_type == RACE_FORM_LABEL and g.category != "ignore":
            pages.extend(p for p in g.pages if p not in pages)
    if not pages:
        return []
    by_page = {pt.get("page"): pt for pt in page_texts or []}
    pages = pages[:_MAX_FORM_PAGES]
    logger.info("Race/ethnicity: reading form page(s) %s", pages)

    def _one(pn: int) -> tuple[int, list[dict]]:
        pt = by_page.get(pn) or {}
        try:
            return pn, _read_page(pn, pt.get("text") or "", pt.get("image_path"), settings)
        except Exception:
            logger.exception("Race/ethnicity: page %d could not be read", pn)
            return pn, []

    with ThreadPoolExecutor(max_workers=min(4, len(pages))) as pool:
        results = dict(pool.map(_one, pages))

    notes: list[dict] = []
    stated: dict[int, int] = {}  # id(member) -> page that set it
    for pn in pages:
        for person in results.get(pn, []):
            races = normalize_races(person.get("races"))
            if person.get("declined") and not races:
                races = [DECLINED]
            ethnicity = normalize_ethnicity(person.get("ethnicity"))
            m = _match_member(person, members)
            if m is None:
                if races or ethnicity:
                    who = person.get("name") or "an unnamed person"
                    notes.append({
                        "code": "RACE_FORM_MEMBER_UNMATCHED", "name": who, "pages": [pn],
                        "text": f"Race and Ethnic Data Form on page {pn} is for {who}, who matches no "
                                f"household member on the certification; its race and ethnicity were not applied",
                    })
                continue
            if not races and not ethnicity:
                logger.info("Race/ethnicity: page %d form for %s has no ticks", pn, _member_names(m)[0])
                continue
            if id(m) in stated:
                if (races and m.race and races != m.race) or (ethnicity and m.ethnicity and ethnicity != m.ethnicity):
                    who = _member_names(m)[0]
                    notes.append({
                        "code": "RACE_FORM_CONFLICT", "name": who, "pages": [stated[id(m)], pn],
                        "text": f"Race and Ethnic Data Forms on pages {stated[id(m)]} and {pn} state different "
                                f"race or ethnicity for {who}; page {stated[id(m)]} was used",
                    })
                continue
            stated[id(m)] = pn
            if races:
                m.race = races
            if ethnicity:
                m.ethnicity = ethnicity
            if not m.gender and person.get("gender") in ("M", "F"):
                m.gender = person["gender"]
            if not m.disabled and person.get("disabled") in ("Y", "N"):
                m.disabled = person["disabled"]
            logger.info("Race/ethnicity: %s — race %s, ethnicity %s (form page %d)",
                        _member_names(m)[0], races, ethnicity, pn)
    for note in notes:
        logger.warning("Race/ethnicity: %s", note["text"])
    return notes
