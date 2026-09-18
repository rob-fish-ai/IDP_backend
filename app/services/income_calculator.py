"""Income calculation engine — computes annual income using four methods (Section 9)."""

import logging
import re
from datetime import date, datetime
from statistics import median

from app.schemas.extraction import (
    IncomeCalculationResult,
    PayStubEntry,
    VerificationIncomeEntry,
)
from app.services.hours_resolver import resolve_hours_range

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frequency vocabulary
# ---------------------------------------------------------------------------

FREQUENCY_MULTIPLIERS: dict[str, int] = {
    "weekly": 52,
    "bi-weekly": 26,
    "semi-monthly": 24,
    "monthly": 12,
    "quarterly": 4,
    "annually": 1,
}

# Every spelling a document (or the model paraphrasing one) uses for a pay
# frequency, mapped to the canonical key above. Exact matches are tried
# first; the ordered substring rules below catch the rest ("Biweekly",
# "every other Friday", "twice a month", "per annum").
_FREQUENCY_ALIASES: dict[str, str] = {}
for _canon, _spellings in {
    "weekly": ("weekly", "week", "wk", "per week", "every week", "each week", "once a week", "w", "52"),
    "bi-weekly": ("bi-weekly", "biweekly", "bi weekly", "bi-wkly", "biwkly", "every two weeks",
                  "every 2 weeks", "every other week", "fortnightly", "b/w", "bw", "26"),
    "semi-monthly": ("semi-monthly", "semimonthly", "semi monthly", "twice a month", "twice monthly",
                     "twice per month", "1st and 15th", "15th and 30th", "15th and last",
                     "1st & 15th", "s/m", "sm", "24"),
    "monthly": ("monthly", "month", "mo", "per month", "every month", "each month", "once a month",
                "mthly", "m", "12"),
    "quarterly": ("quarterly", "quarter", "every three months", "every 3 months", "qtrly", "q", "4"),
    "annually": ("annually", "annual", "yearly", "year", "per year", "per annum", "a year",
                 "salary", "yr", "1"),
}.items():
    for _s in _spellings:
        _FREQUENCY_ALIASES[_s] = _canon

_FREQUENCY_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"hour|hrly|\bhr\b|/hr"), "hourly"),
    (re.compile(r"bi[\s-]*week|every\s*(two|2|other)\s*(week|[a-z]+day)|fortnight"), "bi-weekly"),
    (re.compile(r"semi[\s-]*month|twice\s*(a|per)?\s*month|1st\s*(and|&)\s*15th|15th\s*(and|&)"), "semi-monthly"),
    (re.compile(r"week|every\s*[a-z]+day"), "weekly"),
    (re.compile(r"quarter|every\s*(three|3)\s*month"), "quarterly"),
    (re.compile(r"month"), "monthly"),
    (re.compile(r"annu|year|salar"), "annually"),
    (re.compile(r"\bdaily\b|per\s*day"), "daily"),
)


def normalize_frequency(text: str | None) -> str | None:
    """Canonical pay frequency for any spelling, or None.

    Returns one of the FREQUENCY_MULTIPLIERS keys, or "hourly" / "daily"
    when the text names a rate unit rather than a pay frequency — callers
    that want a multiplier get None for those, callers that want to know
    the model confused the two can see it.
    """
    if not text:
        return None
    t = re.sub(r"\s+", " ", str(text).strip().lower())
    t = t.strip(" .,;:()[]\"'")
    t = re.sub(r"^(paid|pay|payment|payments|pay frequency|frequency)[:\s]+", "", t)
    if not t:
        return None
    if t in _FREQUENCY_ALIASES:
        return _FREQUENCY_ALIASES[t]
    for rx, canon in _FREQUENCY_RULES:
        if rx.search(t):
            return canon
    return None


def get_frequency_multiplier(frequency: str | None) -> int | None:
    """Return the annual multiplier for a pay frequency (any spelling)."""
    canon = normalize_frequency(frequency)
    if canon is None:
        return None
    return FREQUENCY_MULTIPLIERS.get(canon)


# The unit of rateOfPay. A pay frequency answers "how often is the person
# paid"; the rate unit answers "what does the number mean": an hourly
# figure needs hours to become income, a period figure needs the period's
# multiplier, an annual salary is already income.
RATE_UNITS: tuple[str, ...] = ("hourly", "daily", "weekly", "bi-weekly", "semi-monthly",
                               "monthly", "quarterly", "annually", "per_period")
_PER_PERIOD_RE = re.compile(r"per\s*(pay\s*)?period|pay\s*period|/pp|\bpp\b|per\s*check|per\s*paycheck")

# An "hourly" rate above this is not an hourly rate: it is a per-period or
# annual figure whose unit the model mislabelled. The annual salary that
# arrived as rateOfPay=48360 with hours=80 multiplied to $48,360,000/yr.
_MAX_HOURLY_RATE = 300.0
_HOURS_PER_YEAR = 2080.0


def normalize_rate_unit(text: str | None) -> str | None:
    """Canonical rate unit, or None when the text names none."""
    if not text:
        return None
    t = str(text).strip().lower()
    if _PER_PERIOD_RE.search(t) or t in ("per_period", "period", "periodic"):
        return "per_period"
    canon = normalize_frequency(t)
    if canon in RATE_UNITS:
        return canon
    return None


# ---------------------------------------------------------------------------
# Individual calculation methods
# ---------------------------------------------------------------------------

def _money(value) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def calculate_self_declared(
    amount: str | None,
    frequency: str | None = None,
) -> str | None:
    """Self-declared income annualized by frequency.

    The LLM often extracts fixed-benefit amounts (SSA, pension, child support)
    into selfDeclaredAmount with frequencyOfPay="monthly". A monthly $1,414
    must become $16,968 annual, not $1,414. When frequency is missing or
    "annually", the amount is returned as-is.
    """
    val = _money(amount)
    if val is None:
        return None
    mult = get_frequency_multiplier(frequency) if frequency else None
    if mult and mult > 1:
        val = val * mult
    return f"{val:.2f}"


def calculate_voi_based(
    rate_of_pay: str | None,
    frequency_of_pay: str | None,
    hours_per_pay_period: str | None,
    overtime_rate: str | None = None,
    overtime_frequency: str | None = None,
    funding_program: str | None = None,
    rate_unit: str | None = None,
    calc_mode: str = "employment",
) -> tuple[str | None, str | None, list[str]]:
    """VOI-based annual income from a rate, its unit, and the pay frequency.

        hourly rate      → rate × hours per pay period × pay periods per year
        period rate      → rate × that period's multiplier (hours are not a factor)
        annual salary    → rate
        unit not stated  → inferred: a rate with hours that could be an hourly
                           wage is hourly; a rate without hours is per pay period

    Returns:
        (annual_income, details_string, list_of_findings). A details string
        beginning "[rejected]" explains why no income could be computed from
        this record — the caller reports it instead of silently moving on.
    """
    findings: list[str] = []

    rate = _money(rate_of_pay)
    if rate is None:
        return None, None, findings

    freq = normalize_frequency(frequency_of_pay)
    multiplier = FREQUENCY_MULTIPLIERS.get(freq) if freq else None
    unit = normalize_rate_unit(rate_unit)

    hours = None
    if hours_per_pay_period:
        hours, hours_finding = resolve_hours_range(hours_per_pay_period, funding_program)
        if hours_finding:
            findings.append(hours_finding)

    # Infer the unit when the record does not state one.
    if unit is None:
        if hours is not None:
            if rate > _MAX_HOURLY_RATE:
                return None, (
                    f"[rejected] rateOfPay {rate:,.2f} with {hours:g} hours per pay period "
                    f"is not a plausible hourly rate (over {_MAX_HOURLY_RATE:,.0f}/hr) and the "
                    f"rate unit is not stated — annual income not computed; confirm whether "
                    f"the figure is a salary or a per-period amount"
                ), findings
            unit = "hourly"
        elif calc_mode == "employment" and rate <= _MAX_HOURLY_RATE and freq not in ("annually",):
            return None, (
                f"[rejected] rateOfPay {rate:,.2f} reads as an hourly wage but the record "
                f"states no hours per pay period and no rate unit — annual income not computed"
            ), findings
        else:
            unit = "per_period"

    if unit == "hourly":
        if hours is None:
            return None, (
                f"[rejected] hourly rate {rate:,.2f} without hours per pay period — "
                f"annual income not computed"
            ), findings
        if multiplier is None:
            if frequency_of_pay:
                return None, (
                    f"[rejected] pay frequency '{frequency_of_pay}' is not recognised — "
                    f"annual income not computed"
                ), findings
            return None, (
                f"[rejected] hourly rate {rate:,.2f} × {hours:g} hours per pay period "
                f"but the pay frequency is not stated — annual income not computed"
            ), findings
        annual = rate * hours * multiplier
        details = f"{rate:g}/hr × {hours:g} hrs/pp × {multiplier} pp/yr = {annual:.2f}"
    elif unit == "daily":
        if hours is None or multiplier is None:
            return None, (
                f"[rejected] daily rate {rate:,.2f} needs days per pay period and a pay "
                f"frequency — annual income not computed"
            ), findings
        annual = rate * hours * multiplier
        details = f"{rate:g}/day × {hours:g} days/pp × {multiplier} pp/yr = {annual:.2f}"
    elif unit == "annually":
        annual = rate
        details = f"annual salary {rate:.2f}"
    elif unit == "per_period":
        if multiplier is None:
            if frequency_of_pay:
                return None, (
                    f"[rejected] pay frequency '{frequency_of_pay}' is not recognised — "
                    f"annual income not computed"
                ), findings
            if calc_mode == "fixed_monthly":
                multiplier, freq = 12, "monthly"
            else:
                return None, (
                    f"[rejected] periodic rate {rate:,.2f} without a pay frequency — "
                    f"annual income not computed"
                ), findings
        annual = rate * multiplier
        details = f"{rate:.2f} per {freq} period × {multiplier} = {annual:.2f}"
    else:
        unit_mult = FREQUENCY_MULTIPLIERS[unit]
        annual = rate * unit_mult
        details = f"{rate:.2f} {unit} × {unit_mult} = {annual:.2f}"
        if freq and freq != unit and freq in FREQUENCY_MULTIPLIERS:
            details += f" (paid {freq})"

    # Overtime. A per-period overtime amount annualises by its own frequency
    # (or the regular one); an hourly overtime rate without overtime hours
    # cannot be turned into income and is left out, stated.
    ot_rate = _money(overtime_rate)
    overtime_annual = 0.0
    if ot_rate:
        if unit == "hourly" and ot_rate <= _MAX_HOURLY_RATE:
            details += f" (overtime rate {ot_rate:g}/hr stated without overtime hours — not included)"
        else:
            ot_multiplier = get_frequency_multiplier(overtime_frequency) or multiplier or 1
            overtime_annual = ot_rate * ot_multiplier
            details += f" + OT {ot_rate:g} × {ot_multiplier} = {overtime_annual:.2f}"

    total = annual + overtime_annual
    return f"{total:.2f}", details, findings


def calculate_ytd_based(
    ytd_amount: str | None,
    ytd_start_date: str | None,
    ytd_end_date: str | None,
) -> tuple[str | None, str | None]:
    """YTD-based annual income: ytd_amount / days_elapsed × 365.

    Returns:
        (annual_income, details_string)
    """
    ytd = _money(ytd_amount)
    if ytd is None:
        return None, None

    start = _parse_date(ytd_start_date)
    end = _parse_date(ytd_end_date)

    if not start or not end:
        return None, "YTD dates missing — cannot annualize"

    days = (end - start).days
    if days <= 0:
        return None, f"Invalid YTD period: {ytd_start_date} to {ytd_end_date}"

    annual = ytd / days * 365
    details = f"{ytd:.2f} / {days} days × 365 = {annual:.2f}"
    return f"{annual:.2f}", details


def calculate_paystub_ytd(
    paystubs: list[PayStubEntry],
    hire_date: str | None = None,
) -> tuple[str | None, str | None]:
    """Project annual income from the newest stub's own YTD gross.

    Every stub prints a year-to-date figure that the VOI-style ytdAmount
    field never sees; it is the cheapest audit of the stub average. The
    window runs from January 1 of the pay year, or from the hire date when
    the job started that year.
    """
    best: tuple[date, float] | None = None
    for ps in paystubs:
        ytd = _money(ps.ytdGross)
        pay_date = _parse_date(ps.payDate)
        if ytd is None or ytd <= 0 or pay_date is None:
            continue
        if best is None or pay_date > best[0]:
            best = (pay_date, ytd)
    if best is None:
        return None, None
    pay_date, ytd = best
    start = date(pay_date.year, 1, 1)
    hired = _parse_date(hire_date)
    if hired and start < hired < pay_date:
        start = hired
    days = (pay_date - start).days
    if days <= 0:
        return None, None
    annual = ytd / days * 365
    details = (
        f"paystub YTD {ytd:.2f} ({start.isoformat()} to {pay_date.isoformat()}) "
        f"/ {days} days × 365 = {annual:.2f}"
    )
    return f"{annual:.2f}", details


def calculate_paystub_based(
    paystubs: list[PayStubEntry],
    pay_interval: str | None = None,
) -> tuple[str | None, str | None]:
    """Pay-stub-based annual income: average gross × frequency multiplier.

    Args:
        paystubs: list of PayStubEntry for one income source
        pay_interval: override frequency (if not on individual stubs)

    Returns:
        (annual_income, details_string)
    """
    if not paystubs:
        return None, None

    amounts = []
    freq = pay_interval
    for ps in paystubs:
        gross = _money(ps.grossPay)
        if gross is not None:
            amounts.append(gross)
        if not freq and ps.payInterval:
            freq = ps.payInterval

    if not amounts:
        return None, None

    avg = sum(amounts) / len(amounts)
    multiplier = get_frequency_multiplier(freq)
    if multiplier is None:
        # The stubs' own dates say how often the person is paid.
        inferred = _infer_periodicity([d for d in (_parse_date(ps.payDate) for ps in paystubs) if d])
        if inferred:
            freq, multiplier = inferred, FREQUENCY_MULTIPLIERS[inferred]
        else:
            return None, f"[rejected] pay interval '{freq or 'not stated'}' is not recognised and the stub dates do not show one"

    annual = avg * multiplier
    details = f"avg({len(amounts)} stubs) = {avg:.2f} × {multiplier} = {annual:.2f}"
    return f"{annual:.2f}", details


# ---------------------------------------------------------------------------
# Payment histories (child support ledgers, benefit payment records)
# ---------------------------------------------------------------------------

_HISTORY_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m", "%m/%d/%Y", "%m/%d/%y", "%m/%Y", "%m/%y", "%b %Y", "%B %Y")
_PERIOD_LABEL = {"weekly": "weekly", "bi-weekly": "bi-weekly", "semi-monthly": "semi-monthly",
                 "monthly": "monthly", "quarterly": "quarterly"}


def _parse_history_date(value) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in _HISTORY_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _infer_periodicity(dates: list[date]) -> str | None:
    """Pay period implied by the spacing of dated payments, or None."""
    ds = sorted(set(dates))
    if len(ds) < 2:
        return None
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    gap = median(gaps)
    if 5 <= gap <= 9:
        return "weekly"
    if 12 <= gap < 15:
        return "bi-weekly"
    if 15 <= gap <= 18:
        return "semi-monthly"
    if 25 <= gap <= 45:
        return "monthly"
    if 80 <= gap <= 120:
        return "quarterly"
    return None


def _period_key(d: date, periodicity: str) -> str:
    if periodicity == "monthly":
        return f"{d.year}-{d.month:02d}"
    if periodicity == "quarterly":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    return d.isoformat()


def calculate_history_based(
    rows: list,
) -> tuple[str | None, str | None, list[date]]:
    """Annual income from a payment history — the ledger of what was paid.

    Irregular income (child support, alimony, benefits with arrears or
    lapses) is annualised from what was actually received, never from a
    single row × 12. With a full year of rows the answer is the sum of the
    most recent year of payments; with less, the per-period average scaled
    to a year, stated as such.

    Returns (annual_income, details, dated_payments).
    """
    payments: list[tuple[date, float]] = []
    undated = 0
    for row in rows or []:
        amount = _money(getattr(row, "amount", None) if not isinstance(row, dict) else row.get("amount"))
        raw_date = getattr(row, "date", None) if not isinstance(row, dict) else row.get("date")
        if amount is None:
            continue
        d = _parse_history_date(raw_date)
        if d is None:
            undated += 1
            continue
        payments.append((d, amount))
    if len(payments) < 2:
        if payments or undated:
            return None, "[rejected] payment history has fewer than two dated payments — annual income not computed from it", []
        return None, None, []

    periodicity = _infer_periodicity([d for d, _ in payments])
    if periodicity is None:
        return None, "[rejected] payment history dates do not show a regular period — annual income not computed from it", [d for d, _ in payments]

    per_year = FREQUENCY_MULTIPLIERS[periodicity]
    buckets: dict[str, float] = {}
    first_date: dict[str, date] = {}
    for d, amount in payments:
        key = _period_key(d, periodicity)
        buckets[key] = buckets.get(key, 0.0) + amount
        first_date[key] = min(first_date.get(key, d), d)
    ordered = sorted(buckets.items(), key=lambda kv: first_date[kv[0]], reverse=True)
    label = _PERIOD_LABEL[periodicity]

    if len(ordered) >= per_year:
        window = ordered[:per_year]
        annual = sum(v for _, v in window)
        details = (
            f"sum of the {per_year} most recent {label} payments "
            f"({window[-1][0]} to {window[0][0]}) = {annual:.2f}"
        )
    else:
        avg = sum(v for _, v in ordered) / len(ordered)
        annual = avg * per_year
        details = (
            f"avg of {len(ordered)} {label} payments ({ordered[-1][0]} to {ordered[0][0]}) "
            f"= {avg:.2f} × {per_year} = {annual:.2f} — history covers {len(ordered)} of "
            f"{per_year} periods"
        )
    if undated:
        details += f"; {undated} undated row(s) ignored"
    return f"{annual:.2f}", details, [d for d, _ in payments]


def annualize_history(rows: list) -> float | None:
    """Annual figure implied by a payment history, or None."""
    annual, _details, _dates = calculate_history_based(rows)
    return _money(annual)


# ---------------------------------------------------------------------------
# Plausibility envelope
# ---------------------------------------------------------------------------

# The most a single source can plausibly pay a household in this housing
# stock. A result outside the envelope is a misread basis, not income: it
# is rejected with a finding and the next method gets its turn.
_PLAUSIBLE_ANNUAL_MAX = {
    "employment": 1_000_000.0,
    "fixed_monthly": 250_000.0,
    "annual_net": 1_000_000.0,
}


def _plausibility_problem(annual: float, calc_mode: str) -> str | None:
    cap = _PLAUSIBLE_ANNUAL_MAX.get(calc_mode, 1_000_000.0)
    if annual > cap:
        return f"{annual:,.2f}/yr exceeds the plausible ceiling of {cap:,.0f} for this income type"
    if annual < 0 and calc_mode != "annual_net":
        return f"{annual:,.2f}/yr is negative"
    return None


# ---------------------------------------------------------------------------
# Main orchestrator — compute all applicable methods for one income source
# ---------------------------------------------------------------------------

# Wage evidence whose newest pay date is older than this (relative to the
# certification effective date) is treated as historical, not current
# income: EIV / Work Number reports carry multi-year wage HISTORY tables,
# and a terminated job's old quarters must not be annualized into today's
# household income. 15 months tolerates EIV's normal reporting lag.
_STALE_WAGE_MONTHS = 15


def _stale_note(dates: list[date], reference_date: date | None) -> str | None:
    """Note describing why this evidence is historical, or None."""
    if not reference_date or not dates:
        return None
    latest = max(dates)
    months = (
        (reference_date.year - latest.year) * 12
        + reference_date.month - latest.month
    )
    if months <= _STALE_WAGE_MONTHS:
        return None
    return (
        f"latest pay date {latest.isoformat()} is {months} months before "
        f"effective date {reference_date.isoformat()} — wage history "
        f"appears historical (EIV/Work Number), not current income"
    )


def _stale_wage_note(
    paystubs: list[PayStubEntry], reference_date: date | None,
) -> str | None:
    return _stale_note([d for d in (_parse_date(ps.payDate) for ps in paystubs) if d], reference_date)


def calculate_all_methods(
    vi_entry: VerificationIncomeEntry | None,
    matching_paystubs: list[PayStubEntry],
    funding_program: str | None = None,
    reference_date: date | None = None,
) -> list[IncomeCalculationResult]:
    """Compute annual income for one source using SOURCE-OF-TRUTH routing.

    Candidate methods are tried in evidence order and the first one that
    yields a plausible figure is the primary:
      paystubs (≥3)               → paystub-based
      payment history (≥2 rows)   → history-based (child support ledgers,
                                    benefit payment records)
      VOI rate (+unit, hours)     → voi-based (wages, fixed benefits)
      Self-employment / self-cert → self-declared

    A method that cannot produce a figure from what the record states, or
    whose figure falls outside the plausibility envelope, is recorded as a
    "[rejected]" row with no annualIncome so the audit shows why, and the
    next method is tried. Audit methods ("[audit]" rows: YTD projections)
    run alongside for cross-validation and never override the primary.
    """
    results: list[IncomeCalculationResult] = []

    member_name = vi_entry.memberName if vi_entry else (
        matching_paystubs[0].memberName if matching_paystubs else None
    )
    source_name = vi_entry.sourceName if vi_entry else (
        matching_paystubs[0].sourceName if matching_paystubs else None
    )

    income_type = (vi_entry.incomeType or "").lower() if vi_entry else ""
    # Raw (uncased) type carried onto each calc record so the comparator
    # can key benefit income by program rather than payer.
    income_type_raw = vi_entry.incomeType if vi_entry else None
    calc_mode = _classify_income_mode(income_type)

    def _row(method: str, annual: str | None, details: str | None) -> IncomeCalculationResult:
        return IncomeCalculationResult(
            memberName=member_name,
            sourceName=source_name,
            incomeType=income_type_raw,
            method=method,
            annualIncome=annual,
            details=details,
        )

    history_rows = list(getattr(vi_entry, "paymentHistory", None) or []) if vi_entry else []

    # Candidate methods in evidence order; each returns (annual, details, dates).
    candidates: list[tuple[str, callable]] = []
    if len(matching_paystubs) >= 3:
        def _paystubs():
            annual, details = calculate_paystub_based(matching_paystubs)
            dates = [d for d in (_parse_date(ps.payDate) for ps in matching_paystubs) if d]
            return annual, details, dates
        candidates.append(("paystub-based", _paystubs))
    if len(history_rows) >= 2:
        candidates.append(("history-based", lambda: calculate_history_based(history_rows)))
    # A self-employment affidavit that states "$17/hour, 80 hours" is a wage
    # calculation whatever its income type says; only a rate with no unit
    # and no hours is taken as the annual net figure.
    rate_unit = normalize_rate_unit(getattr(vi_entry, "rateUnit", None)) if vi_entry else None
    wage_like_rate = bool(
        vi_entry and vi_entry.rateOfPay and (
            rate_unit in ("hourly", "daily", "weekly", "bi-weekly", "semi-monthly", "monthly", "quarterly", "per_period")
            or (rate_unit is None and vi_entry.hoursPerPayPeriod)
        )
    )
    if vi_entry and vi_entry.rateOfPay and (calc_mode != "annual_net" or wage_like_rate):
        def _voi():
            annual, details, _findings = calculate_voi_based(
                vi_entry.rateOfPay,
                vi_entry.frequencyOfPay,
                vi_entry.hoursPerPayPeriod,
                vi_entry.overtimeRate,
                vi_entry.overtimeFrequency,
                funding_program,
                rate_unit=getattr(vi_entry, "rateUnit", None),
                calc_mode=calc_mode,
            )
            return annual, details, []
        candidates.append(("voi-based", _voi))
    if vi_entry and (vi_entry.selfDeclaredAmount or (calc_mode == "annual_net" and vi_entry.rateOfPay and not wage_like_rate)):
        def _self_declared():
            # selfDeclaredAmount is annual by schema convention (TIC Part III
            # columns, Schedule C net, gift/child-support affidavits) — but
            # benefit letters state MONTHLY amounts and extraction records
            # that basis in frequencyOfPay. Honor it: a monthly $1,098 SSA
            # benefit is $13,176/year, not $1,098. Self-employment
            # (annual_net) stays as-is — Schedule C net is annual regardless
            # of any stray frequency value.
            amount = vi_entry.selfDeclaredAmount or vi_entry.rateOfPay
            freq = None if calc_mode == "annual_net" else vi_entry.frequencyOfPay
            annual_str = calculate_self_declared(amount, freq)
            if annual_str is None:
                return None, None, []
            annual = float(annual_str)
            mult = get_frequency_multiplier(freq) if freq else None
            if calc_mode == "annual_net" and annual < 0:
                details = f"Self-employment net loss {annual:.2f} counted as 0.00 (Section 9)"
                annual_str = "0.00"
            elif mult and mult > 1:
                details = (
                    f"Self-declared {normalize_frequency(freq)}: {_money(amount):.2f} × {mult} = {annual_str}"
                )
            else:
                details = f"Self-declared annual: {annual_str}"
            return annual_str, details, []
        candidates.append(("self-declared", _self_declared))

    primary_method: str | None = None
    for idx, (method, compute) in enumerate(candidates):
        annual, details, dates = compute()
        if annual is None:
            if details and details.startswith("[rejected]"):
                results.append(_row(method, None, details))
            continue
        problem = _plausibility_problem(float(annual), calc_mode)
        if problem:
            more = "; next method used" if idx + 1 < len(candidates) else ""
            results.append(_row(method, None, f"[rejected] {details} — {problem}{more}"))
            continue
        stale = _stale_note(dates, reference_date)
        if stale:
            details = f"[historical] {stale}; {details}"
        results.append(_row(method, annual, details))
        primary_method = method
        break

    # Audit methods — run for cross-validation but don't override primary.
    # These help findings layer flag discrepancies without affecting sums.
    audited = False
    if vi_entry and calc_mode == "employment":
        ytd_annual, ytd_details = calculate_ytd_based(
            vi_entry.ytdAmount, vi_entry.ytdStartDate, vi_entry.ytdEndDate,
        )
        if ytd_annual:
            results.append(_row("ytd-based", ytd_annual, f"[audit] {ytd_details}"))
            audited = True
    if not audited and matching_paystubs and primary_method != "history-based":
        ps_ytd_annual, ps_ytd_details = calculate_paystub_ytd(
            matching_paystubs, vi_entry.hireDate if vi_entry else None,
        )
        if ps_ytd_annual:
            results.append(_row("ytd-based", ps_ytd_annual, f"[audit] {ps_ytd_details}"))

    return results


# The audit YTD projection is evidence; the primary is the answer. When
# they diverge beyond this, income likely changed mid-year (raise, cut
# hours, job change) or a basis was misread — either way an analyst
# should look. Young YTDs annualize too noisily to judge, so windows
# under 90 days never fire.
_YTD_DIVERGENCE_REL = 0.25
_YTD_MIN_ELAPSED_DAYS = 90


def ytd_divergence_findings(
    calcs: list[IncomeCalculationResult],
) -> list[str]:
    """Compare each source's primary projection to its [audit] YTD row."""
    primaries: dict[tuple[str, str], IncomeCalculationResult] = {}
    audits: dict[tuple[str, str], IncomeCalculationResult] = {}
    for c in calcs:
        d = c.details or ""
        key = ((c.memberName or "").lower(), (c.sourceName or "").lower())
        if d.startswith("[audit]"):
            audits.setdefault(key, c)
        elif not d.startswith("[historical]"):
            primaries.setdefault(key, c)

    findings: list[str] = []
    for key, aud in audits.items():
        pri = primaries.get(key)
        if not pri or not pri.annualIncome or not aud.annualIncome:
            continue
        try:
            p, a = float(pri.annualIncome), float(aud.annualIncome)
        except ValueError:
            continue
        if p <= 0 or a <= 0:
            continue
        m = re.search(r"/\s*(\d+)\s*days", aud.details or "")
        if m and int(m.group(1)) < _YTD_MIN_ELAPSED_DAYS:
            continue
        rel = abs(p - a) / max(p, a)
        if rel > _YTD_DIVERGENCE_REL:
            findings.append(
                f"Income source '{pri.sourceName}' ({pri.memberName}): "
                f"projected annual ${p:,.2f} ({pri.method}) vs YTD-implied "
                f"${a:,.2f} — differs {rel:.0%}; income may have changed "
                f"mid-year — verify calculation basis (Section 9)"
            )
    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_income_mode(income_type: str) -> str:
    """Classify income type into a calculation mode.

    Returns:
        "fixed_monthly" — SSA, TANF, pension, child support: rate × frequency
                          (monthly when none is stated)
        "annual_net"    — self-employment, business: annual net from Schedule C
        "employment"    — wages: rate × hours × periods, or a salary
        "other"         — any other periodic amount: rate × frequency
    """
    t = (income_type or "").strip().lower()
    if t in _FIXED_TYPES or any(k in t for k in _FIXED_KEYWORDS):
        return "fixed_monthly"

    if t in ("self-employment", "self employment", "business", "business income") or "self-employ" in t:
        return "annual_net"

    if not t or "wage" in t or "employ" in t or "salary" in t or "military" in t or t == "other income":
        return "employment"

    # Anything else with a rate is a periodic amount, not a wage: no hourly
    # inference, no hours.
    return "other"


_FIXED_TYPES = frozenset((
    "social security", "supplemental security income", "social security disability",
    "pension", "temporary assistance", "child support", "alimony", "ssi", "ssdi",
    "tanf", "veterans benefits", "va benefits", "annuity", "retirement",
    "unemployment", "workers compensation", "disability", "public assistance",
    "general assistance", "adoption assistance", "foster care",
))
_FIXED_KEYWORDS = ("social security", "pension", "child support", "alimony", "tanf",
                   "veteran", "annuity", "retirement", "unemployment", "workers comp",
                   "public assistance", "general assistance")


def _parse_date(value: str | None) -> date | None:
    """Parse a YYYY-MM-DD date string."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def match_paystubs_to_sources(
    paystubs: list[PayStubEntry],
    vi_entries: list[VerificationIncomeEntry],
) -> dict[int, list[PayStubEntry]]:
    """Match paystubs to verification income entries by source/member name.

    A source-name match alone is NOT sufficient when both sides carry member
    names: two household members often work for the same employer, and pooling
    their stubs under one VOI averages two salaries into one wrong annual
    (observed: spouses at the same employer each ~$45k/$29k pooled into a
    single $37k figure). Member names must agree whenever both are present;
    a missing member name on either side falls back to source-only matching.

    Returns:
        dict mapping vi_entry index → list of matching paystubs
    """
    matched: dict[int, list[PayStubEntry]] = {}
    unmatched: list[PayStubEntry] = list(paystubs)

    for i, vi in enumerate(vi_entries):
        matched[i] = []
        vi_source = (vi.sourceName or "").lower().strip()
        vi_member = (vi.memberName or "").lower().strip()

        if not vi_source and not vi_member:
            continue

        still_unmatched = []
        for ps in unmatched:
            ps_source = (ps.sourceName or "").lower().strip()
            ps_member = (ps.memberName or "").lower().strip()

            # Match by source name (fuzzy: one contains the other)
            source_match = False
            if vi_source and ps_source:
                source_match = (
                    vi_source in ps_source
                    or ps_source in vi_source
                    or _token_overlap(vi_source, ps_source) >= 0.5
                )

            # Match by member name
            member_match = False
            if vi_member and ps_member:
                member_match = vi_member == ps_member or _token_overlap(vi_member, ps_member) >= 0.5

            # When both sides name a member, their GIVEN names must agree —
            # an employer match with a conflicting member is a different
            # person's stub. Surname overlap is not agreement: household
            # members share surnames, so whole-name token overlap calls
            # siblings at the same employer a "match".
            members_compatible = (
                not vi_member
                or not ps_member
                or not _given_names_conflict(vi_member, ps_member)
            )

            if (source_match and members_compatible) or (member_match and not vi_source):
                matched[i].append(ps)
            else:
                still_unmatched.append(ps)

        unmatched = still_unmatched

    return matched


def _given_names_conflict(a: str, b: str) -> bool:
    """True when two member names clearly denote different people.

    Compares the first (given-name) token only: "andrea carranza" vs
    "felipe pineda carranza" conflict; "andrea carranza" vs "andrea
    carranza jimenez" do not. Initials and truncations are treated as
    compatible ("j smith" vs "john smith", "dan" vs "daniel")."""
    ta = a.split()
    tb = b.split()
    if not ta or not tb:
        return False
    ga, gb = ta[0], tb[0]
    if ga == gb:
        return False
    if len(ga) == 1 or len(gb) == 1:
        return not (ga.startswith(gb) or gb.startswith(ga))
    if len(ga) >= 3 and len(gb) >= 3 and (ga.startswith(gb) or gb.startswith(ga)):
        return False
    return True


def _token_overlap(a: str, b: str) -> float:
    """Compute token overlap ratio between two strings."""
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = len(tokens_a & tokens_b)
    return overlap / min(len(tokens_a), len(tokens_b))
