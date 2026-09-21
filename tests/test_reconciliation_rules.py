"""Every reconciliation rule pinned with one case where it must fire and one
where it must not. A future change that widens a rule fails here before it
reaches a packet the rule was never written against.

Run from the repository root:  .venv/bin/python -m pytest -q
"""
from app.schemas.extraction import VerificationIncomeEntry
from app.services.extractor import (
    _asset_kind,
    _date_on_text,
    _distinctive_amount,
    _drop_total_rows,
    _reconcile_assets,
    _reconcile_income,
    _repair_paystub_ytd,
)
from app.services.pipeline import _collapse_declared_duplicates


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
