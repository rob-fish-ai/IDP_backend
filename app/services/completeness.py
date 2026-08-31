"""Check an extraction against the certification form's own account of itself.

Every packet contains an independent statement of the same facts: the
property manager filled in a certification declaring how many people live in
the unit, what they earn, and what they hold. That declaration was produced
by a different process than the extraction — by a person, from the same
documents — which is exactly what makes it useful as a check.

Until now that role was played by the MuleSoft extraction. A disagreement
between the two was free evidence that something was wrong, and it is the
reason most extraction faults were ever noticed. MuleSoft retires with
Salesforce at the end of 2026 and takes that signal with it, leaving the
engine as the sole extractor with nothing to contradict it.

These checks are the replacement. They are deliberately not a second
extraction — a second model would share the first one's blind spots and cost
as much again. They are arithmetic and set comparison against figures the
form already states, so they cost nothing per case and they detect the
failure mode that matters most: an extraction that is confidently
incomplete.

What they cannot do is decide who is right. A disagreement means one of the
two is wrong, and which one is a question for a reviewer — so these produce
findings for review rather than corrections.
"""

import logging
import re

from app.schemas.extraction import ExtractionResult
from app.services.doc_taxonomy import is_current_certification_form
from app.services.findings import (
    ASSIGN_INTERNAL,
    CATEGORY_ASSET,
    CATEGORY_MEMBER,
    RESOLVE_PRESENCE,
    make_finding,
)

logger = logging.getLogger(__name__)

# Currency written on a form: $1,234.56, 1234.56, 1,234.00. Cents are
# required — bare integers on a certification are far more often a count, a
# year, a unit number or a percentage than an amount.
_AMOUNT_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})")

# Amounts below this are not worth a reviewer's attention even when
# unexplained: fees, cents-level differences, incidental figures printed on
# the form. Above it, an unexplained amount on a certification is either a
# record the engine missed or a figure the reviewer should be able to place.
_MATERIALITY = 500.0

# An unexplained-amounts finding that lists dozens of figures is noise a
# reviewer skips. Beyond this the finding says how many more there were.
_MAX_LISTED = 8


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return round(float(str(value).replace("$", "").replace(",", "").strip()), 2)
    except ValueError:
        return None


def _digits(value: float) -> str:
    return str(value).replace(".", "").replace("-", "").lstrip("0") or "0"


def _one_edit_apart(a: str, b: str) -> bool:
    """Whether two digit strings differ by a single insertion or substitution.

    A figure read twice from a scan differs by a digit, not by a plausible
    amount: 62,926.52 and 662,926.52 are the same number with one character
    duplicated. Treating that as a missing record would send a reviewer
    looking for an income source that does not exist.
    """
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1:] == shorter:
            return True
    return False


def _is_near(amount: float, seen: set[float]) -> bool:
    """Whether an amount is the same figure as something extracted.

    Two ways the same number arrives differently: a small numeric drift from
    a misread cent or a rounding difference, and a digit-level scan error
    that changes the magnitude entirely. Both mean the engine saw the figure;
    neither means a record is missing.
    """
    digits = _digits(amount)
    for other in seen:
        if other and abs(amount - other) <= max(0.02, abs(other) * 0.001):
            return True
        if _one_edit_apart(digits, _digits(other)):
            return True
    return False


def _reachable_sums(components: list[float], cap: int = 20000) -> set[float]:
    """Totals obtainable by adding up some combination of extracted values.

    A certification states subtotals — income per member, a household total,
    a total for assets — and those are the figures a reader most expects to
    see. None of them appears as a single extracted value, so without this
    every packet would report its own arithmetic as unexplained.
    """
    sums: set[float] = {0.0}
    for value in components:
        if not value:
            continue
        sums |= {round(existing + value, 2) for existing in sums}
        if len(sums) > cap:
            break
    sums.discard(0.0)
    return sums


def _amounts_in(text: str) -> set[float]:
    out: set[float] = set()
    for match in _AMOUNT_RE.finditer(text or ""):
        try:
            out.add(round(float(match.group(1).replace(",", "")), 2))
        except ValueError:
            continue
    return out


def _extracted_amounts(extraction: ExtractionResult) -> set[float]:
    """Every monetary value the extraction produced, from anywhere.

    Deliberately indiscriminate. The question this answers is not "is this
    value in the right field" but "did the engine see this number at all",
    so a figure landing in an unexpected field still counts as seen.
    """
    found: set[float] = set()

    def absorb(value) -> None:
        if value is None:
            return
        text = str(value).replace("$", "").replace(",", "").strip()
        try:
            found.add(round(float(text), 2))
        except ValueError:
            return

    info = extraction.certification_info
    if info is not None:
        for field in (
            info.householdIncome, info.grossRent, info.tenantRent,
            info.utilityAllowance, info.rentLimit,
        ):
            absorb(field)

    for entry in extraction.income.sourceIncome.verificationIncome:
        for field in (
            entry.selfDeclaredAmount, entry.rateOfPay, entry.ytdAmount,
            entry.overtimeRate, entry.hoursPerPayPeriod,
        ):
            absorb(field)
    for stub in extraction.income.sourceIncome.payStub:
        absorb(stub.grossPay)
        absorb(stub.ytdGross)

    for asset in extraction.assets.assetInformation:
        for field in (
            asset.currentBalance, asset.selfDeclaredAmount,
            asset.averageSixMonthBalance, asset.incomeAmount,
        ):
            absorb(field)
        for statement in asset.bankStatment:
            absorb(statement.balance)
        if asset.verificationOfAsset is not None:
            absorb(asset.verificationOfAsset.currentBalance)
            absorb(asset.verificationOfAsset.averageSixMonthBalance)

    for calc in extraction.income_calculations:
        absorb(calc.annualIncome)

    return found


def _certification_text(extraction: ExtractionResult) -> str:
    """Text of the certification form itself, excluding prior-year copies.

    A previous certification restates last year's figures, none of which
    should appear in this year's records. Including it would make every
    packet carrying one look full of unexplained amounts.
    """
    parts = [
        group.combined_text or ""
        for group in extraction.document_groups
        if group.category != "ignore"
        and is_current_certification_form(group.document_type)
    ]
    return "\n".join(parts)


def check_household_size(extraction: ExtractionResult) -> list:
    """Compare the declared household size against the roster extracted."""
    info = extraction.certification_info
    if info is None or not info.householdSize:
        return []
    try:
        declared = int(str(info.householdSize).strip())
    except ValueError:
        return []

    extracted = len(extraction.household_demographics.houseHold)
    if declared == extracted or declared <= 0:
        return []

    direction = "fewer" if extracted < declared else "more"
    return [make_finding(
        "HH_SIZE_MISMATCH",
        f"Household size mismatch: the certification declares {declared} "
        f"member(s) but {extracted} were extracted ({direction} than "
        f"declared) — confirm the roster is complete",
        label="Declared household size does not match the extracted roster",
        category=CATEGORY_MEMBER,
        assignment=ASSIGN_INTERNAL,
        correction_required=(
            "Reconcile the household roster against the certification's "
            "household composition section"
        ),
        resolution_type=RESOLVE_PRESENCE,
    )]


def check_unaccounted_amounts(extraction: ExtractionResult) -> list:
    """Report material amounts on the certification that match nothing extracted.

    The certification itemizes the household's income and assets. An amount
    printed there that appears in no extracted record is either a record the
    engine missed or a figure with an innocent explanation — a limit, a
    subtotal, a prior-year comparison. Which one is a judgement, so this
    reports rather than concludes.

    The check is deliberately blunt: any extracted value anywhere counts as
    accounting for an amount. That keeps it quiet when extraction merely put
    a figure in an unexpected field, and loud only when the engine appears
    not to have seen the number at all — which is the case worth a reviewer's
    time and the one that used to be caught by disagreeing with MuleSoft.
    """
    text = _certification_text(extraction)
    if not text:
        return []

    seen = _extracted_amounts(extraction)

    # The figures a certification most prominently states are its own
    # subtotals, which never appear as a single extracted value.
    components = [
        value for value in (
            [_as_float(c.annualIncome) for c in extraction.income_calculations]
            + [
                _as_float(a.currentBalance) or _as_float(a.selfDeclaredAmount)
                for a in extraction.assets.assetInformation
            ]
        ) if value
    ]
    seen |= _reachable_sums(components)

    unaccounted = sorted(
        amount for amount in _amounts_in(text)
        if amount >= _MATERIALITY
        and amount not in seen
        and not _is_near(amount, seen)
    )
    if not unaccounted:
        return []

    listed = ", ".join(f"${a:,.2f}" for a in unaccounted[:_MAX_LISTED])
    if len(unaccounted) > _MAX_LISTED:
        listed += f" and {len(unaccounted) - _MAX_LISTED} more"

    return [make_finding(
        "CERT_AMOUNT_UNACCOUNTED",
        f"{len(unaccounted)} amount(s) on the certification match no "
        f"extracted record: {listed}. Confirm no income source or asset was "
        f"missed — limits and subtotals are expected here, a balance or a "
        f"wage figure is not",
        label="Certification carries amounts that appear in no extracted record",
        category=CATEGORY_ASSET,
        # Not a compliance failure by the file: it flags that the extraction
        # may be incomplete, which is a question about the audit rather than
        # about the household.
        result="na",
        assignment=ASSIGN_INTERNAL,
        correction_required=(
            "Check each amount against the household's income sources and "
            "assets; add any record the extraction missed"
        ),
        resolution_type=RESOLVE_PRESENCE,
    )]


def check_completeness(extraction: ExtractionResult) -> list:
    """Run every completeness check and return the findings raised."""
    findings: list = []
    findings.extend(check_household_size(extraction))
    findings.extend(check_unaccounted_amounts(extraction))
    if findings:
        logger.info("Completeness checks raised %d finding(s)", len(findings))
    return findings
