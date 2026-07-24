"""Income calculation engine — computes annual income using four methods (Section 9)."""

import logging
from datetime import date, datetime

from app.schemas.extraction import (
    IncomeCalculationResult,
    PayStubEntry,
    VerificationIncomeEntry,
)
from app.services.hours_resolver import resolve_hours_range

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frequency multipliers
# ---------------------------------------------------------------------------

FREQUENCY_MULTIPLIERS: dict[str, int] = {
    "weekly": 52,
    "bi-weekly": 26,
    "semi-monthly": 24,
    "monthly": 12,
    "quarterly": 4,
    "annually": 1,
}


def get_frequency_multiplier(frequency: str | None) -> int | None:
    """Return the annual multiplier for a pay frequency."""
    if not frequency:
        return None
    return FREQUENCY_MULTIPLIERS.get(frequency.strip().lower())


# ---------------------------------------------------------------------------
# Individual calculation methods
# ---------------------------------------------------------------------------

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
    if not amount:
        return None
    try:
        val = float(amount)
    except ValueError:
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
) -> tuple[str | None, str | None, list[str]]:
    """VOI-based annual income: rate × hours × frequency_multiplier + overtime.

    Returns:
        (annual_income, details_string, list_of_findings)
    """
    findings: list[str] = []

    if not rate_of_pay or not frequency_of_pay:
        return None, None, findings

    try:
        rate = float(rate_of_pay)
    except ValueError:
        return None, None, findings

    multiplier = get_frequency_multiplier(frequency_of_pay)
    if multiplier is None:
        return None, f"Unknown frequency: {frequency_of_pay}", findings

    # Resolve hours (may be a range)
    hours = None
    if hours_per_pay_period:
        hours, hours_finding = resolve_hours_range(hours_per_pay_period, funding_program)
        if hours_finding:
            findings.append(hours_finding)

    # Calculate base income.
    # hours_per_pay_period is hours in ONE pay period (Work Number's
    # "Avg Hours Worked/Pay Period" field — e.g. 80 for a bi-weekly worker
    # averaging 40 hrs/week). Annualize via the period multiplier, not a
    # fixed ×52 which would only be correct for weekly frequencies.
    if hours is not None:
        annual = rate * hours * multiplier
        details = f"{rate} × {hours} hrs/pp × {multiplier} pp/yr = {annual:.2f}"
    else:
        # Periodic rate: rate × multiplier
        annual = rate * multiplier
        details = f"{rate} × {multiplier} periods = {annual:.2f}"

    # Add overtime
    overtime_annual = 0.0
    if overtime_rate:
        try:
            ot_rate = float(overtime_rate)
            ot_multiplier = get_frequency_multiplier(overtime_frequency) or multiplier
            overtime_annual = ot_rate * ot_multiplier
            details += f" + OT {ot_rate} × {ot_multiplier} = {overtime_annual:.2f}"
        except ValueError:
            pass

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
    if not ytd_amount:
        return None, None

    try:
        ytd = float(ytd_amount)
    except ValueError:
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
        if ps.grossPay:
            try:
                amounts.append(float(ps.grossPay))
            except ValueError:
                continue
        if not freq and ps.payInterval:
            freq = ps.payInterval

    if not amounts:
        return None, None

    avg = sum(amounts) / len(amounts)
    multiplier = get_frequency_multiplier(freq)
    if multiplier is None:
        return None, f"Unknown pay interval: {freq}"

    annual = avg * multiplier
    details = f"avg({len(amounts)} stubs) = {avg:.2f} × {multiplier} = {annual:.2f}"
    return f"{annual:.2f}", details


# ---------------------------------------------------------------------------
# Main orchestrator — compute all applicable methods for one income source
# ---------------------------------------------------------------------------

# Wage evidence whose newest pay date is older than this (relative to the
# certification effective date) is treated as historical, not current
# income: EIV / Work Number reports carry multi-year wage HISTORY tables,
# and a terminated job's old quarters must not be annualized into today's
# household income. 15 months tolerates EIV's normal reporting lag.
_STALE_WAGE_MONTHS = 15


def _stale_wage_note(
    paystubs: list[PayStubEntry], reference_date: date | None,
) -> str | None:
    """Note describing why this wage evidence is historical, or None."""
    if not reference_date:
        return None
    dates = [d for d in (_parse_date(ps.payDate) for ps in paystubs) if d]
    if not dates:
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


def calculate_all_methods(
    vi_entry: VerificationIncomeEntry | None,
    matching_paystubs: list[PayStubEntry],
    funding_program: str | None = None,
    reference_date: date | None = None,
) -> list[IncomeCalculationResult]:
    """Compute annual income for one source using SOURCE-OF-TRUTH routing.

    Selects ONE primary method per source based on the documents present:
      paystubs (≥3)         → paystub-based
      VOI w/ rate + hours   → voi-based  (employment wages)
      Fixed-income type     → voi-based  (rate × 12 — SSA, pension, etc.)
      Self-employment       → self-declared (annual net from affidavit)
      Self-cert / TIC only  → self-declared (already annual)

    Other methods may run as audit_only entries — included in results so
    findings can flag discrepancies, but the primary is the authoritative
    annualIncome for downstream sums and rent calculations.
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

    # Determine the authoritative method for this source.
    has_paystubs = len(matching_paystubs) >= 3
    has_voi_wage = bool(
        vi_entry
        and calc_mode == "employment"
        and vi_entry.rateOfPay
        and vi_entry.frequencyOfPay
        and vi_entry.hoursPerPayPeriod
    )
    has_fixed_rate = bool(
        vi_entry
        and calc_mode == "fixed_monthly"
        and vi_entry.rateOfPay
    )
    has_self_employment = bool(
        vi_entry
        and calc_mode == "annual_net"
        and (vi_entry.selfDeclaredAmount or vi_entry.rateOfPay)
    )
    has_self_declared = bool(
        vi_entry and vi_entry.selfDeclaredAmount
    )

    if has_paystubs:
        primary_method = "paystub-based"
    elif has_voi_wage:
        primary_method = "voi-based"
    elif has_fixed_rate:
        primary_method = "voi-based"
    elif has_self_employment:
        primary_method = "self-declared"
    elif has_self_declared:
        primary_method = "self-declared"
    elif vi_entry and vi_entry.rateOfPay and vi_entry.frequencyOfPay:
        primary_method = "voi-based"
    else:
        primary_method = None

    # Compute the primary first.
    if primary_method == "paystub-based":
        ps_annual, ps_details = calculate_paystub_based(matching_paystubs)
        if ps_annual:
            stale = _stale_wage_note(matching_paystubs, reference_date)
            if stale:
                ps_details = f"[historical] {stale}; {ps_details}"
            results.append(IncomeCalculationResult(
                memberName=member_name,
                sourceName=source_name,
                incomeType=income_type_raw,
                method="paystub-based",
                annualIncome=ps_annual,
                details=ps_details,
            ))

    elif primary_method == "voi-based" and vi_entry:
        if calc_mode == "fixed_monthly" and vi_entry.rateOfPay:
            try:
                monthly = float(vi_entry.rateOfPay)
                annual = monthly * 12
                results.append(IncomeCalculationResult(
                    memberName=member_name,
                    sourceName=source_name,
                    incomeType=income_type_raw,
                    method="voi-based",
                    annualIncome=f"{annual:.2f}",
                    details=f"Fixed monthly: {monthly:.2f} × 12 = {annual:.2f}",
                ))
            except ValueError:
                pass
        elif calc_mode == "employment":
            voi_annual, voi_details, _ = calculate_voi_based(
                vi_entry.rateOfPay,
                vi_entry.frequencyOfPay,
                vi_entry.hoursPerPayPeriod,
                vi_entry.overtimeRate,
                vi_entry.overtimeFrequency,
                funding_program,
            )
            if voi_annual:
                results.append(IncomeCalculationResult(
                    memberName=member_name,
                    sourceName=source_name,
                    incomeType=income_type_raw,
                    method="voi-based",
                    annualIncome=voi_annual,
                    details=voi_details,
                ))
        else:
            # Unknown type with rate+freq
            try:
                rate = float(vi_entry.rateOfPay)
                mult = get_frequency_multiplier(vi_entry.frequencyOfPay)
                if mult:
                    annual = rate * mult
                    results.append(IncomeCalculationResult(
                        memberName=member_name,
                        sourceName=source_name,
                        incomeType=income_type_raw,
                        method="voi-based",
                        annualIncome=f"{annual:.2f}",
                        details=f"{rate:.2f} × {mult} periods = {annual:.2f}",
                    ))
            except ValueError:
                pass

    elif primary_method == "self-declared" and vi_entry and vi_entry.selfDeclaredAmount:
        # selfDeclaredAmount is annual by schema convention (TIC Part III
        # columns, Schedule C net, gift/child-support affidavits) — but
        # benefit letters state MONTHLY amounts and extraction records that
        # basis in frequencyOfPay. Honor it: a monthly $1,098 SSA benefit
        # is $13,176/year, not $1,098. Self-employment (annual_net) stays
        # as-is — Schedule C net is annual regardless of any stray
        # frequency value.
        freq = None if calc_mode == "annual_net" else vi_entry.frequencyOfPay
        annual_str = calculate_self_declared(vi_entry.selfDeclaredAmount, freq)
        if annual_str:
            mult = get_frequency_multiplier(freq) if freq else None
            if mult and mult > 1:
                details = (
                    f"Self-declared {freq.strip().lower()}: "
                    f"{float(vi_entry.selfDeclaredAmount):.2f} × {mult} "
                    f"= {annual_str}"
                )
            else:
                details = f"Self-declared annual: {annual_str}"
            results.append(IncomeCalculationResult(
                memberName=member_name,
                sourceName=source_name,
                incomeType=income_type_raw,
                method="self-declared",
                annualIncome=annual_str,
                details=details,
            ))

    # Audit methods — run for cross-validation but don't override primary.
    # These help findings layer flag discrepancies without affecting sums.
    if vi_entry:
        # YTD-based audit (employment only)
        if calc_mode == "employment" and primary_method != "ytd-based":
            ytd_annual, ytd_details = calculate_ytd_based(
                vi_entry.ytdAmount,
                vi_entry.ytdStartDate,
                vi_entry.ytdEndDate,
            )
            if ytd_annual:
                results.append(IncomeCalculationResult(
                    memberName=member_name,
                    sourceName=source_name,
                    incomeType=income_type_raw,
                    method="ytd-based",
                    annualIncome=ytd_annual,
                    details=f"[audit] {ytd_details}",
                ))

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_income_mode(income_type: str) -> str:
    """Classify income type into a calculation mode.

    Returns:
        "fixed_monthly" — SSA, TANF, pension, child support: rate × 12
        "annual_net"    — self-employment, business: annual net from Schedule C
        "employment"    — wages: rate × hours × 52 or rate × frequency
    """
    if income_type in (
        "social security", "supplemental security income",
        "social security disability", "pension", "temporary assistance",
        "child support", "alimony", "ssi", "ssdi",
    ):
        return "fixed_monthly"

    if income_type in (
        "self-employment", "business", "business income",
    ):
        return "annual_net"

    # Default: employment (hourly/periodic wage)
    return "employment"


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

            if source_match or (member_match and not vi_source):
                matched[i].append(ps)
            else:
                still_unmatched.append(ps)

        unmatched = still_unmatched

    return matched


def _token_overlap(a: str, b: str) -> float:
    """Compute token overlap ratio between two strings."""
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = len(tokens_a & tokens_b)
    return overlap / min(len(tokens_a), len(tokens_b))
