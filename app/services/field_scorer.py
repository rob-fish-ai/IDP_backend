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
    ExtractionScoreSummary,
    FieldScore,
    RecordScoreCard,
    ScoreFlag,
    StageScore,
)
from app.services.findings import slug
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
                           "ytdAmount", "hireDate"):
                val = getattr(vi, field, None)
                scorer.score_field(field, str(val) if val else None)
            cards.append(scorer.build())

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
            cards.append(scorer.build())

    return cards


# ---------------------------------------------------------------------------
# Stage 1b: Source verification — OCR quality + value-in-text check
# ---------------------------------------------------------------------------

def score_source_verification(
    cards: list[RecordScoreCard],
    document_groups: list,
    ocr_quality: dict[int, dict],
) -> None:
    """Check if extracted values actually appear in the source OCR text.

    Combines two signals:
    - Value-in-text: does the extracted value exist in the OCR text?
    - OCR quality: only matters when the value is NOT found (to distinguish
      "LLM hallucinated" from "OCR couldn't read that region").

    | Value Found | OCR Quality | Score | Reason                            |
    |-------------|-------------|-------|-----------------------------------|
    | yes         | any         | 1.00  | Verified in source                |
    | no          | green       | 0.50  | Not found — possible LLM error    |
    | no          | yellow/red  | 0.30  | Not found + poor OCR — unreliable |

    A YELLOW OCR page flag doesn't downgrade a found-in-source value: if the
    value was literally present in the text, the OCR was good enough for that
    field. This avoids 7+ noise findings per file.
    """
    if not document_groups:
        return

    # Build combined source text per document group type
    # Map record types to the doc types they were extracted from
    _RECORD_DOC_MAP = {
        "household_member": {"HUD 50059", "Tenant Income Certification (TIC)", "HUD 3560 Form",
                             "HUD Model Lease", "Application / Housing Questionnaire"},
        "certification": {"HUD 50059", "Tenant Income Certification (TIC)", "HUD 3560 Form",
                          "HUD Model Lease"},
        "income": {"Verification of Income (VOI)", "Paystub", "SSA Benefit Letter",
                    "Work Number / Equifax Report", "Tenant Income Certification (TIC)",
                    "HUD 50059", "Application / Housing Questionnaire"},
        "asset": {"Verification of Assets (VOA)", "Bank Statement", "Life Insurance Policy",
                   "Asset Self-Certification", "Real Estate Verification",
                   "Tenant Income Certification (TIC)", "HUD 50059",
                   "Application / Housing Questionnaire"},
    }

    # Build source text + worst OCR quality per record type
    source_data: dict[str, dict] = {}  # record_type → {text, worst_flag}
    for record_type, doc_types in _RECORD_DOC_MAP.items():
        texts = []
        worst_flag = "green"
        for g in document_groups:
            if g.category == "ignore":
                continue
            if g.document_type in doc_types:
                texts.append(g.combined_text)
                for pn in g.pages:
                    pq = ocr_quality.get(pn, {})
                    page_flag = pq.get("flag", "green")
                    if page_flag == "red" or (page_flag == "yellow" and worst_flag == "green"):
                        worst_flag = page_flag

        source_data[record_type] = {
            "text": " ".join(texts).lower(),
            "ocr_flag": worst_flag,
        }

    # Everything the packet contains, regardless of which record type the
    # document maps to. The map above has drifted from the classifier's
    # taxonomy — most of its labels are absent from it — so a value can be
    # printed plainly in the packet and still be unfindable to the record
    # that needs it. A resident's date of birth sat on a Tenant Release and
    # Consent Form, a label no record type claims, and was reported to the
    # reviewer as "not found in source text" while being correct.
    packet_text = " ".join(
        g.combined_text for g in document_groups if g.category != "ignore"
    ).lower()

    # Fields that are authoritatively provided by the user (via API param /
    # frontend selector), NOT extracted from OCR. Source-verifying them
    # against OCR text is meaningless and produces false YELLOWs.
    _SKIP_SOURCE_VERIFY = {
        ("certification", "certificationType"),
    }

    # Score each field on each card
    for card in cards:
        sd = source_data.get(card.record_type)
        if not sd or not sd["text"]:
            continue

        source_text = sd["text"]
        ocr_good = sd["ocr_flag"] == "green"

        for fs in card.fields:
            if fs.value is None:
                continue

            # Caller-provided fields are not compared against the OCR text:
            # the caller states them (a frontend selector, or Cartograph's
            # notification), and forms spell the value out in words the code
            # does not contain — a TIC says "Move-in/Initial", not "MI" — so
            # comparing would fail on almost every packet.
            #
            # But skipping the check is not the same as passing it. This used
            # to append a 1.0 "source verification skipped", which asserted
            # the strongest possible confidence in the one value nothing had
            # examined. On a real packet Cartograph sent cert_type "annual"
            # for a certification the document marks Move-in in three places;
            # the engine adopted it, scored it green, and applied the wrong
            # rule set. Appending nothing instead leaves the field
            # unconfirmed, which is what it is.
            if (card.record_type, fs.field_name) in _SKIP_SOURCE_VERIFY:
                continue

            # Check if value appears in source text
            found = _value_in_source(fs.value, source_text)

            if found:
                # Value literally present in the OCR text — trust it.
                # Page-level OCR flags (watermark, yellow) don't matter if
                # the value was successfully extracted from that page.
                score = 1.0
                reason = "Verified in source"
            elif _value_in_source(fs.value, packet_text):
                # Present in the packet, but not in the documents this record
                # was extracted from. That rules out the model having invented
                # it, which is what this stage exists to detect, so it is not
                # scored as a miss. It is still worth separating from a clean
                # verification: a value corroborated only by an unrelated
                # document may belong to a different record.
                score = 0.85
                reason = "Found elsewhere in the packet, not in this record's documents"
            elif ocr_good:
                score = 0.50
                reason = "Not found in source text — verify manually"
            else:
                score = 0.30
                reason = f"Not found + poor OCR ({sd['ocr_flag']}) — unreliable"

            fs.stages.append(StageScore(
                stage="source_verification", score=score, reason=reason,
            ))
            fs.recompute()

    for card in cards:
        card.recompute()


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

    # Direct match
    if val in source_text:
        return True

    # Monetary: try multiple formats
    # "2479.00" → search for "2479", "2,479", "$2,479", "2479.00", "$2,479.00"
    cleaned = val.replace("$", "").replace(",", "").strip()
    if cleaned and cleaned in source_text:
        return True
    # Also try without trailing .00
    no_cents = re.sub(r"\.00$", "", cleaned)
    if no_cents and no_cents != cleaned and no_cents in source_text:
        return True
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
    "social security disability", "child support", "pension", "other income",
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

# Which record types a finding category is about, so a case-level dispute
# lands on the records that could have caused it rather than on all of them.
_CATEGORY_RECORD_TYPES = {
    "income": ("income",),
    "asset": ("asset",),
    "household_member": ("household_member",),
    "unit_rent": ("certification",),
    "expense": ("income", "asset"),
    "file_review": (),          # about the file, not about any extracted record
}


def score_findings(cards: list[RecordScoreCard], findings: list) -> None:
    """Let the audit's own findings lower the confidence of what they dispute.

    Every other stage asks a question of a value in isolation: is it populated,
    does it appear in the source text, does it satisfy a range. None of them
    can see that the household's income sums to 1,500 times what the
    certification declares, because that is a relationship between values
    rather than a property of one.

    So the strongest evidence the engine produces about its own reliability was
    excluded from its confidence score. On a real packet that meant an
    extraction reporting $48,360,000 of wages against a $31,470 certification
    scored 0.769 and flagged "yellow", with the field that caused it — a
    salary transcribed into a rate — scoring 0.96 green.

    Only findings that dispute the extraction are read; see findings.py for
    which those are and why a missing-document finding is not one of them.
    """
    disputing = [
        f for f in findings
        if isinstance(f, Finding) and f.disputes_extraction
    ]
    if not disputing:
        return

    for finding in disputing:
        subject = {
            slug(v) for v in (finding.subject_ref or {}).values() if v
        }
        targets = _CATEGORY_RECORD_TYPES.get(finding.category, ())
        # A dispute about records that do not exist has nowhere to land, and
        # that is the case where it matters most: when extraction produced
        # nothing there is no card to mark down, so the flat mean over the
        # remaining records rises. Observed live — a run that classified the
        # income pages as Unknown found no income at all and scored 0.907
        # green, higher than the run that found the household's $21,720.
        #
        # The certification card carries the declared totals the missing
        # records were supposed to account for, so it is where a
        # contradiction about absent records belongs.
        absent = bool(targets) and not any(c.record_type in targets for c in cards)
        if absent:
            targets = ("certification",)
        for card in cards:
            if card.record_type not in targets:
                continue
            # A finding naming a subject applies to the record carrying that
            # subject. Matching on the record's label rather than on a field
            # keeps this working for every record type without a per-type map.
            label_parts = {slug(p) for p in (card.record_label or "").split("—")}
            named = bool(subject and (subject & label_parts))
            if subject and not named:
                continue
            # A dispute whose records exist is shared among them: the
            # contradiction is real but which record carries the error is
            # unknown, so the penalty is moderate. A dispute whose records do
            # not exist at all admits no such ambiguity — the certification
            # declares a figure and the extraction produced nothing to set
            # against it. That is the strongest evidence of extraction failure
            # the engine can have, and it is scored as such.
            if named or absent:
                score = _DISPUTED_SCORE
            else:
                score = _DISPUTED_CASE_SCORE
            reason = f"Disputed by {finding.code}"
            for field in card.fields:
                if field.flag == ScoreFlag.NA:
                    continue
                update_field_score(
                    card, field.field_name, stage="finding",
                    score=score, reason=reason,
                )
        # Cards are recomputed once below rather than per finding.

    for card in cards:
        card.recompute()


def score_business_rules(
    cards: list[RecordScoreCard],
    certification_type: str | None = None,
) -> None:
    """Apply business rule checks to field values."""
    for card in cards:
        if card.record_type == "income":
            _score_income_rules(card, certification_type)
        elif card.record_type == "asset":
            _score_asset_rules(card)
        elif card.record_type == "household_member":
            _score_member_rules(card)
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

    # rateOfPay: numeric, > 0, < 50k
    rate = vals.get("rateOfPay")
    if rate:
        try:
            rate_num = float(rate.replace(",", ""))
            if rate_num <= 0:
                update_field_score(card, "rateOfPay", stage="business_rule",
                                   score=0.20, reason="Rate is zero or negative")
            elif rate_num > 50000:
                update_field_score(card, "rateOfPay", stage="business_rule",
                                   score=0.50, reason="Unusually high — verify monthly vs hourly")
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


def _score_member_rules(card: RecordScoreCard) -> None:
    vals = {f.field_name: f.value for f in card.fields}

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

    # disabled / student: Y or N
    for field_name in ("disabled", "student"):
        val = vals.get(field_name)
        if val in ("Y", "N"):
            update_field_score(card, field_name, stage="business_rule",
                               score=1.0, reason="Valid Y/N")
        elif val is None:
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
