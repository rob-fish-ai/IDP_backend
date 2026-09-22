"""Field-level confidence scoring — multi-stage validation pipeline.

Each extracted field accumulates evidence from multiple stages:
  Stage 1  Extraction: is the field populated? Does the value look valid?
  Stage 2  Cross-document consistency: do multiple sources agree?
  Stage 3  Business-rule validation: range, format, logical checks

The composite score drives a green / yellow / red flag per field.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class ScoreFlag(str, Enum):
    GREEN = "green"      # high confidence — no review needed
    NA = "na"            # field not applicable for this record type
    YELLOW = "yellow"    # moderate — review recommended
    RED = "red"          # low — likely wrong or missing, must review


# Thresholds (composite score 0-1)
GREEN_THRESHOLD = 0.80
YELLOW_THRESHOLD = 0.50

# Stage weights for LLM-only architecture
WEIGHT_EXTRACTION = 0.20        # is the field populated?
WEIGHT_SOURCE_VERIFY = 0.30     # is the value found in source text + OCR quality?
WEIGHT_CROSS_DOC = 0.15         # do multiple documents agree?
WEIGHT_BUSINESS_RULE = 0.35     # does the value pass format/range/logic checks?
# Does an audit finding contradict this value? Weighted highest because it is
# the only stage that compares the extraction against the document's own
# account of itself rather than against a format or a range. A declared total
# the extracted sources do not sum to is the strongest evidence the engine
# produces that it misread something, and until this stage existed the score
# could not see it: an extraction whose income was 1,500x the certified total
# scored 0.769 "yellow".
WEIGHT_FINDING = 0.40

# Ceiling for a field that no verification stage examined. Just under the
# green threshold: unconfirmed is its own state, distinct from both
# "confirmed good" and "found wrong".
UNVERIFIED_CEILING = 0.79


# Ordering used to take the worse of two flags. NA is not a severity — it
# means the field does not apply — so it sits with GREEN.
_FLAG_SEVERITY = {
    ScoreFlag.NA: 0, ScoreFlag.GREEN: 0, ScoreFlag.YELLOW: 1, ScoreFlag.RED: 2,
}
_SEVERITY_FLAG = {0: ScoreFlag.GREEN, 1: ScoreFlag.YELLOW, 2: ScoreFlag.RED}


def compute_flag(composite: float) -> ScoreFlag:
    if composite >= GREEN_THRESHOLD:
        return ScoreFlag.GREEN
    if composite >= YELLOW_THRESHOLD:
        return ScoreFlag.YELLOW
    return ScoreFlag.RED


# ---------------------------------------------------------------------------
# Per-field score
# ---------------------------------------------------------------------------

class StageScore(BaseModel):
    """Score from one validation stage."""
    stage: str                          # "extraction", "cross_doc", "business_rule"
    score: float = Field(ge=0.0, le=1.0)
    reason: Optional[str] = None        # human-readable explanation
    # A stage may cap the field's composite regardless of the other stages:
    # a value found only on the certification form, or matched weakly, can
    # score well on shape and still must not read as green.
    ceiling: Optional[float] = None


class FieldScore(BaseModel):
    """Accumulated confidence for a single extracted field."""
    field_name: str
    value: Optional[str] = None         # the extracted value (for display)
    stages: list[StageScore] = []
    composite: float = Field(default=0.0, ge=0.0, le=1.0)
    flag: ScoreFlag = ScoreFlag.RED
    flag_message: Optional[str] = None  # summary for UI / findings

    def mark_na(self, reason: str = "Not applicable for this income type") -> None:
        """Mark field as N/A — excluded from scoring."""
        self.stages = [StageScore(stage="na", score=1.0, reason=reason)]
        self.composite = 1.0
        self.flag = ScoreFlag.NA
        self.flag_message = reason

    def recompute(self) -> None:
        """Recalculate composite from stage scores using weights."""
        if any(s.stage == "na" for s in self.stages):
            self.composite = 1.0
            self.flag = ScoreFlag.NA
            self.flag_message = next(
                (s.reason for s in self.stages if s.stage == "na"), "N/A"
            )
            return

        weight_map = {
            "extraction": WEIGHT_EXTRACTION,
            "source_verification": WEIGHT_SOURCE_VERIFY,
            "cross_doc": WEIGHT_CROSS_DOC,
            "business_rule": WEIGHT_BUSINESS_RULE,
            "finding": WEIGHT_FINDING,
        }
        total_weight = 0.0
        weighted_sum = 0.0
        for s in self.stages:
            w = weight_map.get(s.stage, 0.10)
            weighted_sum += s.score * w
            total_weight += w
        self.composite = weighted_sum / total_weight if total_weight > 0 else 0.0

        # Dividing by the weight of the stages that RAN means a field nobody
        # checked scores exactly what its extraction stage gave it — 0.85 for
        # "the model returned something", which is above the green threshold.
        # So "verified and correct" and "never verified" both came out green,
        # and the flag a reviewer triages by could not tell them apart.
        #
        # A value no verification stage examined is not confirmed; it is
        # unconfirmed. It is capped below green rather than pushed down to
        # red, because nothing has been found wrong with it either.
        # Specifically source verification, not any check at all. A business
        # rule asks whether a value has a plausible shape — that a cert type
        # is one of four codes, that a rate is under a bound. Only source
        # verification asks whether the value is what the document says.
        # Green should mean "found in the document", so a value never
        # compared against it cannot earn one.
        verified = any(
            s.stage == "source_verification" for s in self.stages
        )
        if not verified:
            self.composite = min(self.composite, UNVERIFIED_CEILING)
        for s in self.stages:
            if s.ceiling is not None:
                self.composite = min(self.composite, s.ceiling)

        self.flag = compute_flag(self.composite)

        # The flag message names what held the field back. A stage that
        # scored well but set a ceiling counts, and so does the unverified
        # cap: "Review recommended" with no reason was what two thirds of
        # the yellow fields on the delivered cases said.
        if self.flag == ScoreFlag.RED:
            reasons = [s.reason for s in self.stages if s.score < YELLOW_THRESHOLD and s.reason]
            self.flag_message = "; ".join(reasons) if reasons else "Low confidence — manual review required"
        elif self.flag == ScoreFlag.YELLOW:
            reasons = [
                s.reason for s in self.stages
                if s.reason and (s.score < GREEN_THRESHOLD or s.ceiling is not None)
            ]
            if not reasons and not verified:
                reasons = ["Not verified against a source document"]
            self.flag_message = "; ".join(reasons) if reasons else "Review recommended"
        else:
            self.flag_message = None


# ---------------------------------------------------------------------------
# Per-record (row) score card
# ---------------------------------------------------------------------------

class RecordScoreCard(BaseModel):
    """Score card for one extracted record (e.g., one VerificationIncomeEntry)."""
    record_type: str                    # "income", "asset", "household_member", "certification"
    record_label: Optional[str] = None
    fields: list[FieldScore] = []
    # Provenance carried from the record: the packet pages it was read from
    # and the reconciliation verdict (verified / declared_only / ...). Source
    # verification checks a value against these pages first.
    source_pages: list[int] = []
    verification_status: Optional[str] = None
    # What verified an income record (type_of_VOI); the rules read it here.
    voi_type: Optional[str] = None
    # Set when a finding names this record: the contradiction is about this
    # record specifically, so it cannot read as green however its other
    # fields score.
    disputed: bool = False
    composite: float = Field(default=0.0, ge=0.0, le=1.0)
    flag: ScoreFlag = ScoreFlag.RED

    def recompute(self) -> None:
        """Recalculate record composite from field composites (excluding N/A)."""
        scored = [f for f in self.fields if f.flag != ScoreFlag.NA]
        if not scored:
            self.composite = 1.0 if self.fields else 0.0
            self.flag = ScoreFlag.GREEN if self.fields else ScoreFlag.RED
            return
        self.composite = sum(f.composite for f in scored) / len(scored)
        # A record nothing third-party verifies — the household's own
        # declaration kept because no source document carries it — cannot
        # be green as a whole, however well its picklist fields verify
        # against the form it was declared on.
        if self.verification_status == "declared_only" or self.disputed:
            self.composite = min(self.composite, UNVERIFIED_CEILING)
        self.flag = compute_flag(self.composite)

    @property
    def flagged_fields(self) -> list[FieldScore]:
        return [f for f in self.fields if f.flag not in (ScoreFlag.GREEN, ScoreFlag.NA)]


# ---------------------------------------------------------------------------
# Pipeline-level score summary
# ---------------------------------------------------------------------------

class ExtractionScoreSummary(BaseModel):
    """Top-level scoring summary for the entire extraction."""
    records: list[RecordScoreCard] = []
    overall_composite: float = Field(default=0.0, ge=0.0, le=1.0)
    overall_flag: ScoreFlag = ScoreFlag.RED
    total_fields: int = 0
    green_fields: int = 0
    yellow_fields: int = 0
    red_fields: int = 0
    na_fields: int = 0

    def recompute(self) -> None:
        """Recalculate from all records (N/A excluded from composite)."""
        all_fields = [f for r in self.records for f in r.fields]
        self.total_fields = len(all_fields)
        self.green_fields = sum(1 for f in all_fields if f.flag == ScoreFlag.GREEN)
        self.yellow_fields = sum(1 for f in all_fields if f.flag == ScoreFlag.YELLOW)
        self.red_fields = sum(1 for f in all_fields if f.flag == ScoreFlag.RED)
        self.na_fields = sum(1 for f in all_fields if f.flag == ScoreFlag.NA)
        scored = [f for f in all_fields if f.flag != ScoreFlag.NA]
        if scored:
            self.overall_composite = sum(f.composite for f in scored) / len(scored)
        else:
            self.overall_composite = 1.0 if all_fields else 0.0
        self.overall_flag = compute_flag(self.overall_composite)

        # The composite is an honest mean and stays one. The flag is not a
        # mean — it is what a reviewer triages on, and averaging a disputed
        # record away behind a dozen untouched ones hides exactly the thing
        # they need to see.
        #
        # A dispute says the values it names cannot all be right. So the
        # summary cannot be greener than the worst record carrying one, no
        # matter how many clean records sit beside it. Without this rule a run
        # that found no income at all on a household certifying $21,723 still
        # flagged green, because the two asset records and the member record
        # were fine and there was no income record left to be wrong.
        disputed = [
            r for r in self.records
            if any(s.stage == "finding" for f in r.fields for s in f.stages)
        ]
        if disputed:
            worst = max(_FLAG_SEVERITY[r.flag] for r in disputed)
            if worst > _FLAG_SEVERITY[self.overall_flag]:
                self.overall_flag = _SEVERITY_FLAG[worst]

        # Income that only the household declares is income nobody verified.
        # A packet whose income records are all declared-only can average
        # green on its identity and certification fields while the audit's
        # central question — is the income what the file says — is open.
        # (On a self-certification the same records carry status
        # "self_certified" and do not bound the flag.)
        if any(r.record_type == "income" and r.verification_status == "declared_only"
               for r in self.records):
            if _FLAG_SEVERITY[ScoreFlag.YELLOW] > _FLAG_SEVERITY[self.overall_flag]:
                self.overall_flag = ScoreFlag.YELLOW
