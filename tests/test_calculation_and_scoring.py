"""Calculator, normaliser, identity, completeness, scoring and payload rules,
each pinned with a case that must fire and one that must not."""
from datetime import date
from types import SimpleNamespace

from app.schemas.extraction import (
    AssetEntry, DocumentGroup, Finding, HouseholdDemographics,
    HouseholdMember, PayStubEntry, PaymentHistoryRow, VerificationIncomeEntry,
)
from app.schemas.scoring import FieldScore, StageScore
from app.services import findings as F
from app.services.cartograph.adapter import _note_key, build_findings, attach_confidence
from app.services.completeness import _NOT_HOUSEHOLD_RE
from app.services.field_scorer import RecordScorer, score_findings, build_score_summary
from app.services.identity import collect_identity_claims, _member_key
from app.services.income_calculator import (
    calculate_all_methods, calculate_history_based, calculate_voi_based,
    get_frequency_multiplier, normalize_frequency, normalize_rate_unit,
)
from app.services.validation import normalize_date, normalize_money, normalize_ssn, to_title_case


# ---------------------------------------------------------------------------
# Income calculator
# ---------------------------------------------------------------------------

def test_every_frequency_spelling_maps_to_one_vocabulary():
    assert normalize_frequency("Biweekly") == "bi-weekly"
    assert normalize_frequency("every other Friday") == "bi-weekly"
    assert normalize_frequency("Twice a month") == "semi-monthly"
    assert normalize_frequency("per annum") == "annually"
    assert normalize_frequency("Pay Frequency: Hourly") == "hourly"
    assert get_frequency_multiplier("hourly") is None
    assert normalize_frequency("no idea") is None


def test_hours_multiply_only_an_hourly_rate():
    assert calculate_voi_based("18.00", "bi-weekly", "80", rate_unit="hourly")[0] == "37440.00"
    assert calculate_voi_based("48360.00", "bi-weekly", "80", rate_unit="annually")[0] == "48360.00"
    rejected = calculate_voi_based("48360.00", "bi-weekly", "80")
    assert rejected[0] is None and rejected[1].startswith("[rejected]")


def test_hourly_looking_rate_without_hours_is_rejected_for_wages_only():
    assert calculate_voi_based("18.00", "bi-weekly", None)[0] is None
    assert calculate_voi_based("150.00", "monthly", None, calc_mode="other")[0] == "1800.00"
    assert calculate_voi_based("1489.50", None, None, calc_mode="fixed_monthly")[0] == "17874.00"


def test_payment_history_annualises_from_what_was_paid():
    rows = [PaymentHistoryRow(date=f"2026-{m:02d}-01", amount="100.00") for m in range(1, 13)] + [PaymentHistoryRow(date="2025-12-01", amount="900.00")]
    annual, details, _ = calculate_history_based(rows)
    assert annual == "1200.00" and "12 months ending 2026-12-01" in details
    short = [PaymentHistoryRow(date="2026-08-01", amount="81.35"), PaymentHistoryRow(date="2026-07-01", amount="244.05")]
    assert calculate_history_based(short)[0] == f"{(81.35 + 244.05) / 2 * 12:.2f}"
    assert calculate_history_based([PaymentHistoryRow(date="2026-08-01", amount="81.35")])[0] is None


def test_a_year_of_weekly_payments_is_summed_over_the_trailing_twelve_months():
    # 52 ledger rows: 51 weekly payments and one made a year to the day
    # before the last. A manager's twelve-month calculation counts 51.
    from datetime import timedelta
    last = date(2026, 8, 5)
    rows = [PaymentHistoryRow(date=(last - timedelta(days=7 * k)).isoformat(), amount="3.59") for k in range(51)]
    rows.append(PaymentHistoryRow(date=(last - timedelta(days=365)).isoformat(), amount="3.59"))
    annual, details, _ = calculate_history_based(rows)
    assert annual == f"{51 * 3.59:.2f}" and "51 weekly payments" in details
    # Fewer than a year of a fixed payment is that payment times the year's
    # periods, whatever rows the transcription lost or doubled.
    partial = rows[:30] + [PaymentHistoryRow(date=rows[3].date, amount="3.59")]
    annual, details, _ = calculate_history_based(partial)
    assert annual == f"{3.59 * 52:.2f}" and "fixed weekly payment" in details
    # Varying payments are averaged and scaled.
    varying = [PaymentHistoryRow(date="2026-08-01", amount="81.35"), PaymentHistoryRow(date="2026-07-01", amount="244.05"),
               PaymentHistoryRow(date="2026-06-01", amount="100.00"), PaymentHistoryRow(date="2026-05-01", amount="120.00")]
    assert calculate_history_based(varying)[0] == f"{(81.35 + 244.05 + 100 + 120) / 4 * 12:.2f}"


def test_one_or_two_stubs_yield_a_noted_figure_after_an_employer_rate():
    stubs = [
        PayStubEntry(sourceName="Staffmark", memberName="A P", grossPay="432.00", payDate="2026-06-21", payInterval="weekly", ytdGross="6250.50"),
        PayStubEntry(sourceName="Staffmark", memberName="A P", grossPay="688.50", payDate="2026-06-28", payInterval="weekly", ytdGross="6939.00"),
    ]
    only_stubs = VerificationIncomeEntry(memberName="A P", sourceName="Staffmark", incomeType="Non-Federal Wage")
    rows = calculate_all_methods(only_stubs, stubs)
    primary = next(r for r in rows if r.annualIncome and not (r.details or "").startswith("[audit]"))
    assert primary.method == "paystub-based" and primary.annualIncome == f"{(432.00 + 688.50) / 2 * 52:.2f}"
    assert "only 2 pay stub(s)" in primary.details
    # An employer's stated rate outranks two stubs; three stubs outrank it.
    with_voi = VerificationIncomeEntry(memberName="A P", sourceName="Staffmark", incomeType="Non-Federal Wage",
                                       rateOfPay="18.00", rateUnit="hourly", frequencyOfPay="weekly", hoursPerPayPeriod="30")
    rows = calculate_all_methods(with_voi, stubs)
    assert next(r for r in rows if r.annualIncome).method == "voi-based"
    three = stubs + [PayStubEntry(sourceName="Staffmark", memberName="A P", grossPay="540.00", payDate="2026-07-05", payInterval="weekly", ytdGross="7479.00")]
    rows = calculate_all_methods(with_voi, three)
    assert next(r for r in rows if r.annualIncome).method == "paystub-based"
    assert "only" not in next(r for r in rows if r.annualIncome).details


def test_hourly_self_employment_takes_the_wage_path_and_a_bare_rate_is_annual_net():
    hourly = VerificationIncomeEntry(memberName="C", incomeType="Self-Employment", rateOfPay="17.00", rateUnit="hourly", frequencyOfPay="bi-weekly", hoursPerPayPeriod="80")
    assert [(c.method, c.annualIncome) for c in calculate_all_methods(hourly, [])] == [("voi-based", "35360.00")]
    bare = VerificationIncomeEntry(memberName="C", incomeType="Self-Employment", rateOfPay="24000.00")
    assert [(c.method, c.annualIncome) for c in calculate_all_methods(bare, [])] == [("self-declared", "24000.00")]


def test_implausible_result_is_rejected_and_the_next_method_used():
    vi = VerificationIncomeEntry(memberName="X", sourceName="E", incomeType="Non-Federal Wage", rateOfPay="48360.00",
                                 frequencyOfPay="bi-weekly", hoursPerPayPeriod="80", selfDeclaredAmount="31469.10")
    rows = calculate_all_methods(vi, [])
    # the unit-less salary with hours is rejected, and the self-declared
    # figure multiplied by the record's bi-weekly frequency is rejected too
    assert all(r.annualIncome is None and r.details.startswith("[rejected]") for r in rows)
    vi_annual = VerificationIncomeEntry(memberName="X", sourceName="E", incomeType="Non-Federal Wage", rateOfPay="48360.00",
                                        hoursPerPayPeriod="80", selfDeclaredAmount="31469.10")
    rows = calculate_all_methods(vi_annual, [])
    assert rows[0].annualIncome is None and any(r.annualIncome == "31469.10" for r in rows)


def test_paystub_ytd_becomes_an_audit_row():
    stubs = [PayStubEntry(sourceName="E", memberName="X", grossPay="1440.00", payDate="2026-03-06", payInterval="bi-weekly", ytdGross="6900.00"),
             PayStubEntry(sourceName="E", memberName="X", grossPay="1440.00", payDate="2026-02-20", payInterval="bi-weekly", ytdGross="5460.00"),
             PayStubEntry(sourceName="E", memberName="X", grossPay="1500.00", payDate="2026-02-06", payInterval="bi-weekly", ytdGross="4020.00")]
    rows = calculate_all_methods(None, stubs, reference_date=date(2026, 4, 1))
    assert rows[0].method == "paystub-based" and rows[1].method == "ytd-based" and rows[1].details.startswith("[audit]")


def test_rate_units():
    assert normalize_rate_unit("/hr") == "hourly" and normalize_rate_unit("per year") == "annually"
    assert normalize_rate_unit("per pay period") == "per_period" and normalize_rate_unit("bogus") is None


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

def test_money_returns_none_for_junk_and_reads_what_documents_print():
    assert normalize_money("$ 1, 489.50") == "1489.50" and normalize_money("(50.00)") == "-50.00"
    assert normalize_money("1489.50/mo") == "1489.50" and normalize_money(1234.5) == "1234.50"
    for junk in ("N/A", "1,200 - 1,500", "50%", "2026-08-20", "abc", ""):
        assert normalize_money(junk) is None, junk


def test_ssn_accepts_only_ssn_shapes():
    assert normalize_ssn("441- 66- 8882") == "441-66-8882" and normalize_ssn("XXX-XX-8882") == "***-**-8882"
    assert normalize_ssn("last 4: 8882") == "***-**-8882"
    for junk in ("02/20/1959", "405-522-2273", "000-12-3456", "N/A"):
        assert normalize_ssn(junk) is None, junk


def test_dates_pivot_and_tolerate_one_ocr_digit_but_not_impossible_days():
    assert normalize_date("7/15/49") == "1949-07-15" and normalize_date("8/20/26") == "2026-08-20"
    assert normalize_date("071/15/1949") == "1949-07-15" and normalize_date("July 15, 1949") == "1949-07-15"
    assert normalize_date("2020-02-30") is None and normalize_date("08/26") is None


def test_title_case_keeps_what_was_deliberate():
    assert to_title_case("LYONS, ARNOLD") == "Lyons, Arnold" and to_title_case("ANNA LEE") == "Anna Lee"
    assert to_title_case("TBK BANK") == "TBK Bank" and to_title_case("ABC Trucking LLC") == "ABC Trucking LLC"
    assert to_title_case("mcdonald's") == "McDonald's" and to_title_case("O'BRIEN") == "O'Brien"


# ---------------------------------------------------------------------------
# Identity: the certification's own table is a claim
# ---------------------------------------------------------------------------

def test_household_table_with_th_headers_yields_certification_claims():
    members = [HouseholdMember(FirstName="Arnold", LastName="Lyons")]
    group = DocumentGroup(document_type="Tenant Income Certification (TIC)", pages=[2], page_range="2", category="include", combined_text="")
    text = ("<table><tr><th>Last Name</th><th>First Name</th><th>Date of Birth</th><th>Last 4 Digits of Social Security No.</th></tr>"
            "<tr><td>Lyons</td><td>Arnold R.</td><td>02-20-1959</td><td>8882 Y</td></tr></table>")
    claims = collect_identity_claims(members, [group], {2: text})
    ssn = claims[_member_key(members[0])]["ssn"]
    assert ssn and ssn[0]["value"].endswith("8882") and ssn[0]["authority"] == 0


# ---------------------------------------------------------------------------
# Completeness label classes
# ---------------------------------------------------------------------------

def test_non_household_labels_are_excluded_from_the_amount_pool():
    for label in ("HOTMA Annual Inflation Factor: $52,787.00", "Rent Assistance: $978.00", "Income Limit $58,600", "Passbook rate"):
        assert _NOT_HOUSEHOLD_RE.search(label), label
    for label in ("Public Assistance $500", "Social Security 1,810.00", "Checking 556"):
        assert not _NOT_HOUSEHOLD_RE.search(label), label


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _income_card(member="Sharon Keaton", source="Pinellas County Schools"):
    sc = RecordScorer("income", f"{member} — {source}")
    for f, v in (("memberName", member), ("sourceName", source), ("rateOfPay", "18.00"), ("frequencyOfPay", "bi-weekly")):
        sc.score_field(f, v)
    card = sc.build()
    for fs in card.fields:
        fs.stages.append(StageScore(stage="source_verification", score=1.0, reason="Verified in source document"))
        fs.recompute()
    card.recompute()
    return card


def test_informational_finding_does_not_dispute():
    card = _income_card()
    note = F.make_finding("CERT_AMOUNT_UNACCOUNTED", "subtotal", category="income", result="na",
                          subject_ref={"member_name": "Sharon Keaton", "source_name": "Pinellas County Schools"})
    score_findings([card], [note])
    assert not card.disputed and all(s.stage != "finding" for f in card.fields for s in f.stages)


def test_dispute_strength_is_per_code():
    weak, strong = _income_card(), _income_card()
    ref = {"member_name": "Sharon Keaton", "source_name": "Pinellas County Schools"}
    score_findings([weak], [F.make_finding("INCOME_METHODS_DISAGREE", "methods", category="income", subject_ref=ref)])
    score_findings([strong], [F.make_finding("CERT_SUMMARY_INCOME_MISMATCH", "cert", category="income", subject_ref=ref)])
    rate_weak = next(f for f in weak.fields if f.field_name == "rateOfPay")
    rate_strong = next(f for f in strong.fields if f.field_name == "rateOfPay")
    assert weak.disputed and strong.disputed
    assert next(s.score for s in rate_weak.stages if s.stage == "finding") == F.DISPUTE_WEAK
    assert next(s.score for s in rate_strong.stages if s.stage == "finding") == F.DISPUTE_STRONG
    assert rate_weak.composite > rate_strong.composite


def test_unverified_cap_names_itself_and_ceilings_report_their_reason():
    fs = FieldScore(field_name="rateOfPay", value="18.00", stages=[StageScore(stage="extraction", score=0.85, reason="Extracted"),
                                                                     StageScore(stage="business_rule", score=1.0, reason="Valid range")])
    fs.recompute()
    assert fs.flag.value == "yellow" and fs.flag_message == "Not verified against a source document"
    fs2 = FieldScore(field_name="rateOfPay", value="18.00", stages=[StageScore(stage="extraction", score=0.85, reason="Extracted"),
                                                                      StageScore(stage="source_verification", score=0.85, ceiling=0.79, reason="Found only on the certification form")])
    fs2.recompute()
    assert fs2.flag.value == "yellow" and "certification form" in fs2.flag_message


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def test_note_keys_ignore_figures_but_not_names():
    a = _note_key("Income total mismatch: TIC declares $39,819.16 but sources sum to $79,989.56")
    b = _note_key("Income total mismatch: TIC declares $39,819.16 but sources sum to $41,000.00")
    c = _note_key("Household member 'Arnold Lyons' has placeholder SSN")
    d = _note_key("Household member 'Kyla Hamilton' has placeholder SSN")
    assert a == b and c != d and a.startswith("NOTE:")


def test_payload_findings_exclude_field_notes_and_carry_keys():
    ex = SimpleNamespace(findings=["[YELLOW] Randy Buck — Savings → incomeAmount: Review recommended", "AR certification but no previous certification found"],
                         finding_records=[F.make_finding("SIGNATURE_DATE_MISSING", "Signed but undated")])
    rows = build_findings(ex, [])
    assert [r["code"] for r in rows] == ["SIGNATURE_DATE_MISSING", "NOTE"]
    assert rows[0]["finding_key"] == "SIGNATURE_DATE_MISSING:case" and rows[1]["description"].startswith("AR certification")


def test_confidence_attaches_by_position_then_label():
    household = HouseholdDemographics(houseHold=[HouseholdMember(FirstName="Kobe", LastName="Kuykendall")])
    from app.services.field_scorer import score_pydantic_records
    cards = score_pydantic_records(household=household)
    ex = SimpleNamespace(field_scores=build_score_summary(cards))
    payload = {"household_members": [{"ref": "m01", "first_name": "Kobe", "last_name": "Kuykendall"}], "income_records": [], "asset_records": [], "cert_review": {}}
    attach_confidence(payload, ex)
    assert "confidence" in payload["household_members"][0] and payload["confidence"]["fields"]["na"] >= 0
