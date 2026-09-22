"""Every reconciliation rule pinned with one case where it must fire and one
where it must not. A future change that widens a rule fails here before it
reaches a packet the rule was never written against.

Run from the repository root:  .venv/bin/python -m pytest -q
"""
from app.schemas.extraction import (
    DeclaredIncome, DocumentGroup, IncomeExtraction, PaymentHistoryRow, QuestionnaireDisclosures,
    QuestionnaireEmployment, SourceIncome, VerificationIncomeEntry,
)
from app.services.extractor import (
    _asset_kind,
    _date_on_text,
    _distinctive_amount,
    _drop_total_rows,
    _prune_payment_history,
    _reconcile_assets,
    _reconcile_income,
    _repair_paystub_ytd,
    _unify_paystub_sources,
)
from app.services.pipeline import (
    _collapse_declared_duplicates, _link_questionnaire_to_income, _merge_household_level_sources,
)


# ---------------------------------------------------------------------------
# Total rows
# ---------------------------------------------------------------------------

def _line(amount, page, itype="Social Security", member="A B", doc="TIC"):
    return {"memberName": member, "incomeType": itype, "amount": amount, "page": page, "documentType": doc, "amountPeriod": "annual"}


def test_total_row_on_same_page_is_dropped():
    lines = [_line("100.00", 2), _line("250.00", 2), _line("350.00", 2, itype="Other Income")]
    assert [d["amount"] for d in _drop_total_rows(lines, "t")] == ["100.00", "250.00"]


def test_total_row_needs_two_components():
    lines = [_line("100.00", 2), _line("100.00", 2, itype="Other Income")]
    assert len(_drop_total_rows(lines, "t")) == 2


def test_same_page_sum_drops_the_line_whatever_its_type():
    # Known misfire mode, stated on purpose: on one page a typed line equal
    # to the sum of two or more others is read as the table's total. A real
    # income that happens to equal two others to the cent would be dropped.
    lines = [_line("100.00", 2), _line("250.00", 2), _line("350.00", 2, itype="Child Support")]
    assert len(_drop_total_rows(lines, "t")) == 2


def test_typed_line_across_pages_is_never_a_total():
    lines = [_line("100.00", 2), _line("250.00", 2), _line("350.00", 9, itype="Child Support")]
    assert len(_drop_total_rows(lines, "t", reference_total="350.00")) == 3


def test_untyped_total_across_pages_is_dropped():
    lines = [_line("100.00", 2), _line("250.00", 2), _line("350.00", 9, itype="Other Income")]
    assert len(_drop_total_rows(lines, "t")) == 2


def test_untyped_line_near_certification_total_is_dropped_when_a_component_is_missing():
    lines = [_line("17874.00", 2), _line("9292.80", 2), _line("39326.90", 12, itype="Other Income")]
    assert len(_drop_total_rows(lines, "t", reference_total="39819.16")) == 2
    assert len(_drop_total_rows(lines, "t", reference_total=None)) == 3


def test_untyped_line_far_from_certification_total_is_kept():
    lines = [_line("17874.00", 2), _line("9292.80", 2), _line("5000.00", 12, itype="Other Income")]
    assert len(_drop_total_rows(lines, "t", reference_total="39819.16")) == 3


# ---------------------------------------------------------------------------
# Declared income against verified records
# ---------------------------------------------------------------------------

def _record(member, source, itype, **kw):
    rec = {"memberName": member, "sourceName": source, "incomeType": itype, "verificationStatus": "verified"}
    rec.update(kw)
    return rec


def test_household_level_income_declared_under_another_member_matches_the_one_record():
    vi = [_record("Arnold Lyons", "Oklahoma CSS", "Child Support", rateOfPay="162.70", frequencyOfPay="monthly")]
    d = [_line("3359.56", 2, itype="Child Support", member="Kyla Hamilton")]
    _reconcile_income(vi, d, "AR")
    assert len(vi) == 1 and vi[0]["declaredAnnualAmount"] == "3359.56"


def test_wages_declared_under_another_member_are_not_pooled():
    vi = [_record("Arnold Lyons", "Employer A", "Non-Federal Wage", rateOfPay="18.00", rateUnit="hourly", frequencyOfPay="weekly", hoursPerPayPeriod="40")]
    d = [_line("20000.00", 2, itype="Non-Federal Wage", member="Kyla Hamilton")]
    _reconcile_income(vi, d, "AR")
    assert len(vi) == 2 and vi[1]["verificationStatus"] == "declared_only"


def test_household_level_match_needs_exactly_one_record_of_the_type():
    vi = [_record("Arnold Lyons", "CSS one", "Child Support", rateOfPay="100.00", frequencyOfPay="monthly"),
          _record("Kameryn Meely", "CSS two", "Child Support", rateOfPay="200.00", frequencyOfPay="monthly")]
    d = [_line("3359.56", 2, itype="Child Support", member="Kyla Hamilton")]
    _reconcile_income(vi, d, "AR")
    assert len(vi) == 3


def test_untyped_line_matches_the_members_one_unclaimed_record():
    vi = [_record("Arnold Lyons", "SSA", "Social Security", rateOfPay="1489.50", frequencyOfPay="monthly"),
          _record("Arnold Lyons", "Oklahoma CSS", "Child Support", rateOfPay="162.70", frequencyOfPay="monthly")]
    d = [_line("17874.00", 2, itype="Social Security", member="Arnold Lyons"),
         _line("2867.30", 11, itype="Other Income", member="Arnold Lyons", doc="Application / Housing Questionnaire")]
    _reconcile_income(vi, d, "AR")
    assert len(vi) == 2 and vi[1]["declaredAnnualAmount"] == "2867.30"


def test_second_untyped_declaration_attaches_to_nearest_record_and_is_noted():
    vi = [_record("Arnold Lyons", "SSA", "Social Security", rateOfPay="1489.50", frequencyOfPay="monthly"),
          _record("Arnold Lyons", "Oklahoma CSS", "Child Support", rateOfPay="280.00", frequencyOfPay="monthly")]
    d = [_line("17874.00", 2, itype="Social Security", member="Arnold Lyons"),
         _line("3359.56", 2, itype="Child Support", member="Arnold Lyons"),
         _line("2867.30", 11, itype="Other Income", member="Arnold Lyons", doc="Application / Housing Questionnaire")]
    _reconcile_income(vi, d, "AR")
    assert len(vi) == 2
    assert "2867.30" in vi[1]["evidence"]["alsoDeclared"]


def test_untyped_line_under_another_member_attaches_to_a_household_level_income():
    vi = [_record("Kyla Hamilton", "SSA", "Social Security", rateOfPay="774.40", frequencyOfPay="monthly"),
          _record("Arnold Lyons", "Oklahoma CSS", "Child Support", rateOfPay="280.00", frequencyOfPay="monthly")]
    d = [_line("9292.80", 2, itype="Social Security", member="Kyla Hamilton"),
         _line("3359.56", 2, itype="Child Support", member="Kyla Hamilton"),
         _line("2867.30", 11, itype="Other Income", member="Kyla Hamilton", doc="Application / Housing Questionnaire")]
    _reconcile_income(vi, d, "AR")
    assert len(vi) == 2 and "2867.30" in vi[1]["evidence"]["alsoDeclared"]


def test_untyped_declaration_far_from_every_record_stays_its_own_record():
    vi = [_record("Arnold Lyons", "SSA", "Social Security", rateOfPay="1489.50", frequencyOfPay="monthly")]
    d = [_line("17874.00", 2, itype="Social Security", member="Arnold Lyons"),
         _line("500.00", 11, itype="Other Income", member="Arnold Lyons", doc="Application / Housing Questionnaire")]
    _reconcile_income(vi, d, "AR")
    assert len(vi) == 2 and vi[1]["verificationStatus"] == "declared_only"


# ---------------------------------------------------------------------------
# Declared duplicates across declaration documents
# ---------------------------------------------------------------------------

def _declared_only(member, source, itype, amount, doc):
    return VerificationIncomeEntry(memberName=member, sourceName=source, incomeType=itype,
                                   selfDeclaredAmount=amount, declaredAnnualAmount=amount,
                                   declaredSource=doc, verificationStatus="declared_only")


def test_monthly_basis_on_a_second_document_collapses_into_the_certification_line():
    out, findings = _collapse_declared_duplicates([
        _declared_only("Randy Buck", "Soc. Sec.", "Social Security", "21720.00", "HUD 50059"),
        _declared_only("Randy Buck", "Social Security (declared)", "Social Security", "1810.00", "Zero Income Certification"),
    ])
    assert len(out) == 1 and out[0].sourceName == "Soc. Sec." and out[0].rateOfPay == "1810.00" and not findings


def test_different_figures_for_one_income_keep_the_certification_and_raise_a_finding():
    out, findings = _collapse_declared_duplicates([
        _declared_only("R B", "Soc. Sec.", "Social Security", "21720.00", "HUD 50059"),
        _declared_only("R B", "SS", "Social Security", "15000.00", "Application / Housing Questionnaire"),
    ])
    assert len(out) == 1 and len(findings) == 1 and "15,000.00" in findings[0]


def test_different_income_types_do_not_collapse():
    out, _ = _collapse_declared_duplicates([
        _declared_only("R B", "Soc. Sec.", "Social Security", "21720.00", "HUD 50059"),
        _declared_only("R B", "Pension", "Pension", "6000.00", "HUD 50059"),
    ])
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Declared assets against verified records
# ---------------------------------------------------------------------------

def _asset(owner, kind, balance=None, number=None):
    return {"assetOwner": owner, "accountType": kind, "accountNumber": number, "currentBalance": balance, "verificationStatus": "verified"}


def _decl(owner, kind, amount, page=2, doc="HUD 50059", number=None, income=None):
    return {"assetOwner": owner, "accountType": kind, "amount": amount, "page": page, "documentType": doc,
            "accountNumber": number, "incomeAmount": income, "kind": "asset"}


def test_only_declared_account_of_a_kind_merges_into_the_only_verified_one():
    recs = [_asset("Randy Buck", "Savings", "1123.81", "3000008685")]
    _reconcile_assets(recs, [_decl("Randy Buck", "Savings", "886.00", income="3.00")])
    assert len(recs) == 1 and recs[0]["selfDeclaredAmount"] == "886.00" and recs[0]["incomeAmount"] == "3.00"


def test_two_declared_accounts_of_a_kind_do_not_merge_into_one_verified():
    recs = [_asset("A B", "Savings", "100.00")]
    _reconcile_assets(recs, [_decl("A B", "Savings", "50.00"), _decl("A B", "Savings", "75.00")])
    assert len(recs) == 3


def test_checking_and_savings_are_different_kinds():
    recs = [_asset("A B", "Checking", "100.00")]
    _reconcile_assets(recs, [_decl("A B", "Savings", "50.00")])
    assert len(recs) == 2


def test_cent_exact_figure_matches_across_owner_and_type():
    recs = [_asset("Arnold Lyons", "Real Estate", "6294.74"), _asset("Arnold Lyons", "Checking", "82.05", "x2788")]
    _reconcile_assets(recs, [_decl("Kyla Hamilton", "Other", "82.05", page=8, doc="Questionnaire"),
                             _decl("Arnold Lyons", "Cash", "6294.74", page=13, doc="Asset Self-Certification")])
    assert len(recs) == 2 and recs[1]["selfDeclaredAmount"] == "82.05" and recs[0]["selfDeclaredAmount"] == "6294.74"


def test_round_figure_does_not_match_across_owners():
    recs = [_asset("Arnold Lyons", "Checking", "500.00", "x2788")]
    _reconcile_assets(recs, [_decl("Kyla Hamilton", "Other", "500.00", page=8, doc="Questionnaire")])
    assert len(recs) == 2


def test_zero_declared_lines_make_no_record_unless_numbered():
    recs = []
    _reconcile_assets(recs, [_decl("K", "Checking", "0.00"), _decl("K", "Cash", None, page=10, doc="Asset Self-Certification"),
                             _decl("K", "Savings", "0.00", number="1234")])
    assert [(r["accountType"], r["selfDeclaredAmount"]) for r in recs] == [("Savings", "0.00")]


def test_asset_kind_and_distinctive_amount():
    assert _asset_kind("Checking Account - Non Interest Bearing") == "checking"
    assert _asset_kind("Certificates of Deposit") == "cd"
    assert _asset_kind("Equity in Real Estate") == "real estate"
    assert _asset_kind("Lump Sum Receipts") == "other"
    assert _distinctive_amount("82.05") and _distinctive_amount("6294.74") and _distinctive_amount("120")
    assert not _distinctive_amount("500.00") and not _distinctive_amount("0.00") and not _distinctive_amount("1250")


# ---------------------------------------------------------------------------
# Pay-stub YTD in sequence
# ---------------------------------------------------------------------------

def _stub(date, gross, ytd, member="Sharon Keaton", source="Pinellas County Schools"):
    return {"memberName": member, "sourceName": source, "payDate": date, "grossPay": gross, "ytdGross": ytd}


def test_lost_decimal_in_ytd_is_repaired_from_the_sequence():
    stubs = [_stub("2026-07-17", "1518.00", "759768.00"), _stub("2026-07-31", "2007.32", "9605.00"), _stub("2026-08-14", "2090.46", "11695.46")]
    assert _repair_paystub_ytd(stubs) == 1
    assert stubs[0]["ytdGross"] == "7597.68" and "759768" in stubs[0]["evidence"]["ytdGross"]


def test_ytd_that_fits_no_repair_is_dropped():
    stubs = [_stub("2026-03-01", "1000.00", "500.00"), _stub("2026-03-15", "1000.00", "6000.00")]
    _repair_paystub_ytd(stubs)
    assert stubs[0]["ytdGross"] is None and stubs[1]["ytdGross"] == "6000.00"


def test_consistent_sequence_is_untouched():
    stubs = [_stub("2026-08-05", "1601.74", "8535.28"), _stub("2026-07-20", "1580.49", "6933.54")]
    assert _repair_paystub_ytd(stubs) == 0


def test_stubs_of_different_sources_are_not_compared():
    stubs = [_stub("2026-07-17", "1518.00", "7597.68"), _stub("2026-07-31", "500.00", "500.00", source="Other Employer")]
    assert _repair_paystub_ytd(stubs) == 0


# ---------------------------------------------------------------------------
# Signature date on the certification's own pages
# ---------------------------------------------------------------------------

def test_date_on_text_reads_every_printed_form():
    for printed in ("Signed 08/12/2026", "Date: 8/12/26", "August 12, 2026", "12 Aug 2026", "2026-08-12", "08/ 12/ 2026"):
        assert _date_on_text("2026-08-12", printed), printed


def test_date_on_text_rejects_other_dates_and_embedded_digits():
    assert not _date_on_text("2026-08-12", "Date 08/13/2026")
    assert not _date_on_text("2026-08-12", "108/12/2026x")


# ---------------------------------------------------------------------------
# One employer, one source
# ---------------------------------------------------------------------------

def _ps(source, member="Aridia Perez", emp_id=None, page=26):
    return {"sourceName": source, "memberName": member, "employeeId": emp_id, "grossPay": "432.00",
            "payDate": "2026-06-21", "payInterval": "weekly", "sourcePages": [page], "evidence": {}}


def test_stubs_sharing_an_employee_id_are_one_employer():
    stubs = [_ps("Intralot, Inc.", emp_id="1342892"), _ps("Staffink Investment LLC", emp_id="1342892", page=27)]
    assert _unify_paystub_sources(stubs) == 1
    assert {s["sourceName"] for s in stubs} == {"Staffink Investment LLC"}
    renamed = next(s for s in stubs if s["evidence"].get("sourceName"))
    assert "Intralot" in renamed["evidence"]["sourceName"] and "employee ID" in renamed["evidence"]["sourceName"]


def test_near_identical_employer_names_are_one_employer_and_a_blank_header_yields():
    stubs = [_ps("Staffmark Investment LLC"), _ps("Staffink Investment LLC", page=27), _ps("Staffmark Investment LLC", page=28)]
    _unify_paystub_sources(stubs)
    assert {s["sourceName"] for s in stubs} == {"Staffmark Investment LLC"}
    stubs = [_ps(None, emp_id="77"), _ps("Acme Staffing", emp_id="77", page=27)]
    _unify_paystub_sources(stubs)
    assert {s["sourceName"] for s in stubs} == {"Acme Staffing"}


def test_different_employers_and_different_members_stay_apart():
    stubs = [_ps("Kroger"), _ps("Walmart", page=27)]
    assert _unify_paystub_sources(stubs) == 0
    stubs = [_ps("Kroger", member="A B", emp_id="1"), _ps("Kroger Co", member="C D", emp_id="1", page=27)]
    assert _unify_paystub_sources(stubs) == 0


# ---------------------------------------------------------------------------
# Payment-history dates are printed or absent
# ---------------------------------------------------------------------------

def _ledger_group(text, pages=(28,)):
    return DocumentGroup(document_type="Child Support Statement", category="include", pages=list(pages),
                         page_range="-".join(str(p) for p in pages), combined_text=text)


def test_history_dates_the_page_does_not_print_are_cleared_and_rows_kept():
    worksheet = _ledger_group("Child Support Income Worksheet\n1 $3.59\n2 $3.59\n3 $3.59\nTotal $10.77", pages=(27,))
    rec = {"paymentHistory": [{"date": "2026-01-01", "amount": "3.59"}, {"date": "2026-02-01", "amount": "3.59"},
                              {"date": "2030-03-01", "amount": "3.59"}], "evidence": {}}
    _prune_payment_history([rec], worksheet, "t")
    assert [r["date"] for r in rec["paymentHistory"]] == [None, None, None]
    assert "not printed" in rec["evidence"]["paymentHistory"]


def test_printed_history_dates_survive_in_every_form():
    ledger = _ledger_group("08/05/2026 3.59\n07/27/2026 3.59\nAug 2026 925.90\n07/26 925.90")
    rec = {"paymentHistory": [{"date": "2026-08-05", "amount": "3.59"}, {"date": "2026-07-27", "amount": "3.59"},
                              {"date": "2026-08-01", "amount": "925.90"}, {"date": "2026-07-01", "amount": "925.90"}], "evidence": {}}
    _prune_payment_history([rec], ledger, "t")
    assert [r["date"] for r in rec["paymentHistory"]] == ["2026-08-05", "2026-07-27", "2026-08-01", "2026-07-01"]
    assert "paymentHistory" not in rec["evidence"]


# ---------------------------------------------------------------------------
# One member, one household-level type, one source
# ---------------------------------------------------------------------------

def _cs(source, pages, status, history=None, declared=None):
    return VerificationIncomeEntry(
        memberName="Tionna Simmons", sourceName=source, incomeType="Child Support", type_of_VOI="Child Support Order",
        sourcePages=pages, verificationStatus=status, declaredAnnualAmount=declared,
        selfDeclaredAmount=declared, declaredSource="Tenant Income Certification (TIC)" if declared else None,
        paymentHistory=[PaymentHistoryRow(date=d, amount="3.59") for d in (history or [])],
    )


def test_worksheet_and_ledger_are_one_child_support_source():
    worksheet = _cs("Child Support", [27], "verified", history=[None, None], declared="183.09")
    ledger = _cs("Keith A. Amos", [28, 29, 30], "verified_not_declared", history=["2026-08-05", "2026-07-27"])
    out = _merge_household_level_sources([worksheet, ledger])
    assert len(out) == 1
    kept = out[0]
    assert kept.sourceName == "Keith A. Amos" and kept.sourcePages == [27, 28, 29, 30]
    assert kept.verificationStatus == "verified" and kept.declaredAnnualAmount == "183.09"
    assert len(kept.paymentHistory) == 2 and "page(s) 27" in kept.evidence["alsoDocumented"]


def test_two_payers_with_their_own_ledgers_stay_two_and_wages_never_merge():
    a = _cs("Keith A. Amos", [28], "verified", history=["2026-08-05", "2026-07-27"])
    b = _cs("John Doe", [31], "verified", history=["2026-08-01", "2026-07-01"])
    assert len(_merge_household_level_sources([a, b])) == 2
    w1 = VerificationIncomeEntry(memberName="A B", sourceName="Kroger", incomeType="Non-Federal Wage", sourcePages=[3])
    w2 = VerificationIncomeEntry(memberName="A B", sourceName="Walmart", incomeType="Non-Federal Wage", sourcePages=[4])
    assert len(_merge_household_level_sources([w1, w2])) == 2


# ---------------------------------------------------------------------------
# The application's start date reaches the wage record
# ---------------------------------------------------------------------------

def test_application_start_date_becomes_the_hire_date_when_the_record_has_none():
    vi = VerificationIncomeEntry(memberName="Aridia Perez", sourceName="Staffmark Investment LLC", incomeType="Non-Federal Wage")
    income = IncomeExtraction(sourceIncome=SourceIncome(payStub=[], verificationIncome=[vi]))
    disclosures = QuestionnaireDisclosures(has_employment=True, employers=["Staffmark"],
                                           employment=[QuestionnaireEmployment(employer="Staffmark", start_date="2026-04-13")])
    findings = _link_questionnaire_to_income(disclosures, income, [])
    assert vi.hireDate == "2026-04-13" and "application" in vi.evidence["hireDate"]
    assert not findings
    stated = VerificationIncomeEntry(memberName="A", sourceName="Staffmark", incomeType="Non-Federal Wage", hireDate="2025-01-06")
    income = IncomeExtraction(sourceIncome=SourceIncome(payStub=[], verificationIncome=[stated]))
    _link_questionnaire_to_income(disclosures, income, [])
    assert stated.hireDate == "2025-01-06"


def test_the_only_employer_on_the_application_dates_the_only_wage_source():
    vi = VerificationIncomeEntry(memberName="Aridia Perez", sourceName="Staffink Investment LLC",
                                 incomeType="Non-Federal Wage", verificationStatus="verified")
    income = IncomeExtraction(sourceIncome=SourceIncome(payStub=[], verificationIncome=[vi]))
    income.declared = [DeclaredIncome(memberName="Aridia Perez", sourceName="Stafmark", incomeType="Non-Federal Wage",
                                      amount="3320.00", amountPeriod="monthly", page=19, documentType="Application")]
    disclosures = QuestionnaireDisclosures(has_employment=True, employers=["Stafmark"],
                                           employment=[QuestionnaireEmployment(employer="Stafmark", start_date="2026-04-13")])
    _link_questionnaire_to_income(disclosures, income, [])
    assert vi.hireDate == "2026-04-13" and "only employer" in vi.evidence["hireDate"]
    # Two wage sources in the file: the application's one employer names neither.
    other = VerificationIncomeEntry(memberName="B C", sourceName="Kroger", incomeType="Non-Federal Wage", verificationStatus="verified")
    vi.hireDate = None
    income = IncomeExtraction(sourceIncome=SourceIncome(payStub=[], verificationIncome=[vi, other]))
    income.declared = [DeclaredIncome(memberName="Aridia Perez", sourceName="Stafmark", incomeType="Non-Federal Wage",
                                      amount="3320.00", amountPeriod="monthly", page=19, documentType="Application")]
    _link_questionnaire_to_income(disclosures, income, [])
    assert vi.hireDate is None and other.hireDate is None


def test_stubs_alone_verify_a_disclosed_job_and_a_self_certification_covers_a_checking_account():
    from app.services.questionnaire_extractor import validate_affirmative_responses
    groups = [DocumentGroup(document_type="Pay Stub", category="include", pages=[26], page_range="26", combined_text="x"),
              DocumentGroup(document_type="Asset Self-Certification", category="include", pages=[23], page_range="23", combined_text="x")]
    disclosures = QuestionnaireDisclosures(has_employment=True, has_checking_account=True)
    assert validate_affirmative_responses(disclosures, groups) == []
    bare = [DocumentGroup(document_type="Tenant Income Certification (TIC)", category="include", pages=[1], page_range="1", combined_text="x")]
    texts = validate_affirmative_responses(disclosures, bare)
    assert any("Employment disclosed" in t for t in texts) and any("Checking account disclosed" in t for t in texts)
