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


def test_a_first_paycheck_sets_the_year_to_date_basis():
    from app.services.income_calculator import calculate_paystub_ytd
    stubs = [PayStubEntry(sourceName="Bickford", memberName="J S", grossPay="1010.23", payDate="2026-06-05", payInterval="bi-weekly", ytdGross="1010.23"),
             PayStubEntry(sourceName="Bickford", memberName="J S", grossPay="756.38", payDate="2026-06-18", payInterval="bi-weekly", ytdGross="1766.61")]
    annual, details = calculate_paystub_ytd(stubs)
    assert "2026-05-22 to 2026-06-18" in details and "first paycheck" in details
    assert annual == f"{1766.61 / 27 * 365:.2f}"
    # A stated hire date wins; a stub mid-year with YTD above its gross measures from January.
    assert "2026-04-13 to" in calculate_paystub_ytd(stubs, "2026-04-13")[1]
    mid = [PayStubEntry(sourceName="A", memberName="B", grossPay="432.00", payDate="2026-06-21", payInterval="weekly", ytdGross="6250.50")]
    assert "2026-01-01 to 2026-06-21" in calculate_paystub_ytd(mid)[1]


def test_ocr_gate_catches_a_repeated_phrase_page_and_unread_choice_marks():
    from app.services.pdf_service import _is_degenerate, _selection_marks_unread
    looped = "DEVONTE CORNIELIES 2421 EASTON " + "Payrolls by Paychex, Inc. rights by Paychex, Inc. " * 40
    assert _is_degenerate(looped)[0]
    form = "Do you have a checking account? " + "○ Yes ○ No " * 12 + "1. 1. 1. 1. 1. 1. 1. 1. 1. 1." * 6
    # A form's own repetition (numbered marks, "$ - $ -") carries no word and
    # is not a phrase loop; the compression rule still judges the page.
    from app.services.pdf_service import _repeated_phrase_fraction
    assert _repeated_phrase_fraction("Name: A B " + "1. 1. 1. " * 40 + "Signature")[0] < 0.1
    assert _selection_marks_unread(form)
    assert not _selection_marks_unread("Job 1 o Yes ☑No Job 2 o Yes ☑No Child Support ☑Yes o No " + "○ Yes ○ No " * 4)
    assert not _selection_marks_unread("○ Yes ○ No ○ Yes ○ No")


def test_a_signature_date_a_year_before_the_effective_date_is_doubted_not_asserted():
    from app.schemas.extraction import CertificationInfo
    from app.services.signature_validator import _check_signed_after_effective
    out = _check_signed_after_effective(CertificationInfo(effectiveDate="2026-05-29", signatureDate="2024-08-28"))
    assert len(out) == 1 and out[0].code == "CERT_SIGNATURE_DATE_IMPLAUSIBLE" and out[0].result == "na"
    assert _check_signed_after_effective(CertificationInfo(effectiveDate="2026-05-29", signatureDate="2026-05-20")) == []


# ---------------------------------------------------------------------------
# Payload v1.3
# ---------------------------------------------------------------------------

def test_relationships_map_onto_cartographs_words_by_form_word_and_age():
    from app.services.cartograph.adapter import relationship_out
    assert relationship_out("Dependent", "2012-06-18", "2027-01-01") == "Minor Child"
    assert relationship_out("Son", "2001-03-01", "2027-01-01") == "Other Adult"
    assert relationship_out("Daughter", None, "2027-01-01") == "Minor Child"
    assert relationship_out("Co-Head", "1990-01-01", None) == "Co-Head"
    assert relationship_out("Live-in Aide", None, None) == "Live-in Aide"
    assert relationship_out("Mother", "1950-05-05", "2027-01-01") == "Other Adult"
    assert relationship_out("Unborn Child", None, None) == "Unborn"
    assert relationship_out("Zzyzx", None, None) is None


def test_payload_carries_annual_income_calculation_rate_unit_and_history():
    from app.schemas.extraction import CertificationInfo, IncomeCalculationResult, IncomeExtraction, SourceIncome
    from app.services.cartograph.adapter import build_household_members, build_income_records
    from app.core.config import Settings
    hh = HouseholdDemographics(houseHold=[
        HouseholdMember(FirstName="Rebecca", LastName="Knott", DOB="1990-12-19", head="H", relationship="Head"),
        HouseholdMember(FirstName="Kyzer", LastName="Sammons", DOB="2015-07-03", relationship="Dependent"),
        HouseholdMember(FirstName="Unborn", LastName="Child", relationship="Unborn Child"),
    ])
    ssi = VerificationIncomeEntry(memberName="Kyzer Sammons", sourceName="Social Security Administration",
                                  incomeType="Supplemental Security Income", type_of_VOI="SSA Benefit Letter",
                                  rateOfPay="994.00", rateUnit="monthly", sourcePages=[27], verificationStatus="verified")
    cs = VerificationIncomeEntry(memberName="Rebecca Knott", sourceName="Keith Amos", incomeType="Child Support",
                                 sourcePages=[28], verificationStatus="verified",
                                 paymentHistory=[PaymentHistoryRow(date="2026-08-05", amount="3.59"), PaymentHistoryRow(date=None, amount="3.59")])
    ex = SimpleNamespace(
        household_demographics=hh,
        certification_info=CertificationInfo(effectiveDate="2026-05-29"),
        income=IncomeExtraction(sourceIncome=SourceIncome(payStub=[], verificationIncome=[ssi, cs])),
        income_calculations=[
            IncomeCalculationResult(memberName="Kyzer Sammons", sourceName="Social Security Administration", method="voi-based", annualIncome="11928.00", details="994.00 monthly × 12 = 11928.00"),
            IncomeCalculationResult(memberName="Rebecca Knott", sourceName="Keith Amos", method="history-based", annualIncome="186.68", details="fixed weekly payment of 3.59 × 52 = 186.68"),
            IncomeCalculationResult(memberName="Rebecca Knott", sourceName="Keith Amos", method="ytd-based", annualIncome="180.00", details="[audit] projection"),
        ],
    )
    warnings: list[str] = []
    members = build_household_members(ex, warnings)
    assert [m["relationship"] for m in members] == ["Head of Household", "Minor Child", "Unborn"]
    assert [m["member_status"] for m in members] == ["Active", "Active", "Unborn"]
    assert members[1]["relationship_as_printed"] == "Dependent"
    records = build_income_records(ex, members, Settings(cartograph_income_types=["ssi", "child_support"]), warnings)
    r0, r1 = records
    assert r0["rate_unit"] == "monthly" and r0["frequency_of_pay"] == "monthly"
    assert r0["annual_income"] == "11928.00" and r0["calculation"]["method"] == "voi-based"
    assert r0["vois"][0]["rate_unit"] == "monthly"
    assert r1["annual_income"] == "186.68" and r1["calculation"]["alternatives"][0]["status"] == "audit"
    assert r1["payment_history"] == [{"date": "2026-08-05", "amount": "3.59"}, {"date": None, "amount": "3.59"}]


def test_a_scanners_own_text_layer_is_recognised_as_garbled():
    from app.services.pdf_service import _text_layer_is_garbled
    scanner = ("R.lphs Grocery Comp.ny (FEIN: 95-4356!30) 1100 Wesl A.resia Bo!levard Complon CA 90220 08/31t26 "
               "PeBon Number:3682722 NE FTALI AR REOON OO HR Locallon:000?7 29 5500 USO cA s2201 Slraight 23 820 "
               "S6ial S.ddry Emplome wirrh6E M6di€re Emkrye wlhherd Sol Employ.. withh6n (ca) 12 A1 20 5t 10 5S "
               "1,S93.23 46733 336 75 0 250 ro 09,11 0 9l 5! 39 3A 43 13 2A 100 ,ta 2t 3500 15 23 505 03 0 170 "
               "A HIGHLY SATISfI€O CUSTOMER MAOE IHISi PAYCHECK POSSISLE rl sr oo tu")
    assert _text_layer_is_garbled(scanner)[0]
    digital = ("Owner's Certification of Compliance with HUD's Tenant Eligibility and Rent Procedures. Section C. "
               "Household Information 33. No. 34. Last Name 35. First Name 36. MI 37. Rel. 38. Sex 39. Race 40. Eth. "
               "41. Birth Date 42. Special Status 43. Stdnt Stat. 44. ID Code (SSN) 1 Caldera Maria R H F W 1 9/26/1944 "
               "53. Number of Family Members: 1 54. Number of Non-Family Members: 0 55. Total Annual Income: $7,608 "
               "56. 2nd Adjusted Income 1st Floor 401k 10th of the month Effective Date: 1/1/2027 Unit 115")
    assert not _text_layer_is_garbled(digital)[0]
    assert not _text_layer_is_garbled("short layer")[0]


def test_a_scanners_layer_is_judged_over_the_file_and_a_symbol_in_a_word_counts():
    """The Work Number header page of a scanned packet read 10% broken,
    under the per-page line, and its layer replaced a good OCR read with
    "44t2412026" for a pay date. Half the packet's pages were over the
    line. A file with that many broken pages has a scanner's layer on
    every page. A born-digital file has none over the line and keeps
    its layer even when a page or two read a few percent broken."""
    from app.services.pdf_service import _garble_fraction, _scanner_layer_file
    # A page mostly clean but for the scanner's reads of "e" and a date:
    # each on its own is well under the line.
    cleaner = ("Employer: Desert VIP Urgent Care Young EMGY PHY MED GR INC GEN Current As Of 08/28/2026 "
               "Headquarters Address 72630 Fred Waring Dr Ste 101 Palm Desert CA 92260 Federal Employer "
               "Identification No (FEIN) 330992342 Original Hire Date 08/25/2021 Total Time With Employer "
               "0 Yrs 6 Months Employment Status Active Most Recent Start Date 03/13/2026 Name Bianca Avila "
               "Payroll and Salary Details Income and Deductions Pay Rai€ $21 00 Hourly Pay Period Details "
               "Total Gross Earnings $773 43 Pay Date 44t2412026 0atml2026 Typ€ Historical Pay Period Summary")
    frac = _garble_fraction(cleaner)
    assert frac is not None and 0.03 < frac < 0.12
    scanner = ("R.lphs Grocery Comp.ny (FEIN: 95-4356!30) 1100 Wesl A.resia Bo!levard Complon CA 90220 08/31t26 "
               "PeBon Number:3682722 NE FTALI AR REOON OO HR Locallon:000?7 29 5500 USO cA s2201 Slraight 23 820 "
               "S6ial S.ddry Emplome wirrh6E M6di€re Emkrye wlhherd Sol Employ.. withh6n (ca) 12 A1 20 5t 10 5S "
               "1,S93.23 46733 336 75 0 250 ro 09,11 0 9l 5! 39 3A 43 13 2A 100 ,ta 2t 3500 15 23 505 03 0 170 "
               "A HIGHLY SATISfI€O CUSTOMER MAOE IHISi PAYCHECK POSSISLE rl sr oo tu")
    digital = ("Owner's Certification of Compliance with HUD's Tenant Eligibility and Rent Procedures. Section C. "
               "Household Information 33. No. 34. Last Name 35. First Name 36. MI 37. Rel. 38. Sex 39. Race 40. Eth. "
               "41. Birth Date 42. Special Status 43. Stdnt Stat. 44. ID Code (SSN) 1 Caldera Maria R H F W 1 9/26/1944 "
               "53. Number of Family Members: 1 54. Number of Non-Family Members: 0 55. Total Annual Income: $7,608 "
               "56. 2nd Adjusted Income 1st Floor 401k 10th of the month Effective Date: 1/1/2027 Unit 115")
    # Two broken pages out of four: the file is a scanner's, the cleaner page included.
    assert _scanner_layer_file({1: scanner, 2: cleaner, 3: scanner, 4: digital})[0]
    # One broken page among eight is a bad scan stapled into a digital file, not a scanner's layer.
    assert not _scanner_layer_file({1: scanner, **{n: digital for n in range(2, 9)}})[0]
    assert not _scanner_layer_file({1: digital, 2: cleaner})[0]
    assert not _scanner_layer_file({})[0]


def test_three_dated_stubs_decide_the_pay_frequency_over_a_stated_interval():
    from app.services.income_calculator import calculate_paystub_based
    monthly = [PayStubEntry(sourceName="Ralphs", memberName="N A", grossPay="2659.13", payDate=f"2026-0{m}-30", payInterval="weekly") for m in (4, 5, 6)]
    annual, details = calculate_paystub_based(monthly)
    assert annual == f"{2659.13 * 12:.2f}" and "pay dates are monthly" in details
    weekly = [PayStubEntry(sourceName="R", memberName="N", grossPay="500.00", payDate=d, payInterval="weekly") for d in ("2026-08-07", "2026-08-14", "2026-08-21")]
    assert calculate_paystub_based(weekly)[0] == "26000.00"
    # Two stubs cannot infer a period: the stated interval stands.
    assert calculate_paystub_based(monthly[:2])[0] == f"{2659.13 * 52:.2f}"


def test_a_stub_labelled_as_a_work_number_report_is_relabelled_by_what_it_prints():
    from app.services.two_pass_classifier import _label_by_printed_title
    stub = ("Ralphs Grocery Company Pay Period 08/31/26-09/06/26 Pay Date 09/10/26 Gross Pay 509.17 YTD 16,529.10 "
            "Net Pay 432.99 A HIGHLY SATISFIED CUSTOMER MADE THIS PAYCHECK POSSIBLE")
    assert _label_by_printed_title("Work Number / Equifax Report", stub)[0] == "Paystub"
    wn = "The Work Number Employment Data Report Verifier: Palo Verde Permissible purpose: housing Pay Period End 06/30/26 Gross Pay 2,659.13"
    assert _label_by_printed_title("Work Number / Equifax Report", wn) is None


def test_a_voi_row_is_sent_only_when_a_verification_stated_something():
    from app.schemas.extraction import CertificationInfo, IncomeExtraction, SourceIncome
    from app.services.cartograph.adapter import build_household_members, build_income_records
    from app.core.config import Settings
    hh = HouseholdDemographics(houseHold=[HouseholdMember(FirstName="Bianca", LastName="Avila", DOB="2000-12-21", head="H", relationship="Head")])
    stubs = [PayStubEntry(sourceName="Desert VIP", memberName="Bianca Avila", grossPay=g, payDate=d, payInterval="bi-weekly")
             for g, d in (("773.43", "2026-08-28"), ("711.47", "2026-09-11"))]
    declared_only = VerificationIncomeEntry(memberName="Bianca Avila", sourceName="Desert VIP", incomeType="Non-Federal Wage",
                                            type_of_VOI="Self-Declaration", selfDeclaredAmount="21252.00", frequencyOfPay="annually",
                                            verificationStatus="verified", sourcePages=[9])
    work_number = VerificationIncomeEntry(memberName="Bianca Avila", sourceName="Desert VIP", incomeType="Non-Federal Wage",
                                          type_of_VOI="Work Number", rateOfPay="21.00", rateUnit="hourly", hoursPerPayPeriod="36.83",
                                          frequencyOfPay="bi-weekly", ytdAmount="9701.07", ytdStartDate="2026-01-01", ytdEndDate="2026-08-28",
                                          hireDate="2026-03-13", dateReceived="2026-09-10", verificationStatus="verified", sourcePages=[10])
    ex = SimpleNamespace(household_demographics=hh, certification_info=CertificationInfo(effectiveDate="2027-01-01"),
                         income=IncomeExtraction(sourceIncome=SourceIncome(payStub=stubs, verificationIncome=[declared_only, work_number])),
                         income_calculations=[])
    warnings: list[str] = []
    members = build_household_members(ex, warnings)
    records = build_income_records(ex, members, Settings(cartograph_income_types=["wages_and_salaries"]), warnings)
    assert records[0]["vois"] == []
    assert records[0]["frequency_of_pay"] == "bi-weekly"
    voi = records[1]["vois"][0]
    assert voi["voi_type"] == "Work Number" and voi["rate_of_pay"] == "21.00" and voi["rate_unit"] == "hourly"
    assert voi["frequency_of_pay"] == "bi-weekly" and voi["ytd_amount"] == "9701.07" and voi["employment_start_date"] == "2026-03-13"


def test_a_value_on_no_page_is_red_and_the_overall_weights_headline_figures_and_coverage():
    """Scoring measures accuracy, not only provenance: a figure printed on
    no page is red, the figures a reviewer acts on weigh more than the
    fields that describe a record, and what the audit says was missed
    lowers the overall."""
    from app.schemas.extraction import Finding
    from app.schemas.scoring import ExtractionScoreSummary, FieldScore, RecordScoreCard, ScoreFlag, StageScore
    from app.services.field_scorer import apply_coverage
    not_found = FieldScore(field_name="householdIncome", value="16481.00", stages=[
        StageScore(stage="extraction", score=0.85, reason="Extracted"),
        StageScore(stage="source_verification", score=0.30, ceiling=0.49, reason="Not found in source text — verify manually")])
    not_found.recompute()
    assert not_found.flag == ScoreFlag.RED and "Not found in source text" in not_found.flag_message
    good = lambda name, rt="certification": FieldScore(field_name=name, stages=[
        StageScore(stage="extraction", score=0.85), StageScore(stage="source_verification", score=1.0)])
    cert = RecordScoreCard(record_type="certification", record_label="CertificationInfo",
                           fields=[not_found, good("effectiveDate"), good("unitNumber"), good("certificationType")])
    member = RecordScoreCard(record_type="household_member", record_label="A B", fields=[good("FirstName"), good("LastName"), good("DOB")])
    for f in cert.fields + member.fields: f.recompute()
    cert.recompute(); member.recompute()
    s = ExtractionScoreSummary(records=[cert, member]); s.recompute()
    # Plain mean of the seven fields would be (0.49*? ...) ≈ 0.93; the wrong headline income pulls the weighted mean lower.
    plain = sum(f.composite for f in cert.fields + member.fields) / 7
    assert s.field_composite < plain and s.coverage == 1.0 and s.overall_composite == round(s.field_composite, 6)
    apply_coverage(s, [
        Finding(code="CERT_AMOUNT_UNACCOUNTED", text="1 income amount(s) on the certification match no extracted record: $12,708.00.", result="na"),
        Finding(code="CERT_AMOUNT_UNACCOUNTED", text="2 unplaced amount(s) on the certification match no extracted record", result="na"),
        Finding(code="INCOME_DECLARED_NOT_VERIFIED", text="x: Social Security income declared but no verification carries it"),
        Finding(code="HH_SIZE_MISMATCH", text="the certification declares 3 member(s) but 2 were extracted (fewer than declared)"),
        Finding(code="HH_SIZE_MISMATCH", text="the certification declares 1 member(s) but 2 were extracted (more than declared)"),
    ])
    s.recompute()
    assert s.omissions == ["CERT_AMOUNT_UNACCOUNTED", "INCOME_DECLARED_NOT_VERIFIED", "HH_SIZE_MISMATCH"]
    assert s.coverage == 0.76 and abs(s.overall_composite - s.field_composite * 0.76) < 1e-6


def test_wage_history_alone_does_not_establish_a_current_income():
    """A source whose only evidence is period totals (EIV quarters), that
    the household declares nowhere and no current pay document supports,
    is prior employment: reported, not counted, not delivered."""
    from app.schemas.extraction import PayStubEntry, VerificationIncomeEntry
    from app.services.income_calculator import _history_only_note
    q = lambda d: PayStubEntry(memberName="A B", sourceName="Staples", payDate=d, grossPay="1000.00", payInterval="quarterly")
    bare = VerificationIncomeEntry(memberName="A B", sourceName="Staples", incomeType="Non-Federal Wage")
    note = _history_only_note(bare, [q("2025-12-31"), q("2025-09-30")])
    assert note and "wage history only" in note and "2025-12-31" in note
    declared = VerificationIncomeEntry(memberName="A B", sourceName="Staples", incomeType="Non-Federal Wage", declaredAnnualAmount="12000.00")
    assert _history_only_note(declared, [q("2025-12-31")]) is None
    with_rate = VerificationIncomeEntry(memberName="A B", sourceName="Staples", incomeType="Non-Federal Wage", rateOfPay="15.00")
    assert _history_only_note(with_rate, [q("2025-12-31")]) is None
    stub = PayStubEntry(memberName="A B", sourceName="Staples", payDate="2026-06-05", grossPay="700.00", payInterval="bi-weekly")
    assert _history_only_note(bare, [q("2025-12-31"), stub]) is None
    assert _history_only_note(bare, []) is None


def test_coverage_falls_only_when_the_extraction_falls_short():
    from app.schemas.extraction import Finding
    from app.schemas.scoring import ExtractionScoreSummary
    from app.services.field_scorer import apply_coverage
    s = ExtractionScoreSummary()
    apply_coverage(s, [Finding(code="TIC_TOTAL_MISMATCH", text="TIC declares $22,101.12 but extracted sources sum to $39,616.88 (79% higher). Sources: ...")])
    assert s.omissions == [] and s.coverage == 1.0
    apply_coverage(s, [Finding(code="TIC_TOTAL_MISMATCH", text="TIC declares $25,426.00 but extracted sources sum to $18,708.00 (26% lower). Sources: ...")])
    assert s.omissions == ["TIC_TOTAL_MISMATCH"] and s.coverage == 0.92
