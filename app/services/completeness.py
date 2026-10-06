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
from collections import Counter

from app.schemas.extraction import ExtractionResult
from app.services.doc_taxonomy import is_current_certification_form
from app.services.findings import (
    CATEGORY_INCOME,
    ASSIGN_INTERNAL,
    CATEGORY_ASSET,
    CATEGORY_MEMBER,
    RESOLVE_PRESENCE,
    make_finding,
)

logger = logging.getLogger(__name__)

# Currency written on a form: $1,234.56, 1234.56, 1,234.00 — and 17,874.
# A bare integer without cents is not matched: on a certification it is far
# more often a count, a year, a unit number or a percentage than an amount.
# A comma-grouped integer is matched: nothing else on a form is written
# that way, and the head of household's whole income sat on a TIC as
# "17,874" — the largest figure on the page, and the one line the scanner
# could not see while it flagged the $3,359.56 beside it.
_AMOUNT_RE = re.compile(
    r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})"
)

# Words a certification uses to label a figure that is not the household's:
# the program's income limits, and the statutory fines quoted in the perjury
# certification. Whole words, so "fine" does not fire inside "defined".
# Also a label class: the certification restates figures that are not the
# household's current income or assets — the income at move-in, the prior
# certification's numbers, adjusted or imputed derivations, the passbook
# rate. Each is a comparative or a computation, not a record the engine
# could have missed.
_NOT_HOUSEHOLD_RE = re.compile(
    r"\b(limits?|penalt(?:y|ies)|fined?|fines)\b|\bnot (?:less|more) than\b"
    r"|\bat move[\s-]?in\b|\bmove[\s-]?in income\b|\bprior\b|\bprevious\b"
    r"|\badjusted\b|\bimputed\b|\bpassbook\b|\binflation\b|\bfactor\b|\bthreshold\b"
    r"|\brent\b|\bsubsid(?:y|ies)\b"
    # A monthly figure, or a percentage of one, on an annual certification
    # is the form's own arithmetic ("Monthly Adjusted Income", "30% of
    # Monthly Adjusted Income"), never a source the engine could have missed.
    r"|\bmonthly\b|\d{1,2}\s?%\s+of\b|\bpercent\b",
    re.IGNORECASE,
)
# How far back to look for that label. Far enough for "Designated Income
# Limit x 140% (170% for Deep Rent Skewing): 70,728"; bounded by the previous
# amount regardless, so a long window cannot reach across a table cell.
_LABEL_WINDOW = 70

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
        # A substitution never touches the leading digit: "2000" and "5000"
        # are $20 and $50, two figures, not one misread.
        return a[:1] == b[:1] and sum(x != y for x, y in zip(a, b)) == 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1:] == shorter:
            return True
    return False


def _is_same_figure(amount: float, seen: set[float]) -> bool:
    """The same printed figure, allowing only a digit-level scan error.

    Used for the form's own scalars. The 0.1% drift that _is_near allows is
    right for a record value read twice, and wrong here: a 50059 printed
    "SS = Soc. Sec. 21,720" beside a total of 21,723, the drift rule called
    them one figure, and the one income line the extraction had missed
    raised no finding.
    """
    digits = _digits(amount)
    for other in seen:
        if other and abs(amount - other) <= 0.02:
            return True
        if _one_edit_apart(digits, _digits(other)):
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


def _amounts_in(text: str) -> Counter:
    """Every amount printed on the certification, with how many times.

    A Counter rather than a set because a figure printed twice is two
    claims. Two grandchildren on one TIC each drew the same $9,292.80 survivor
    benefit; the set saw one number, matched it against the one record that
    was extracted, and reported the certification fully accounted for while
    the second child's income was missing entirely.
    """
    out: Counter = Counter()
    prev_end = 0
    contexts: dict[float, set[str]] = {}
    _amounts_in.contexts = contexts
    for match in _AMOUNT_RE.finditer(text or ""):
        # The form labels what a figure is, and the label sits between the
        # previous number and this one. Read it: a certification prints
        # income limits, and its perjury boilerplate quotes statutory fines,
        # and neither is a household figure the extraction could have
        # missed. The window stops at the previous amount so a label is
        # only ever attributed to the figure it precedes.
        label = text[max(prev_end, match.start() - _LABEL_WINDOW):match.start()]
        prev_end = match.end()
        if _NOT_HOUSEHOLD_RE.search(label):
            continue
        try:
            amount = round(float(match.group(1).replace(",", "")), 2)
        except ValueError:
            continue
        out[amount] += 1
        # Which table the figure sits in, from the label beside it.
        low = label.lower()
        if _ASSET_LABEL_RE.search(low):
            contexts.setdefault(amount, set()).add("asset")
        elif _INCOME_LABEL_RE.search(low):
            contexts.setdefault(amount, set()).add("income")
    return out


_INCOME_LABEL_RE = re.compile(r"income|wage|salary|benefit|support|employ|pension|ssa|social security|annual")
_ASSET_LABEL_RE = re.compile(r"asset|balance|checking|savings|account|cash value|market value|equity|interest")


def _consume(amount: float, pool: Counter) -> bool:
    """Match an amount against the pool of extracted record values, using
    one up. Exact first, then the same tolerances _is_near allows."""
    if pool.get(amount, 0) > 0:
        pool[amount] -= 1
        return True
    digits = _digits(amount)
    for other, remaining in pool.items():
        if remaining <= 0 or not other:
            continue
        if (abs(amount - other) <= max(0.02, abs(other) * 0.001)
                or _one_edit_apart(digits, _digits(other))):
            pool[other] -= 1
            return True
    return False

def _extracted_amounts(extraction: ExtractionResult) -> tuple[Counter, set[float]]:
    """Every monetary value the extraction produced, split by what it is.

    Deliberately indiscriminate about fields: the question is not "is this
    value in the right field" but "did the engine see this number at all",
    so a figure landing in an unexpected field still counts as seen.

    Two pools, because two kinds of figure sit on a certification:

      - Record values — an income source's amount, an asset's balance. Each
        line on the form is one record, so each extracted record accounts
        for one printed line. Returned as a Counter, one count per record
        per distinct value, and consumed as lines are matched.
      - The form's own scalars — total income, rents, the limit. A
        certification restates these freely (a total appears in the income
        table and again in the eligibility section), so they match without
        limit.
    """
    pool: Counter = Counter()
    unlimited: set[float] = set()

    def as_float(value) -> float | None:
        if value is None:
            return None
        text = str(value).replace("$", "").replace(",", "").strip()
        try:
            return round(float(text), 2)
        except ValueError:
            return None

    def record(*values) -> None:
        distinct = {f for f in (as_float(v) for v in values) if f is not None}
        for f in distinct:
            pool[f] += 1

    info = extraction.certification_info
    if info is not None:
        for value in (
            info.householdIncome, info.grossRent, info.tenantRent,
            info.utilityAllowance, info.rentLimit,
        ):
            f = as_float(value)
            if f is not None:
                unlimited.add(f)

    for entry in extraction.income.sourceIncome.verificationIncome:
        record(
            entry.selfDeclaredAmount, entry.rateOfPay, entry.ytdAmount,
            entry.overtimeRate, entry.hoursPerPayPeriod,
            # the certification's own row for this source, once reconciled
            entry.declaredAnnualAmount,
        )
    for stub in extraction.income.sourceIncome.payStub:
        record(stub.grossPay, stub.ytdGross)

    for asset in extraction.assets.assetInformation:
        values = [
            asset.currentBalance, asset.selfDeclaredAmount,
            asset.averageSixMonthBalance, asset.incomeAmount,
        ]
        values += [st.balance for st in asset.bankStatment]
        if asset.verificationOfAsset is not None:
            values += [
                asset.verificationOfAsset.currentBalance,
                asset.verificationOfAsset.averageSixMonthBalance,
            ]
        record(*values)

    for calc in extraction.income_calculations:
        record(calc.annualIncome)

    return pool, unlimited

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

    pool, unlimited = _extracted_amounts(extraction)

    # The figures a certification most prominently states are its own
    # subtotals, which never appear as a single extracted value. Only sums
    # of two or more components count here: a single component is a
    # record's own value, which lives in the pool and is used once.
    components = [
        value for value in (
            [_as_float(c.annualIncome) for c in extraction.income_calculations]
            + [
                _as_float(a.currentBalance) or _as_float(a.selfDeclaredAmount)
                for a in extraction.assets.assetInformation
            ]
        ) if value
    ]
    unlimited |= _reachable_sums(components) - {round(c, 2) for c in components}

    unaccounted: list[float] = []
    for amount, printed in sorted(_amounts_in(text).items()):
        if amount < _MATERIALITY:
            continue
        for _ in range(printed):
            if _consume(amount, pool):
                continue
            if amount in unlimited or _is_same_figure(amount, unlimited):
                continue
            unaccounted.append(amount)
    if not unaccounted:
        return []

    contexts = getattr(_amounts_in, "contexts", {}) or {}
    by_table: dict[str, list[float]] = {}
    for amount in unaccounted:
        ctx = contexts.get(amount) or set()
        table = "asset" if ctx == {"asset"} else "income" if ctx == {"income"} else "unknown"
        by_table.setdefault(table, []).append(amount)
    out = []
    for table, amounts in by_table.items():
        listed = ", ".join(f"${a:,.2f}" for a in amounts[:_MAX_LISTED])
        if len(amounts) > _MAX_LISTED:
            listed += f" and {len(amounts) - _MAX_LISTED} more"
        where = {"asset": "asset", "income": "income", "unknown": "unplaced"}[table]
        out.append(make_finding(
            "CERT_AMOUNT_UNACCOUNTED",
            f"{len(amounts)} {where} amount(s) on the certification match no "
            f"extracted record: {listed}. Confirm no income source or asset was "
            f"missed — limits and subtotals are expected here, a balance or a "
            f"wage figure is not",
            label="Certification carries amounts that appear in no extracted record",
            # The table the figure sits in decides whose records the finding
            # is about; it used to be "asset" for every amount, so an income
            # line the extraction missed marked down the bank accounts.
            category=CATEGORY_ASSET if table == "asset" else CATEGORY_INCOME,
            subject_ref={"table": table},
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
        ))
    return out


def check_completeness(extraction: ExtractionResult) -> list:
    """Run every completeness check and return the findings raised."""
    findings: list = []
    findings.extend(check_household_size(extraction))
    findings.extend(check_unaccounted_amounts(extraction))
    if findings:
        logger.info("Completeness checks raised %d finding(s)", len(findings))
    return findings
