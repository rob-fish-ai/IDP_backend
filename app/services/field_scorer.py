"""Field-level scoring engine for LLM-only extraction pipeline.

Extraction stage scores based on whether the LLM returned a value:
  - Populated with valid-looking content: 0.85 (high — LLM found it)
  - Populated but looks suspicious:      0.60 (medium — might be wrong)
  - Null / empty:                         0.00 (absent — needs manual entry)

Stage weights live in schemas/scoring.py. The findings stage carries the most
weight because it is the only one that compares the extraction against the
document's own account of itself rather than against a format or a range.

Green means "found in the source document". A value no source verification
examined is capped below green — unconfirmed is its own state, distinct from
both confirmed-good and found-wrong.
"""

from __future__ import annotations

import logging
import re

from app.schemas.extraction import Finding
from app.schemas.scoring import (
    UNVERIFIED_CEILING,
    ExtractionScoreSummary,
    FieldScore,
    RecordScoreCard,
    ScoreFlag,
    StageScore,
)
from app.services.doc_taxonomy import is_current_certification_form, is_previous_certification
from app.services.findings import dispute_strength, slug
# The scorer's bounds and the annualizer's arithmetic have to agree about
# how long a pay period is, so both read the same multiplier table.
from app.services.income_calculator import get_frequency_multiplier

logger = logging.getLogger(__name__)

# Extraction-stage scores for LLM output
_POPULATED = 0.85       # LLM returned a value
_SUSPICIOUS = 0.60      # value looks odd (too long, contains HTML, etc.)
_ABSENT = 0.00          # field is null


def _is_suspicious(value: str) -> bool:
    """Check if an extracted value looks like garbage."""
    if len(value) > 200:
        return True
    if any(tag in value.lower() for tag in ("<td", "<tr", "</t", "&amp;", "colspan")):
        return True
    if re.match(r"^\d{5,}$", value):  # long number that's not money
        return True
    return False


class RecordScorer:
    """Builder for a RecordScoreCard."""

    def __init__(self, record_type: str, label: str | None = None):
        self._record_type = record_type
        self._label = label
        self._fields: dict[str, FieldScore] = {}

    def score_field(
        self,
        field_name: str,
        value: str | None,
        *,
        extraction: float | None = None,
        reason: str | None = None,
    ) -> None:
        """Score a field. If extraction is not provided, auto-detect from value."""
        if extraction is None:
            if value is None:
                extraction = _ABSENT
                reason = reason or "Not extracted"
            elif _is_suspicious(value):
                extraction = _SUSPICIOUS
                reason = reason or "Value looks suspicious — verify"
            else:
                extraction = _POPULATED
                reason = reason or "Extracted"

        fs = FieldScore(
            field_name=field_name,
            value=str(value) if value is not None else None,
            stages=[StageScore(stage="extraction", score=extraction, reason=reason)],
        )
        fs.recompute()
        self._fields[field_name] = fs

    def build(self) -> RecordScoreCard:
        card = RecordScoreCard(
            record_type=self._record_type,
            record_label=self._label,
            fields=list(self._fields.values()),
        )
        card.recompute()
        return card


def update_field_score(
    card: RecordScoreCard,
    field_name: str,
    *,
    stage: str,
    score: float,
    reason: str | None = None,
) -> None:
    """Add a stage score to an existing field on a card, then recompute."""
    for fs in card.fields:
        if fs.field_name == field_name:
            fs.stages.append(StageScore(stage=stage, score=score, reason=reason))
            fs.recompute()
            return
    fs = FieldScore(
        field_name=field_name,
        stages=[StageScore(stage=stage, score=score, reason=reason)],
    )
    fs.recompute()
    card.fields.append(fs)


# ---------------------------------------------------------------------------
# Score card generation from final Pydantic data
# ---------------------------------------------------------------------------

def score_pydantic_records(
    household=None,
    certification_info=None,
    income=None,
    assets=None,
) -> list[RecordScoreCard]:
    """Generate score cards from final extraction data.

    Auto-detects extraction confidence from field values:
    populated = 0.85, suspicious = 0.60, null = 0.00.
    """
    cards: list[RecordScoreCard] = []

    # Household members
    if household and household.houseHold:
        for m in household.houseHold:
            name = f"{m.FirstName or ''} {m.LastName or ''}".strip()
            scorer = RecordScorer("household_member", name or "Unknown")
            for field in ("FirstName", "LastName", "DOB", "socialSecurityNumber",
                           "relationship", "disabled", "student"):
                val = getattr(m, field, None)
                scorer.score_field(field, str(val) if val else None)
            cards.append(scorer.build())

    # Certification info
    if certification_info:
        ci = certification_info
        scorer = RecordScorer("certification", "CertificationInfo")
        for field in ("certificationType", "effectiveDate", "unitNumber", "grossRent",
                       "tenantRent", "utilityAllowance", "householdIncome", "isSigned"):
            val = getattr(ci, field, None)
            scorer.score_field(field, str(val) if val else None)
        cards.append(scorer.build())

    # Income
    if income and income.sourceIncome:
        for vi in income.sourceIncome.verificationIncome:
            label = f"{vi.memberName or ''} — {vi.sourceName or ''}".strip(" —")
            scorer = RecordScorer("income", label or "Unknown")
            for field in ("sourceName", "memberName", "selfDeclaredAmount",
                           "rateOfPay", "frequencyOfPay",
                           "hoursPerPayPeriod", "incomeType", "employmentStatus",
                           "ytdAmount", "hireDate", "terminationDate"):
                val = getattr(vi, field, None)
                scorer.score_field(field, str(val) if val else None)
            card = scorer.build()
            card.source_pages = list(getattr(vi, "sourcePages", None) or [])
            card.verification_status = getattr(vi, "verificationStatus", None)
            _mark_redacted(card, getattr(vi, "evidence", None))
            cards.append(card)

    # Assets
    # Include the last 4 of the account number in the label so two distinct
    # accounts at the same bank/type don't collapse together in cross-doc
    # consistency scoring (which groups records by label).
    if assets:
        for a in assets.assetInformation:
            acct_tail = ""
            if a.accountNumber and len(a.accountNumber) >= 4:
                acct_tail = f" #{a.accountNumber[-4:]}"
            label = (
                f"{a.assetOwner or ''} — {a.accountType or ''}{acct_tail}"
            ).strip(" —")
            scorer = RecordScorer("asset", label or "Unknown")
            # selfDeclaredAmount belongs here even though it is not always
            # populated: an asset carries its value in currentBalance when
            # third-party verified and in selfDeclaredAmount when
            # self-certified. Scoring only one of them made the business
            # rule below — which excuses the empty sibling — unable to see
            # the field it keys on, so every self-certified asset scored a
            # false RED for a balance it was never going to have.
            for field in (
                "accountType", "currentBalance", "selfDeclaredAmount",
                "incomeAmount", "assetOwner",
            ):
                val = getattr(a, field, None)
                scorer.score_field(field, str(val) if val else None)
            card = scorer.build()
            card.source_pages = list(getattr(a, "sourcePages", None) or [])
            card.verification_status = getattr(a, "verificationStatus", None)
            _mark_redacted(card, getattr(a, "evidence", None))
            cards.append(card)

    return cards


def _mark_redacted(card: RecordScoreCard, evidence: dict | None) -> None:
    """A field the page shows blacked out is not available, not missed."""
    for fs in card.fields:
        if fs.value is None and (evidence or {}).get(fs.field_name) == "redacted on page":
            fs.mark_na("Redacted on the page — verify from the original document")


# ---------------------------------------------------------------------------
# Stage 1b: Source verification — OCR quality + value-in-text check
# ---------------------------------------------------------------------------

def score_source_verification(
    cards: list[RecordScoreCard],
    document_groups: list,
    ocr_quality: dict[int, dict],
) -> None:
    """Check whether extracted values appear in the pages they were read from.

    Tiers, in order of what a hit means:
      1.00  on the record's own pages — the value is what its document says
      0.85  on those pages, but the record is the household's own declaration
            (declared_only): nothing third-party carries it   [capped yellow]
      0.85  short number found without its field label nearby  [capped yellow]
      0.85  income/asset value found only on the certification form
            — copied from what the household declared          [capped yellow]
      0.70  found elsewhere in the packet's evidence documents [capped yellow]
      0.50  not found; the record's pages read cleanly
      0.30  not found; the record's pages read poorly

    Before this, one text pool per record type was searched with a bare
    substring: "953.00" verified against a BNC reference number, "82.05"
    against "1,082.05", "58.00" against the income limit "58,200", and a
    figure copied from the certification scored the same 1.0 as one read
    from a benefit letter. The pools were literal label lists that had
    drifted from the taxonomy, and the fallback pool was mostly compliance
    paperwork.
    """
    if not document_groups:
        return

    # Per-page text, from the OCR quality map (which carries the text the
    # extractor consumed) with the groups' page-marked text as fallback.
    page_text: dict[int, str] = {}
    for pn, q in ocr_quality.items():
        t = q.get("text") if isinstance(q, dict) else None
        if t:
            page_text[pn] = t.lower()
    for g in document_groups:
        for pn, t in _split_group_pages(g).items():
            page_text.setdefault(pn, t.lower())
    page_flag = {pn: (q.get("flag") if isinstance(q, dict) else None) or "green" for pn, q in ocr_quality.items()}

    classes: dict[int, str] = {}
    for g in document_groups:
        if g.category == "ignore":
            continue
        cls = _evidence_class(g.document_type, g.category)
        for pn in g.pages:
            classes[pn] = cls
    cert_pages = sorted(pn for pn, c in classes.items() if c == "cert")
    household_pages = sorted(pn for pn, c in classes.items() if c in ("cert", "household"))
    evidence_pages = sorted(pn for pn, c in classes.items() if c != "compliance")
    class_pages = {
        "certification": cert_pages,
        "household_member": household_pages,
        "income": sorted(pn for pn, c in classes.items() if c in ("income", "cert", "household")),
        "asset": sorted(pn for pn, c in classes.items() if c in ("asset", "cert", "household")),
    }

    _SKIP_SOURCE_VERIFY = {("certification", "certificationType")}
    _DECLARED_FIELDS = {"selfDeclaredAmount", "declaredAnnualAmount"}

    def _texts(pages: list[int]) -> list[str]:
        return [page_text.get(pn, "") for pn in pages]

    for card in cards:
        own = [pn for pn in (card.source_pages or []) if pn in page_text] or class_pages.get(card.record_type, [])
        if not own and not evidence_pages:
            continue
        own_flag = "green"
        for pn in own:
            f = page_flag.get(pn, "green")
            if f == "red" or (f == "yellow" and own_flag == "green"):
                own_flag = f
        declared_only = card.verification_status == "declared_only"
        own_texts = _texts(own)

        for fs in card.fields:
            if fs.value is None:
                continue
            if (card.record_type, fs.field_name) in _SKIP_SOURCE_VERIFY:
                continue
            if fs.field_name in _VOCAB_SYNONYMS:
                # A picklist value is the engine's word for what the form
                # says; it is verified through its synonyms and otherwise left
                # unconfirmed rather than scored as an OCR failure.
                if _vocab_in(fs.field_name, fs.value, own_texts):
                    fs.stages.append(StageScore(stage="source_verification", score=1.0,
                                                reason="Verified in source document"))
                    fs.recompute()
                continue

            hit = _find_value(fs.value, fs.field_name, own_texts)
            if (fs.field_name in _DECLARED_FIELDS and card.record_type in ("income", "asset")
                    and hit != "strong"
                    and _find_value(fs.value, fs.field_name, _texts(household_pages)) == "strong"):
                # A declared amount lives on the declaration, not on the
                # record's source document: the 50059's $886 merged into the
                # bank-verified savings account is verified when the 50059
                # prints it, and "found elsewhere" would be the wrong verdict.
                stage = StageScore(stage="source_verification", score=1.0,
                                   reason="Declared on the certification form or questionnaire")
            elif hit == "strong" and declared_only and card.record_type in ("income", "asset"):
                stage = StageScore(stage="source_verification", score=0.85, ceiling=UNVERIFIED_CEILING,
                                   reason="Declared by the household; no verification document carries it")
            elif hit == "strong":
                stage = StageScore(stage="source_verification", score=1.0, reason="Verified in source document")
            elif hit == "weak":
                stage = StageScore(stage="source_verification", score=0.85, ceiling=UNVERIFIED_CEILING,
                                   reason="Short value found without its field label nearby — weak match")
            elif card.record_type in ("income", "asset") and _find_value(fs.value, fs.field_name, _texts(cert_pages)) == "strong":
                stage = StageScore(stage="source_verification", score=0.85, ceiling=UNVERIFIED_CEILING,
                                   reason="Found only on the certification form, not in this record's documents")
            elif _find_value(fs.value, fs.field_name, _texts(evidence_pages)) is not None:
                stage = StageScore(stage="source_verification", score=0.70, ceiling=UNVERIFIED_CEILING,
                                   reason="Found elsewhere in the packet, not in this record's documents")
            elif own_flag == "green":
                stage = StageScore(stage="source_verification", score=0.50,
                                   reason="Not found in source text — verify manually")
            else:
                stage = StageScore(stage="source_verification", score=0.30,
                                   reason=f"Not found + poor OCR ({own_flag}) — unreliable")
            fs.stages.append(stage)
            fs.recompute()

    for card in cards:
        card.recompute()


_PAGE_MARK_RE = re.compile(r"--- Page (\d+) ---\n?")


def _split_group_pages(group) -> dict[int, str]:
    parts = _PAGE_MARK_RE.split(group.combined_text or "")
    out: dict[int, str] = {}
    for i in range(1, len(parts) - 1, 2):
        try:
            out[int(parts[i])] = parts[i + 1]
        except ValueError:
            continue
    if not out and group.pages:
        out[group.pages[0]] = group.combined_text or ""
    return out


_INCOME_EVIDENCE_WORDS = ("income", "paystub", "pay stub", "benefit", "pension", "tanf",
                          "child support", "work number", "equifax", "unemployment",
                          "self-employment", "gift", "disability", "zero income")
_ASSET_EVIDENCE_WORDS = ("asset", "bank", "investment", "real estate", "life insurance",
                         "direct express", "debit card", "disposal", "verification of deposit")
_HOUSEHOLD_EVIDENCE_WORDS = ("questionnaire", "application", "identity", "summary sheet",
                             "student status")


def _evidence_class(document_type: str | None, category: str | None) -> str:
    """Which kind of evidence a document is, derived from its taxonomy label
    rather than from a literal list that has to be kept in sync by hand."""
    label = (document_type or "").lower()
    if is_previous_certification(document_type):
        return "other"
    if is_current_certification_form(document_type):
        return "cert"
    if category == "compliance":
        return "compliance"
    if any(w in label for w in _HOUSEHOLD_EVIDENCE_WORDS):
        return "household"
    if any(w in label for w in _ASSET_EVIDENCE_WORDS):
        return "asset"
    if any(w in label for w in _INCOME_EVIDENCE_WORDS):
        return "income"
    return "other"


# Words a field's label uses on the forms; a short number must sit within
# _LABEL_REACH characters after one of them to count as a strong match.
_FIELD_LABEL_WORDS = {
    "tenantRent": ("tenant rent", "rent", "ttp", "tenant payment"),
    "utilityAllowance": ("utility", "allowance"),
    "grossRent": ("gross rent", "rent"),
    "householdIncome": ("income",),
    "householdSize": ("household", "members", "family", "size"),
    "numberOfBedrooms": ("bedroom", "br", "size"),
    "currentBalance": ("balance", "value", "amount", "equity"),
    "averageSixMonthBalance": ("average", "balance"),
    "selfDeclaredAmount": ("amount", "value", "balance", "income", "cash", "support", "benefit"),
    "rateOfPay": ("rate", "pay", "benefit", "amount", "salary", "wage", "per", "$"),
    "hoursPerPayPeriod": ("hours", "hrs"),
    "incomeAmount": ("income", "interest", "dividend", "yield"),
    "ytdAmount": ("ytd", "year to date", "year-to-date", "total"),
}
_DEFAULT_LABEL_WORDS = ("$", "amount", "total", "income", "rent", "balance", "value")
_LABEL_REACH = 60


def _numeric_forms(value: str) -> list[str]:
    cleaned = value.replace("$", "").replace(",", "").strip()
    forms = [cleaned]
    try:
        num = float(cleaned)
    except ValueError:
        return forms
    if num == int(num):
        forms += [str(int(num)), f"{int(num):,}", f"{int(num):,}.00", f"{int(num)}.00"]
    forms += [f"{num:,.2f}", f"{num:.2f}"]
    no_cents = re.sub(r"\.00$", "", cleaned)
    if no_cents != cleaned:
        forms.append(no_cents)
    return list(dict.fromkeys(f for f in forms if f))


def _find_number(value: str, text: str) -> int | None:
    """Position of the value as a whole number token in text, else None.

    Anchored on both sides: not preceded by a digit or a decimal point, and
    not followed by more digits or by a separator that continues the number.
    "953.00" no longer matches inside "26TC252G95301", "82.05" inside
    "1,082.05", "58.00" inside "58,200", "116.00" inside "HUD-116".
    """
    for form in _numeric_forms(value):
        m = re.search(r"(?<![\d.,])(?<![a-z0-9]-)" + re.escape(form) + r"(?![\d]|[.,]\d)", text)
        if m:
            return m.start()
    return None


def _is_numeric_value(value: str) -> bool:
    return re.fullmatch(r"\$?\s*-?\d[\d,]*(?:\.\d+)?", value.strip()) is not None


def _find_value(value: str, field_name: str, texts: list[str]) -> str | None:
    """'strong', 'weak' or None: whether the value is on any of the texts.

    Numbers are matched as whole tokens; a number with three or fewer
    significant digits must additionally sit near its field's label, or
    the hit is weak — a bare "58" is a form field number as often as a
    utility allowance.
    """
    val = value.strip().lower()
    if not val:
        return None
    if _is_numeric_value(val):
        digits = re.sub(r"\D", "", re.sub(r"\.00$", "", val.replace(",", "")))
        short = len(digits.lstrip("0")) <= 3
        labels = _FIELD_LABEL_WORDS.get(field_name, _DEFAULT_LABEL_WORDS)
        best = None
        for text in texts:
            pos = _find_number(val, text)
            if pos is None:
                continue
            if not short:
                return "strong"
            window = text[max(0, pos - _LABEL_REACH):pos]
            if any(w in window for w in labels):
                return "strong"
            best = "weak"
        return best
    for text in texts:
        if _value_in_source(val, text):
            return "strong"
    return None


# Picklist fields: the engine's word for what the form says. Verified by
# synonym on the record's own pages; never scored as poor OCR when absent.
_VOCAB_SYNONYMS: dict[str, dict[str, list[str]]] = {
    "frequencyOfPay": {
        "bi-weekly": [r"bi[\s-]?weekly", r"every (?:two|2) weeks", r"\bbiwkly\b"],
        "weekly": [r"\bweekly\b", r"per week", r"/wk\b"],
        "monthly": [r"\bmonthly\b", r"per month", r"/mo\b", r"each month"],
        "semi-monthly": [r"semi[\s-]?monthly", r"twice a month", r"1st and 15th"],
        "annually": [r"\bannual", r"\byearly\b", r"per year", r"/yr\b"],
        "hourly": [r"\bhourly\b", r"per hour", r"/hr\b"],
    },
    "employmentStatus": {
        "active": [r"presently employed[:\s]*(?:yes|x)", r"currently employed", r"\bactive\b", r"still employed", r"employed[:\s]*yes"],
        "terminated": [r"terminat", r"no longer employed", r"separat", r"last day", r"end(?:ed)? date"],
        "on leave": [r"\bleave\b"],
    },
    "incomeType": {
        "social security": [r"social security", r"\bssa\b", r"retirement benefit", r"\bss\b", r"soc\.? sec"],
        "supplemental security income": [r"supplemental security", r"\bssi\b"],
        "social security disability": [r"disability", r"\bssdi\b"],
        "child support": [r"child support"],
        "pension": [r"pension", r"annuity", r"retirement"],
        "non-federal wage": [r"\bwages?\b", r"salary", r"gross pay", r"rate of pay", r"employ", r"earnings"],
        "federal wage": [r"federal", r"\bwages?\b"],
        "temporary assistance": [r"\btanf\b", r"temporary assistance", r"cash aid", r"public assistance", r"calworks"],
        "self-employment": [r"self[\s-]?employ", r"business", r"schedule c"],
        "zero income": [r"no income", r"zero income", r"\$0"],
        "other income": [r"\bincome\b"],
        "employment": [r"employ", r"\bwages?\b", r"gross pay"],
    },
    "accountType": {
        "checking": [r"checking", r"\bchk\b", r"\bdda\b"],
        "savings": [r"savings?\b", r"\bsav\b"],
        "cash": [r"\bcash\b"],
        "real estate": [r"real estate", r"property", r"parcel", r"tax roll", r"\bdeed\b", r"apprais", r"equity"],
        "cd": [r"certificate of deposit", r"\bcd\b"],
        "investment": [r"invest", r"brokerage", r"mutual fund", r"401", r"\bira\b", r"stock"],
        "retirement": [r"retirement", r"401", r"\bira\b", r"pension"],
        "life insurance": [r"life insurance", r"surrender value", r"cash value"],
        "prepaid card": [r"prepaid", r"direct express", r"\bcard\b"],
        "direct express": [r"direct express"],
        "other": [r"\bother\b"],
    },
}


def _vocab_in(field_name: str, value: str, texts: list[str]) -> bool:
    val = value.strip().lower()
    patterns = _VOCAB_SYNONYMS.get(field_name, {}).get(val, [])
    patterns = patterns + [re.escape(val).replace(r"\ ", r"[\s-]?").replace(r"\-", r"[\s-]?")]
    for text in texts:
        for pat in patterns:
            try:
                if re.search(pat, text):
                    return True
            except re.error:
                continue
    return False


# At or below this many characters, a substring hit is not evidence: short
# codes and abbreviations occur inside ordinary words. Chosen to cover the
# two- and three-character values the schema actually carries — cert types
# (MI, AR, IR), state codes, Y/N flags — without disturbing longer values,
# where an accidental substring hit is vanishingly unlikely.
_MIN_UNANCHORED_MATCH = 3


def _whole_word_in(value: str, source_text: str) -> bool:
    """Whether the value appears as its own token in the source text."""
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", source_text) is not None


def _value_in_source(value: str, source_text: str) -> bool:
    """Check if an extracted value appears in the source OCR text.

    Handles common variations:
    - Exact match (case-insensitive)
    - Monetary: "2479.00" matches "$2,479.00", "2,479", "2479"
    - Dates: "2026-04-01" matches "04/01/2026", "4/1/2026", "04/01/26"
    - Names: "David Platt" matches "david platt", "DAVID PLATT"
    - SSN: "***-**-2999" matches "2999"
    """
    val = value.strip().lower()
    if not val:
        return False

    # A short value proves nothing by appearing somewhere in a page of text.
    # "AR" occurs inside YEAR, PART, CLARIFICATION and MARIJUANA, so a
    # certification type of "AR" scored a perfect source_verification against
    # a document that says Move-In. Below this length only a whole-word match
    # counts as having found the value.
    if len(val) <= _MIN_UNANCHORED_MATCH:
        return _whole_word_in(val, source_text)

    # Numbers match as whole tokens only (see _find_number): a bare
    # substring test verified "953.00" against a reference number and
    # "82.05" against "1,082.05".
    if _is_numeric_value(val):
        return _find_number(val, source_text) is not None

    # Direct match
    if val in source_text:
        return True

    cleaned = val.replace("$", "").replace(",", "").strip()
    # Try with comma formatting: "2479" → "2,479" or "46584" → "46,584"
    try:
        num = float(cleaned)
        if num == int(num):
            formatted = f"{int(num):,}"
            if formatted.lower() in source_text:
                return True
        formatted_dec = f"{num:,.2f}"
        if formatted_dec.lower() in source_text:
            return True
    except ValueError:
        pass

    # Date: try many format variants.
    # Extracted dates are YYYY-MM-DD, but source OCR may have:
    #   MM/DD/YYYY, M/D/YYYY, MM/DD/YY, M/D/YY,
    #   MM-DD-YYYY, M-D-YY,
    #   YYYY/MM/DD, YYYY/M/D (common on TIC forms like "2026/6/7")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", val)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        y_short = y[2:]
        mo_i, d_i = str(int(mo)), str(int(d))
        variants = [
            # MM/DD/YYYY and M/D/YYYY
            f"{mo}/{d}/{y}", f"{mo_i}/{d_i}/{y}",
            # MM/DD/YY and M/D/YY
            f"{mo}/{d}/{y_short}", f"{mo_i}/{d_i}/{y_short}",
            # MM-DD-YYYY and M-D-YYYY
            f"{mo}-{d}-{y}", f"{mo_i}-{d_i}-{y}",
            # MM-DD-YY and M-D-YY
            f"{mo}-{d}-{y_short}", f"{mo_i}-{d_i}-{y_short}",
            # YYYY/MM/DD and YYYY/M/D (TIC/LIHTC forms)
            f"{y}/{mo}/{d}", f"{y}/{mo_i}/{d_i}",
            # YYYY-MM-DD (the extracted format itself)
            f"{y}-{mo}-{d}",
        ]
        for v in variants:
            if v.lower() in source_text:
                return True
        # OCR pads a part with a stray digit ("071/15/1949"); a token that
        # normalises to the value is the value.
        from app.services.identity import _normalize_dob
        for tok in re.findall(r"(?<![\d/])\d{1,3}[/-]\d{1,3}[/-]\d{2,4}(?![\d/])", source_text):
            if _normalize_dob(tok) == val:
                return True

    # SSN (masked or full): verify by last 4 digits. Full SSNs are stored
    # dash-formatted but documents print them with dashes, spaces, or bare
    # digits — the last 4 are the stable verification token either way.
    ssn_match = re.match(r"(?:[\*X]{3}-[\*X]{2}|\d{3}-\d{2})-(\d{4})$", val)
    if ssn_match:
        last4 = ssn_match.group(1)
        if last4 in source_text:
            return True

    # Name: check individual words (first name, last name separately)
    words = val.split()
    if len(words) >= 2 and all(w in source_text for w in words):
        return True

    # Space/hyphen normalization: OCR often inserts or drops spaces around
    # hyphens and punctuation. "D-214" → "D- 214", "B1-209" → "B1- 209".
    # Collapse all whitespace around hyphens/slashes in both value and source.
    val_collapsed = re.sub(r"\s*([/\-])\s*", r"\1", val)
    src_collapsed = re.sub(r"\s*([/\-])\s*", r"\1", source_text)
    if val_collapsed and val_collapsed in src_collapsed:
        return True

    return False


# ---------------------------------------------------------------------------
# Stage 2: Cross-document consistency
# ---------------------------------------------------------------------------

def _cross_doc_norm(field_name: str, value: str) -> str:
    """Normalize a value for cross-document comparison.

    SSNs compare by last-4 digits: the same number legitimately appears
    full on one document and masked on another ("530-38-7514" vs
    "***-**-7514" vs "XXX-XX-7514") — that is presentation, not conflict."""
    if field_name == "socialSecurityNumber":
        digits = [c for c in value if c.isdigit()]
        if len(digits) >= 4:
            return "".join(digits[-4:])
    return value.lower().strip()


def score_cross_doc_consistency(cards: list[RecordScoreCard]) -> None:
    """Compare fields across records that refer to the same entity."""
    by_label: dict[str, list[RecordScoreCard]] = {}
    for card in cards:
        key = (card.record_label or "").lower().strip()
        if key:
            by_label.setdefault(key, []).append(card)

    for label, group in by_label.items():
        if len(group) < 2:
            continue

        field_values: dict[str, list[tuple[str | None, RecordScoreCard]]] = {}
        for card in group:
            for fs in card.fields:
                field_values.setdefault(fs.field_name, []).append((fs.value, card))

        for field_name, entries in field_values.items():
            values = [v for v, _ in entries if v is not None]
            if len(values) < 2:
                continue
            unique = set(_cross_doc_norm(field_name, v) for v in values)
            if len(unique) == 1:
                for _, card in entries:
                    update_field_score(card, field_name, stage="cross_doc",
                                       score=1.0, reason=f"Confirmed by {len(values)} sources")
            else:
                for _, card in entries:
                    update_field_score(card, field_name, stage="cross_doc",
                                       score=0.30,
                                       reason=f"Conflict across documents: {unique}")

    for card in cards:
        card.recompute()


# ---------------------------------------------------------------------------
# Stage 3: Business rule validation
# ---------------------------------------------------------------------------

_FIXED_INCOME_TYPES = {
    "temporary assistance", "social security", "supplemental security income",
    "social security disability", "child support", "pension", "veterans benefits", "other income",
    "zero income", "self-employment", "self-declared",
}

_FIXED_INCOME_NA_FIELDS = {
    # Non-employment income sources (SSA, SSI, SSDI, pension, TANF,
    # child support, VA, self-employment, self-declared) store amounts
    # differently from wages. These fields don't apply.
    "rateOfPay", "frequencyOfPay",
    "hoursPerPayPeriod", "employmentStatus", "hireDate",
    "overtimeRate", "overtimeFrequency",
    "ytdAmount", "ytdStartDate", "ytdEndDate",
}

# An income record must carry at least one of these to be usable. With none of
# them we know the source but not how much it pays — useless for income
# calculation or MuleSoft comparison. This invariant holds for ALL income
# types, including fixed benefits that store the amount in rateOfPay.
_INCOME_AMOUNT_FIELDS = ("rateOfPay", "selfDeclaredAmount", "ytdAmount", "overtimeRate")

# AR-SC certifications use the TIC as the source of truth — there are NO
# third-party verification documents (VOI, paystubs, Equifax). Every
# wage-verification field is EXPECTED to be null. Suppress them as N/A
# instead of penalizing as RED.
_AR_SC_NA_FIELDS = {
    "rateOfPay", "frequencyOfPay", "hoursPerPayPeriod",
    "overtimeRate", "overtimeFrequency",
    "ytdAmount", "ytdStartDate", "ytdEndDate",
    "employmentStatus", "terminationDate", "hireDate",
    "type_of_VOI", "dateReceived",
}

# Terminated employment: the record documents that income STOPPED, so
# ongoing-pay fields are definitionally absent — a termination VOE with
# no rate/hours/YTD is the expected state, not an extraction gap.
# terminationDate is deliberately NOT here: that field IS expected on a
# terminated record (the "Terminated but no termination date" rule fires).
_TERMINATED_NA_FIELDS = {
    "rateOfPay", "frequencyOfPay", "hoursPerPayPeriod",
    "overtimeRate", "overtimeFrequency",
    "ytdAmount", "ytdStartDate", "ytdEndDate",
    "hireDate",
}


# A finding that disputes the extraction is not a pass/fail on one field — it
# says the values it names cannot all be right. The score it contributes is
# low rather than zero: the finding proves a contradiction exists, not which
# side of it is wrong.
_DISPUTED_SCORE = 0.15
# When a dispute names no subject, it concerns the case as a whole. Applied to
# every record in the categories it touches, but more gently — a case-level
# contradiction is weaker evidence against any one record than a finding that
# names it.
_DISPUTED_CASE_SCORE = 0.40

# Which fields a finding category is about. A dispute lands on the fields
# it could be wrong about — the amounts of an income dispute, the balances
# of an asset dispute — not on every field of every record in the category.
# One case-level income dispute used to stamp 0.40 on memberName,
# accountType and assetOwner alike, and the highest-weighted stage was the
# least attributed.
_CATEGORY_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "income": {
        "income": ("rateOfPay", "selfDeclaredAmount", "ytdAmount", "hoursPerPayPeriod", "frequencyOfPay"),
        "certification": ("householdIncome",),
    },
    "asset": {
        "asset": ("currentBalance", "selfDeclaredAmount", "incomeAmount"),
    },
    "household_member": {
        "household_member": ("FirstName", "LastName", "DOB", "socialSecurityNumber"),
    },
    "unit_rent": {
        "certification": ("tenantRent", "utilityAllowance", "grossRent"),
    },
    "expense": {
        "income": ("rateOfPay", "selfDeclaredAmount", "ytdAmount"),
        "asset": ("currentBalance", "selfDeclaredAmount", "incomeAmount"),
    },
    "file_review": {},          # about the file; lands only where a field is named
}
# Fields a finding may name in subject_ref["field"], and the record type
# that carries them, for findings whose category alone says nothing.
_NAMED_FIELD_RECORD = {
    "isSigned": "certification", "signatureDate": "certification",
    "effectiveDate": "certification", "householdIncome": "certification",
    "tenantRent": "certification", "utilityAllowance": "certification",
    "grossRent": "certification", "householdSize": "certification",
}


def score_findings(cards: list[RecordScoreCard], findings: list) -> None:
    """Let the audit's own findings lower the confidence of what they dispute.

    Every other stage asks a question of a value in isolation: is it populated,
    does it appear in the source text, does it satisfy a range. None of them
    can see that the household's income sums to 1,500 times what the
    certification declares, because that is a relationship between values
    rather than a property of one.

    Attribution, narrowest first:
      - a finding that names a field (subject_ref["field"]) lowers that field
        on the record it names, or on the certification card for a
        certification field;
      - a finding that names a record (member_name / source_name /
        account_type) lowers that record's category fields at _DISPUTED_SCORE;
      - a case-level finding lowers the category fields of every candidate
        record, with the penalty shared among them — the contradiction is
        real, which record carries it is unknown;
      - an income dispute with no income records at all lands on the
        certification's householdIncome, the figure the missing records were
        supposed to account for.
    Only findings that dispute the extraction are read (findings.py).
    """
    # An informational finding (result "na" or "compliant" — a subtotal
    # noted, a name spelled two ways) is not a contradiction and must not
    # lower anything, whatever its code's registration says.
    disputing = [
        f for f in findings
        if isinstance(f, Finding) and f.disputes_extraction and f.result == "non_compliant"
    ]
    if not disputing:
        return

    for finding in disputing:
        ref = finding.subject_ref or {}
        named_field = ref.get("field")
        subject = {slug(v) for k, v in ref.items() if v and k not in ("field", "table")}
        fields_by_type = _CATEGORY_FIELDS.get(finding.category, {})
        reason = f"Disputed by {finding.code}"
        strength = dispute_strength(finding.code)

        # Named field on a named record, or on the certification card.
        if named_field:
            if subject:
                targets = [c for c in cards if c.record_type in fields_by_type
                           and _names_record(subject, c)]
            else:
                rt = _NAMED_FIELD_RECORD.get(named_field)
                targets = [c for c in cards if c.record_type == rt] if rt else []
            for card in targets:
                card.disputed = True
                if any(f.field_name == named_field for f in card.fields):
                    _lower_fields(card, (named_field,), strength, reason)
                else:
                    # The named field is not one the card scores (a
                    # reconciliation field such as declaredAnnualAmount):
                    # the dispute is still about this record's amounts.
                    _lower_fields(card, fields_by_type.get(card.record_type, ()), strength, reason)
            if targets:
                continue

        candidates = [c for c in cards if c.record_type in fields_by_type]
        if subject:
            targets = [c for c in candidates if _names_record(subject, c)]
            for card in targets:
                card.disputed = True
                _lower_fields(card, fields_by_type[card.record_type], strength, reason)
            continue

        # Case level. When the category's records do not exist, an income
        # dispute lands on the declared total it was measured against.
        record_types = [rt for rt in fields_by_type if rt != "certification"]
        present = [c for c in candidates if c.record_type in record_types]
        if not present and finding.category in ("income", "expense"):
            for card in cards:
                if card.record_type == "certification":
                    _lower_fields(card, ("householdIncome",), strength, reason)
            continue
        if not present:
            continue
        # Shared penalty: with n candidate records, each carries 1/n of it.
        share = 1.0 - (1.0 - max(strength, _DISPUTED_CASE_SCORE)) / len(present)
        for card in present:
            _lower_fields(card, fields_by_type[card.record_type], share, reason)

    for card in cards:
        card.recompute()


def _names_record(subject: set, card: RecordScoreCard) -> bool:
    """Whether every named part of a finding's subject is on the record's
    label. Matching on any one part let a finding about a member's child
    support land on the same member's Social Security record."""
    label_parts = {slug(p) for p in (card.record_label or "").split("—")}
    return bool(subject) and subject <= label_parts


def _lower_fields(card: RecordScoreCard, field_names: tuple, score: float, reason: str) -> None:
    for field in card.fields:
        if field.field_name in field_names and field.flag != ScoreFlag.NA and field.value is not None:
            update_field_score(card, field.field_name, stage="finding", score=score, reason=reason)


# Member fields each certification form actually prints. A null in a field
# the form does not carry is not a gap; a null in one it carries is.
_FORM_MEMBER_FIELDS = {
    "HUD 50059": {"disabled", "student"},
    "HUD 3560 Form": {"disabled", "student"},
    "Tenant Income Certification (TIC)": {"student"},
    "HUD Model Lease": set(),
}


def score_business_rules(
    cards: list[RecordScoreCard],
    certification_type: str | None = None,
    cert_form_type: str | None = None,
) -> None:
    """Apply business rule checks to field values.

    cert_form_type: the current certification form's document type, so a
    member field the form does not print is N/A rather than red.
    """
    for card in cards:
        if card.record_type == "income":
            _score_income_rules(card, certification_type)
        elif card.record_type == "asset":
            _score_asset_rules(card)
        elif card.record_type == "household_member":
            _score_member_rules(card, cert_form_type)
        elif card.record_type == "certification":
            _score_certification_rules(card, certification_type)
        card.recompute()


def _score_income_rules(card: RecordScoreCard, cert_type: str | None) -> None:
    vals = {f.field_name: f.value for f in card.fields}

    # sourceName / memberName: basic name validation (pushes names to GREEN)
    for field_name in ("sourceName", "memberName"):
        val = vals.get(field_name)
        if val and len(val) >= 2 and not any(
            kw in val.lower() for kw in ("<td", "section", "worksheet", "total")
        ):
            update_field_score(card, field_name, stage="business_rule",
                               score=1.0, reason="Valid name")

    # Record-level invariant: every income record needs at least one usable
    # amount. Runs BEFORE the fixed-income / AR-SC N/A marking below so those
    # records aren't excused — a Social Security row with no benefit figure is
    # still a real gap. When an amount IS present (in any field), a null
    # selfDeclaredAmount is not a gap, so mark it N/A instead of false-RED.
    is_terminated = "terminated" in (vals.get("employmentStatus") or "").lower()
    if not is_terminated:
        for fs in card.fields:
            if fs.field_name == "terminationDate" and fs.value is None:
                fs.mark_na("Employment not terminated")
    if any(vals.get(f) for f in _INCOME_AMOUNT_FIELDS):
        for fs in card.fields:
            if fs.field_name == "selfDeclaredAmount" and fs.value is None:
                fs.mark_na("Amount captured in another field")
    elif is_terminated:
        # A termination record with no amount documents that income
        # stopped — that IS its content, not a gap.
        for fs in card.fields:
            if fs.field_name == "selfDeclaredAmount" and fs.value is None:
                fs.mark_na("Terminated employment — no ongoing amount expected")
    else:
        update_field_score(
            card, "selfDeclaredAmount", stage="business_rule", score=0.0,
            reason="Income record has no amount in any field — verify source",
        )

    # selfDeclaredAmount magnitude check when present
    sda = vals.get("selfDeclaredAmount")
    if sda:
        try:
            amt = float(sda.replace(",", ""))
            if amt <= 0:
                update_field_score(card, "selfDeclaredAmount", stage="business_rule",
                                   score=0.30, reason="Zero or negative amount")
            else:
                update_field_score(card, "selfDeclaredAmount", stage="business_rule",
                                   score=1.0, reason="Valid amount")
        except ValueError:
            update_field_score(card, "selfDeclaredAmount", stage="business_rule",
                               score=0.30, reason="Not a valid number")

    # AR-SC: TIC is the source of truth, no VOI/paystubs expected.
    # Mark wage-verification fields as N/A when null instead of RED.
    if (cert_type or "").upper() == "AR-SC":
        for fs in card.fields:
            if fs.field_name in _AR_SC_NA_FIELDS and fs.value is None:
                fs.mark_na("Not applicable for AR-SC (TIC is source of truth)")

    # Terminated employment: ongoing-pay fields are expected-null (see
    # _TERMINATED_NA_FIELDS). Without this, one termination VOE produces
    # 8 RED findings and drags the extraction score low enough to disable
    # IR scoping — exactly on the packets whose whole point is the
    # termination.
    if is_terminated:
        for fs in card.fields:
            if fs.field_name in _TERMINATED_NA_FIELDS and fs.value is None:
                fs.mark_na("Terminated employment — ongoing pay fields not applicable")

    # Self-certified income: when the record's ONLY amount basis is
    # selfDeclaredAmount, wage-verification fields (rate/hours/YTD) don't
    # exist by design — a self-cert annual figure IS the whole disclosure.
    # Keyed on the record's basis, not the cert-type label: 421-A-style
    # self-cert recerts arrive labeled plain "AR", so the AR-SC exemption
    # never fires and a perfect-agreement packet scores 14 false REDs.
    if vals.get("selfDeclaredAmount") and not any(
        vals.get(f) for f in ("rateOfPay", "ytdAmount", "overtimeRate")
    ):
        for fs in card.fields:
            if fs.field_name in _AR_SC_NA_FIELDS and fs.value is None:
                fs.mark_na("Self-declared income — wage verification fields not applicable")

    income_type = (vals.get("incomeType") or "").lower()

    # Mark N/A fields for fixed-income types
    if income_type in _FIXED_INCOME_TYPES:
        for fs in card.fields:
            if fs.field_name in _FIXED_INCOME_NA_FIELDS and fs.value is None:
                fs.mark_na(f"Not applicable for {vals.get('incomeType', 'fixed income')}")
        source = vals.get("sourceName")
        if not source:
            for fs in card.fields:
                if fs.field_name == "sourceName" and fs.value is None:
                    fs.mark_na(f"Source is the program ({vals.get('incomeType', 'N/A')})")
        return

    # rateOfPay: numeric, > 0. An hourly figure (hours are stated) above
    # $300 is not an hourly rate; a periodic or annual figure is bounded
    # only by what one job can pay.
    rate = vals.get("rateOfPay")
    if rate:
        try:
            rate_num = float(rate.replace(",", ""))
            hourly_looking = bool(vals.get("hoursPerPayPeriod"))
            if rate_num <= 0:
                update_field_score(card, "rateOfPay", stage="business_rule",
                                   score=0.20, reason="Rate is zero or negative")
            elif hourly_looking and rate_num > 300:
                update_field_score(card, "rateOfPay", stage="business_rule",
                                   score=0.50, reason="Hours are stated but the rate is not an hourly figure — verify rate unit")
            elif rate_num > 1_000_000:
                update_field_score(card, "rateOfPay", stage="business_rule",
                                   score=0.50, reason="Unusually high — verify rate unit")
            else:
                update_field_score(card, "rateOfPay", stage="business_rule",
                                   score=1.0, reason="Valid range")
        except ValueError:
            update_field_score(card, "rateOfPay", stage="business_rule",
                               score=0.30, reason="Not a valid number")

    # hoursPerPayPeriod is hours in ONE pay period, so its plausible range
    # scales with how long that period is. Bounding it at 1-168 assumed a
    # week, which scored every correct non-weekly value as suspect: a
    # bi-weekly 80 was "hrs/week seems high" at 0.70 and a monthly 173.33
    # was out of range at 0.30. That pushed the extractor toward reporting
    # hours per week, which the annualizer then multiplies by the number of
    # pay periods — the reading that halves a bi-weekly wage.
    #
    # Bounds are derived from the frequency's own multiplier rather than
    # listed per frequency, so a frequency added to FREQUENCY_MULTIPLIERS is
    # bounded correctly without touching this rule.
    hours = vals.get("hoursPerPayPeriod")
    if hours:
        try:
            h = float(hours)
            multiplier = get_frequency_multiplier(vals.get("frequencyOfPay"))
            # Unknown frequency gets the loosest period rather than the
            # tightest: penalising a value we cannot bound is the mistake
            # this rule just made.
            weeks = 52 / multiplier if multiplier else 52 / 12
            hard_cap = 168 * weeks          # every hour of every day
            plausible = 60 * weeks          # sustained full-time plus overtime
            if h < 1 or h > hard_cap:
                update_field_score(card, "hoursPerPayPeriod", stage="business_rule",
                                   score=0.30,
                                   reason=f"Hours {h} outside 1-{hard_cap:.0f} "
                                          f"range for a {weeks:.2f}-week pay period")
            elif h > plausible:
                update_field_score(card, "hoursPerPayPeriod", stage="business_rule",
                                   score=0.70,
                                   reason=f"{h} hrs in a {weeks:.2f}-week pay "
                                          f"period seems high")
            else:
                update_field_score(card, "hoursPerPayPeriod", stage="business_rule",
                                   score=1.0, reason="Valid range")
        except ValueError:
            update_field_score(card, "hoursPerPayPeriod", stage="business_rule",
                               score=0.30, reason="Not a valid number")

    # frequencyOfPay: known picklist value
    freq = vals.get("frequencyOfPay")
    valid_freqs = {"hourly", "weekly", "bi-weekly", "semi-monthly", "monthly", "annually"}
    if freq:
        if freq.lower() in valid_freqs:
            update_field_score(card, "frequencyOfPay", stage="business_rule",
                               score=1.0, reason="Valid frequency")
        else:
            update_field_score(card, "frequencyOfPay", stage="business_rule",
                               score=0.40, reason=f"Unknown frequency '{freq}'")

    # incomeType: known picklist
    itype = vals.get("incomeType")
    known_types = {
        "non-federal wage", "federal wage", "social security", "temporary assistance",
        "supplemental security income", "social security disability", "child support",
        "pension", "self-employment", "other income", "zero income",
    }
    if itype:
        if itype.lower() in known_types:
            update_field_score(card, "incomeType", stage="business_rule",
                               score=1.0, reason="Valid income type")
        else:
            update_field_score(card, "incomeType", stage="business_rule",
                               score=0.50, reason=f"Unknown type '{itype}'")

    # Terminated without termination date
    status = vals.get("employmentStatus")
    if status and "terminated" in status.lower() and not vals.get("terminationDate"):
        update_field_score(card, "employmentStatus", stage="business_rule",
                           score=0.40, reason="Terminated but no termination date")

    # YTD date consistency
    ytd_start = vals.get("ytdStartDate")
    ytd_end = vals.get("ytdEndDate")
    if ytd_start and ytd_end and ytd_start > ytd_end:
        update_field_score(card, "ytdStartDate", stage="business_rule",
                           score=0.20, reason="Start date after end date")


def _score_asset_rules(card: RecordScoreCard) -> None:
    vals = {f.field_name: f.value for f in card.fields}

    # Self-certified assets store the amount in selfDeclaredAmount, not
    # currentBalance — the comparator already reads both (mirroring
    # _ai_asset_balance); the scorer must too, or a fully-captured
    # self-cert asset gets a false RED on the empty sibling field.
    # Self-certifications also don't itemize per-asset income, so
    # incomeAmount is expected-null there.
    if vals.get("selfDeclaredAmount") and not vals.get("currentBalance"):
        for fs in card.fields:
            if fs.field_name == "currentBalance" and fs.value is None:
                fs.mark_na("Balance captured in selfDeclaredAmount")
            if fs.field_name == "incomeAmount" and fs.value is None:
                fs.mark_na("Self-declared asset — per-asset income not itemized")

    # The mirror case: a third-party verified asset carries its value in
    # currentBalance and has no self-declaration to record. Scoring the
    # empty sibling would just move the false RED to the other field.
    elif vals.get("currentBalance") and not vals.get("selfDeclaredAmount"):
        for fs in card.fields:
            if fs.field_name == "selfDeclaredAmount" and fs.value is None:
                fs.mark_na("Balance captured in currentBalance")
    # A statement for a non-interest account states no interest. A null
    # incomeAmount there is what the document says, not a missed field.
    if vals.get("currentBalance") and not vals.get("incomeAmount"):
        atype = (vals.get("accountType") or "").lower()
        if any(t in atype for t in ("checking", "cash", "prepaid", "direct express", "debit")):
            for fs in card.fields:
                if fs.field_name == "incomeAmount" and fs.value is None:
                    fs.mark_na("No interest stated for this account type")
        elif card.verification_status in ("verified", None):
            # A verification of deposit states a balance and a rate; the
            # annual income is imputed on the worksheet, not printed. Its
            # absence is what the document says, not a missed field.
            for fs in card.fields:
                if fs.field_name == "incomeAmount" and fs.value is None:
                    fs.mark_na("Document states no income figure — imputed on the asset worksheet")

    # currentBalance: numeric >= 0
    balance = vals.get("currentBalance")
    if balance:
        try:
            b = float(balance.replace(",", ""))
            if b < 0:
                update_field_score(card, "currentBalance", stage="business_rule",
                                   score=0.20, reason="Negative balance")
            else:
                update_field_score(card, "currentBalance", stage="business_rule",
                                   score=1.0, reason="Valid balance")
        except ValueError:
            update_field_score(card, "currentBalance", stage="business_rule",
                               score=0.30, reason="Not a valid number")

    # incomeAmount should not equal currentBalance (common LLM error)
    income_amt = vals.get("incomeAmount")
    if income_amt and balance and income_amt == balance:
        try:
            if float(balance.replace(",", "")) > 10:
                update_field_score(card, "incomeAmount", stage="business_rule",
                                   score=0.40, reason="Equals cash value — likely extraction error")
        except ValueError:
            pass

    # accountType: known picklist
    atype = vals.get("accountType")
    known = {"checking", "savings", "cd", "life insurance", "investment", "real estate",
             "retirement", "cash", "prepaid card", "annuity", "peer-to-peer",
             "able account", "cryptocurrency", "direct express"}
    if atype:
        if atype.lower() in known:
            update_field_score(card, "accountType", stage="business_rule",
                               score=1.0, reason="Valid account type")
        else:
            update_field_score(card, "accountType", stage="business_rule",
                               score=0.60, reason=f"Uncommon type '{atype}'")


def _score_member_rules(card: RecordScoreCard, cert_form_type: str | None = None) -> None:
    vals = {f.field_name: f.value for f in card.fields}
    form_fields = _FORM_MEMBER_FIELDS.get(cert_form_type or "")

    # Name fields: basic validation (alphabetic, not garbage)
    for field_name in ("FirstName", "LastName"):
        val = vals.get(field_name)
        if val:
            # Valid name: starts with letter, contains mostly letters/hyphens/spaces
            is_valid = bool(re.match(r"^[A-Za-z]", val)) and not any(
                kw in val.lower() for kw in ("section", "income", "asset", "total", "<td")
            )
            if is_valid:
                update_field_score(card, field_name, stage="business_rule",
                                   score=1.0, reason="Valid name")
            else:
                update_field_score(card, field_name, stage="business_rule",
                                   score=0.20, reason=f"Doesn't look like a name: '{val}'")

    # DOB: valid calendar date
    dob = vals.get("DOB")
    if dob:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", dob)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1900 < y < 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                update_field_score(card, "DOB", stage="business_rule",
                                   score=1.0, reason="Valid date")
            else:
                update_field_score(card, "DOB", stage="business_rule",
                                   score=0.10, reason=f"Invalid date: {dob}")
        else:
            update_field_score(card, "DOB", stage="business_rule",
                               score=0.10, reason=f"Invalid format: {dob}")

    # SSN: masked format
    ssn = vals.get("socialSecurityNumber")
    if ssn:
        if re.match(r"(\*{3}-\*{2}-\d{4}|XXX-XX-\d{4}|\d{3}-\d{2}-\d{4})", ssn):
            update_field_score(card, "socialSecurityNumber", stage="business_rule",
                               score=1.0, reason="Valid SSN format")
        else:
            update_field_score(card, "socialSecurityNumber", stage="business_rule",
                               score=0.40, reason=f"Invalid format: {ssn}")

    # disabled / student: Y or N. Absent on a form that carries the column is
    # a gap to verify; absent on a form that has no such column is nothing.
    for field_name in ("disabled", "student"):
        val = vals.get(field_name)
        if val in ("Y", "N"):
            update_field_score(card, field_name, stage="business_rule",
                               score=1.0, reason="Valid Y/N")
        elif val is None:
            if form_fields is not None and field_name not in form_fields:
                for fs in card.fields:
                    if fs.field_name == field_name:
                        fs.mark_na(f"Not a field on the {cert_form_type}")
            else:
                update_field_score(card, field_name, stage="business_rule",
                                   score=0.30, reason="Missing — verify against cert form")


def _score_certification_rules(card: RecordScoreCard, cert_type: str | None) -> None:
    vals = {f.field_name: f.value for f in card.fields}

    # certificationType: known picklist
    ct = vals.get("certificationType")
    valid_types = {"MI", "AR", "AR-SC", "IR", "IC", "IN"}
    if ct:
        if ct.upper() in valid_types:
            update_field_score(card, "certificationType", stage="business_rule",
                               score=1.0, reason="Valid cert type")
        else:
            update_field_score(card, "certificationType", stage="business_rule",
                               score=0.20, reason=f"Unknown: '{ct}'")

    # effectiveDate: valid calendar date
    ed = vals.get("effectiveDate")
    if ed:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", ed)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1900 < y < 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                update_field_score(card, "effectiveDate", stage="business_rule",
                                   score=1.0, reason="Valid date")
            else:
                update_field_score(card, "effectiveDate", stage="business_rule",
                                   score=0.10, reason=f"Invalid: {ed}")
        else:
            update_field_score(card, "effectiveDate", stage="business_rule",
                               score=0.10, reason=f"Invalid format: {ed}")

    # grossRent > 0
    gr = vals.get("grossRent")
    if gr:
        try:
            if float(gr.replace(",", "")) > 0:
                update_field_score(card, "grossRent", stage="business_rule",
                                   score=1.0, reason="Positive gross rent")
            else:
                update_field_score(card, "grossRent", stage="business_rule",
                                   score=0.20, reason="Zero or negative")
        except ValueError:
            update_field_score(card, "grossRent", stage="business_rule",
                               score=0.30, reason="Not a valid number")

    # tenantRent <= grossRent
    tr = vals.get("tenantRent")
    if tr and gr:
        try:
            t, g = float(tr.replace(",", "")), float(gr.replace(",", ""))
            if t <= g:
                update_field_score(card, "tenantRent", stage="business_rule",
                                   score=1.0, reason="Tenant rent <= gross rent")
            else:
                update_field_score(card, "tenantRent", stage="business_rule",
                                   score=0.30, reason=f"${t} exceeds gross ${g}")
        except ValueError:
            pass

    # isSigned
    signed = vals.get("isSigned")
    if signed == "Yes":
        update_field_score(card, "isSigned", stage="business_rule",
                           score=1.0, reason="Signed")
    elif signed == "No":
        update_field_score(card, "isSigned", stage="business_rule",
                           score=0.20, reason="NOT signed — resubmission required")


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def build_score_summary(cards: list[RecordScoreCard]) -> ExtractionScoreSummary:
    summary = ExtractionScoreSummary(records=cards)
    summary.recompute()
    return summary
