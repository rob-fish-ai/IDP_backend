"""Household, certification and classification rules from the Middletown
packet verifications, each pinned with a case that must fire and one that
must not."""
from types import SimpleNamespace

from app.schemas.context import PipelineContext
from app.schemas.extraction import (
    CertificationInfo, DocumentGroup, DocumentInventory, HouseholdDemographics, HouseholdMember,
)
from app.services import findings as F
from app.services.cert_type_rules import validate_cert_type_requirements
from app.services.doc_taxonomy import canonical_label
from app.services.extractor import _labelled_total, required_member_gaps
from app.services.field_scorer import RecordScorer, _score_income_rules, _score_member_rules
from app.services.members import is_unborn
from app.services.pipeline import (
    _deduplicate_household_members, _document_certification_type, _rent_identity_findings, _same_cert_type,
)
from app.services.signature_validator import _check_signed_after_effective, _count_adults, validate_signatures
from app.services.two_pass_classifier import _label_by_printed_title, _post_group_title_relabel


def _group(label, pages, text="x", category="include"):
    return DocumentGroup(document_type=label, category=category, pages=list(pages),
                         page_range=f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0]), combined_text=text)


# ---------------------------------------------------------------------------
# Labels: the page's printed title decides
# ---------------------------------------------------------------------------

def test_new_labels_and_aliases_resolve():
    assert canonical_label("Student Certification")[0] == "Student Status Certification"
    assert canonical_label("Low Income Housing Tax Credit Lease Addendum")[0] == "LIHTC Lease Addendum"
    assert canonical_label("Residential Lease")[0] == "Lease Agreement"
    assert canonical_label("VAWA Addendum")[0] == "VAWA Lease Addendum"


def test_printed_title_relabels_only_when_the_page_does_not_name_its_own_label():
    tic = "Ohio Housing Finance Agency Tenant Income Certification Move-In Date: 5/29/26 PART II"
    assert _label_by_printed_title("HUD 3560 Form", tic)[0] == "Tenant Income Certification (TIC)"
    assert _label_by_printed_title("Tenant Income Certification (TIC)", tic) is None
    vawa = "LEASE ADDENDUM Violence Against Women and Justice Department Reauthorization Act of 2005 (VAWA)"
    assert _label_by_printed_title("VAWA Lease Addendum", vawa) is None
    lihtc = "Ohio Housing Finance Agency Low Income Housing Tax Credit Lease Addendum ... Violence Against Women Act"
    assert _label_by_printed_title("VAWA Lease Addendum", lihtc)[0] == "LIHTC Lease Addendum"
    app = "VAN ROOY TAX CREDIT RENTAL APPLICATION Property Name ... Tenant Income Certification Questionnaire"
    assert _label_by_printed_title("Application / Housing Questionnaire", app) is None


def test_relabelled_pages_leave_their_group():
    text = {14: "TAX CREDIT RENTAL APPLICATION family data", 15: "housing information", 16: "employment information",
            17: "signature clause", 18: "Ohio Housing Finance Agency Student Certification TO BE COMPLETED BY ALL"}
    groups = [_group("Application / Housing Questionnaire", [14, 15, 16, 17, 18])]
    out, updates = _post_group_title_relabel(groups, text)
    assert [(g.document_type, g.pages) for g in out] == [
        ("Application / Housing Questionnaire", [14, 15, 16, 17]), ("Student Status Certification", [18])]
    assert updates == [(18, "Student Status Certification", "include", updates[0][3])] and "headed" in updates[0][3]
    # A previous-certification group and an ignored group are never touched.
    prev = [_group("HUD 50059 (Previous)", [5], category="ignore")]
    assert _post_group_title_relabel(prev, {5: "Tenant Income Certification"})[1] == []


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def _member(first, last, dob, ssn, n="01", **kw):
    return HouseholdMember(FirstName=first, LastName=last, DOB=dob, socialSecurityNumber=ssn, householdMemberNumber=n, **kw)


def test_same_dob_and_ssn_last_four_is_one_member_whatever_the_surname():
    hh = HouseholdDemographics(houseHold=[
        _member("Aridia", "Perez Trinidad", "1988-06-08", "***-**-8076", head="H", relationship="Head"),
        _member("Aridia", "Perez", "1988-06-08", "***-**-8076", "02"),
        _member("Mattwes", "Diaz", "2021-10-01", "***-**-1914", "03"),
    ])
    findings = _deduplicate_household_members(hh)
    assert [m.LastName for m in hh.houseHold] == ["Perez Trinidad", "Diaz"]
    assert any(f.code == "MEMBER_MERGED" and "same date of birth" in f.text for f in findings)
    # Same surname, different people: siblings with different DOBs stay two.
    hh = HouseholdDemographics(houseHold=[_member("Ana", "Diaz", "2015-01-01", "***-**-1111"),
                                          _member("Eva", "Diaz", "2018-01-01", "***-**-2222", "02")])
    assert not _deduplicate_household_members(hh) and len(hh.houseHold) == 2


def test_unborn_child_is_a_member_with_no_identity_fields():
    unborn = HouseholdMember(FirstName="Unborn", LastName="Child", relationship="Unborn Child", householdMemberNumber="03")
    assert is_unborn(unborn) and not is_unborn(_member("Ana", "Diaz", "2015-01-01", None))
    hh = HouseholdDemographics(houseHold=[_member("Aridia", "Perez", "1988-06-08", "***-**-8076", relationship="Head"), unborn])
    assert _count_adults(hh, CertificationInfo(effectiveDate="2026-05-29")) == 1
    assert required_member_gaps([m.model_dump() for m in hh.houseHold]) == []
    sc = RecordScorer("household_member", "Unborn Child")
    for f, v in (("FirstName", "Unborn"), ("LastName", "Child"), ("relationship", "Unborn Child"),
                 ("DOB", None), ("socialSecurityNumber", None), ("student", None), ("disabled", None)):
        sc.score_field(f, v)
    card = sc.build()
    _score_member_rules(card, "Tenant Income Certification (TIC)")
    na = {fs.field_name for fs in card.fields if fs.flag.value == "na"}
    assert {"DOB", "socialSecurityNumber", "student", "disabled"} <= na


# ---------------------------------------------------------------------------
# Certification
# ---------------------------------------------------------------------------

def test_move_in_date_equal_to_effective_date_is_a_move_in():
    assert _document_certification_type(CertificationInfo(effectiveDate="2026-05-29", moveInDate="2026-05-29")) == "MI"
    assert _document_certification_type(CertificationInfo(effectiveDate="2026-05-29", moveInDate="2019-03-01")) is None
    assert _document_certification_type(CertificationInfo(effectiveDate="2026-05-29")) is None
    assert _same_cert_type("MI", "IC") and not _same_cert_type("MI", "AR")


def test_rent_identity_holds_or_is_a_finding_on_tax_credit_forms_only():
    tic = [_group("Tenant Income Certification (TIC)", [1, 2])]
    bad = CertificationInfo(tenantRent="580.00", utilityAllowance="116.00", grossRent="746.00")
    out = _rent_identity_findings(bad, tic)
    assert len(out) == 1 and out[0].code == "RENT_IDENTITY_MISMATCH" and F.disputes_extraction(out[0].code)
    good = CertificationInfo(tenantRent="0.00", utilityAllowance="154.00", grossRent="154.00")
    assert _rent_identity_findings(good, tic) == []
    assert _rent_identity_findings(bad, [_group("HUD 50059", [1, 2])]) == []
    assert _rent_identity_findings(CertificationInfo(tenantRent="580.00"), tic) == []


def test_certification_signed_long_after_its_effective_date_is_a_finding():
    late = CertificationInfo(effectiveDate="2026-05-29", signatureDate="2026-09-10")
    out = _check_signed_after_effective(late)
    assert len(out) == 1 and out[0].code == "CERT_SIGNED_AFTER_EFFECTIVE_DATE" and "104 days" in out[0].text
    assert _check_signed_after_effective(CertificationInfo(effectiveDate="2026-05-29", signatureDate="2026-06-01")) == []
    assert _check_signed_after_effective(CertificationInfo(effectiveDate="2026-05-29", signatureDate="2026-05-01")) == []
    assert _check_signed_after_effective(CertificationInfo(effectiveDate="2026-05-29")) == []


def test_hud_only_move_in_forms_are_owed_by_hud_properties_only():
    hh = HouseholdDemographics(houseHold=[_member("T", "S", "1972-07-27", "***-**-5009")])
    lihtc = [_group("Tenant Income Certification (TIC)", [1, 2]), _group("Application / Housing Questionnaire", [14, 17])]
    codes = {f.code for f in validate_cert_type_requirements("MI", lihtc, None, hh)}
    assert codes == set()
    hud = lihtc + [_group("HUD 50059", [3, 4])]
    codes = {f.code for f in validate_cert_type_requirements("MI", hud, None, hh)}
    assert {"MI_CITIZENSHIP_DECLARATION_MISSING", "MI_RACE_ETHNIC_FORM_MISSING"} <= codes
    # The application is owed on every move-in.
    codes = {f.code for f in validate_cert_type_requirements("MI", lihtc[:1], None, hh)}
    assert codes == {"MI_APPLICATION_MISSING"}


def test_race_and_ethnic_form_rule_follows_the_hud_gate():
    hh = HouseholdDemographics(houseHold=[_member("T", "S", "1972-07-27", "***-**-5009")])
    inv = DocumentInventory(documents=[])
    ci = CertificationInfo(effectiveDate="2026-05-29", signatureDate="2026-05-29", isSigned="Yes")
    texts = [F.text_of(f) for f in validate_signatures(
        inv, inv, hh, ci, [_group("Tenant Income Certification (TIC)", [1, 2])], PipelineContext())]
    assert not any("Race and Ethnic" in t for t in texts)
    texts = [F.text_of(f) for f in validate_signatures(
        inv, inv, hh, ci, [_group("HUD 50059", [1, 2])], PipelineContext())]
    assert any("Race and Ethnic" in t for t in texts)


# ---------------------------------------------------------------------------
# Declared lines and stub-backed records
# ---------------------------------------------------------------------------

def test_a_whole_form_total_printed_nowhere_else_is_not_a_declared_line():
    sworn = {21: 'Real Property Cash Value: [blank]\nTotal Value of Non-Necessary Personal Property: $ NONE '
                 '[handwritten above: "$25.00" with initials] (Value of items)\nTotal of Net Assets: $ 0'}
    assert _labelled_total("25.00", sworn, [21])
    assert not _labelled_total("25.00", {19: "Do you have a Checking Account? Current Balance: 25.00 Interest Rate: 0"}, [19])
    assert not _labelled_total("100", {1: "Total Annual Income: 5000\nChild support 100"}, [1])
    # A certification's single income line is printed in its row and again on
    # the total line: the row is the declaration.
    tic = {1: "Simmons Tionna Non-emp Child support Ledger 183.09 Totals PART IV ... "
              "TOTAL ANNUAL HOUSEHOLD INCOME FROM ALL SOURCES: $ 183.09 Income Equates to: 0 % AMGI"}
    assert not _labelled_total("183.09", tic, [1])
    # Per-type subtotals on a worksheet are the accounts; the grand total is not.
    ws = {18: 'Asset Type Total Value Asset Income Total Checking (all) $50.00 $0.00 Total Savings/Money Market (all) '
              '$600.00 $0.90 Total "Other" (all) $20.00 $0.00 Total Real Estate 0 Total Value all Assets $670.00'}
    assert [_labelled_total(a, ws, [18]) for a in ("50.00", "600.00", "20.00", "670.00")] == [False, False, False, True]


def test_fields_an_employer_form_would_carry_are_not_gaps_on_a_stub_backed_record():
    sc = RecordScorer("income", "Aridia Perez — Staffmark")
    for f, v in (("memberName", "Aridia Perez"), ("sourceName", "Staffmark"), ("type_of_VOI", "Pay Stubs"),
                 ("selfDeclaredAmount", "29133.00"), ("rateOfPay", None), ("hoursPerPayPeriod", None),
                 ("ytdAmount", None), ("dateReceived", None)):
        sc.score_field(f, v)
    card = sc.build()
    _score_income_rules(card, None)
    na = {fs.field_name for fs in card.fields if fs.flag.value == "na"}
    assert {"rateOfPay", "hoursPerPayPeriod", "ytdAmount", "dateReceived"} <= na
    sc = RecordScorer("income", "A — B")
    for f, v in (("memberName", "A"), ("sourceName", "B"), ("type_of_VOI", "Employer Verification"),
                 ("rateOfPay", "18.00"), ("hoursPerPayPeriod", None), ("ytdAmount", None)):
        sc.score_field(f, v)
    card = sc.build()
    _score_income_rules(card, None)
    assert not any(fs.field_name == "hoursPerPayPeriod" and fs.flag.value == "na" for fs in card.fields)


def test_certification_pages_without_a_readable_title_are_labelled_by_their_printed_form():
    p1 = ("Move-In Date: 05/29/2026 Certification Date: 05/29/2026 PART II - HOUSEHOLD COMPOSITION "
          "Income Equates to: 15 % AMGI HH Meets Income Restriction at 60 %")
    assert _label_by_printed_title("HUD 3560 Form", p1)[0] == "Tenant Income Certification (TIC)"
    p50059 = "OWNER'S CERTIFICATION OF COMPLIANCE WITH HUD'S TENANT ELIGIBILITY AND RENT PROCEDURES HUD-50059 Total Tenant Payment"
    assert _label_by_printed_title("Tenant Income Certification (TIC)", p50059)[0] == "HUD 50059"
    assert _label_by_printed_title("HUD 50059", p50059) is None
    # One phrase alone does not relabel a certification form.
    assert _label_by_printed_title("HUD 3560 Form", "some page mentioning amgi once") is None
    # The form's second page, with no marker of its own, follows the first.
    text = {1: p1, 2: "PART VI - RENT Tenant Paid Rent: $580 Utility Allowance: $166 SIGNATURES"}
    out, updates = _post_group_title_relabel([_group("HUD 3560 Form", [1, 2])], text)
    assert [(g.document_type, g.pages) for g in out] == [("Tenant Income Certification (TIC)", [1, 2])]
    assert {u[0] for u in updates} == {1, 2}


def test_the_household_size_is_the_count_the_form_prints():
    from app.services.extractor import _household_size_on_form
    hud = ("52. Family has Visual Disability?: N 53. Number of Family Members: 1 54. Number of Non-Family Members: 0 "
           "55. Total Annual Income")
    assert _household_size_on_form(hud) == (1, "Number of Family Members: 1; Number of Non-Family Members: 0")
    assert _household_size_on_form("53. Number of Family Members: 3 54. Number of Non-Family Members: 1")[0] == 4
    tic = "Move-In Date: 5/29/26 Certification Date: 5/29/26 Current Household Size: 3 Project Name: MP3"
    assert _household_size_on_form(tic) == (3, "Current Household Size: 3")
    assert _household_size_on_form("Number of Family Members: [blank]")[0] is None
    assert _household_size_on_form("no such field here")[0] is None


def test_an_account_printed_whole_and_as_its_last_four_is_one_asset():
    from app.schemas.extraction import AssetEntry
    from app.services.pipeline import _deduplicate_assets
    a = AssetEntry(assetOwner="Juan Garcia Ortega", accountType="Investment", sourceName="LPL Financial",
                   accountNumber="3872-4603", currentBalance="106195.56", sourcePages=[19])
    b = AssetEntry(assetOwner="Juan Garcia Ortega", accountType="Investment", sourceName="LPL Financial",
                   accountNumber="4603", currentBalance="106195.56", sourcePages=[20, 21, 22])
    c = AssetEntry(assetOwner="Juan Garcia Ortega", accountType="Checking", sourceName="Chase",
                   accountNumber="9901", currentBalance="39.48", sourcePages=[25])
    out = _deduplicate_assets([a, b, c])
    assert len(out) == 2 and {r.accountType for r in out} == {"Investment", "Checking"}


def test_compound_surnames_printed_whole_or_cut_are_one_member():
    from app.services.extractor import _same_member
    assert _same_member("Neftali Arredondo Mora", "Neftali Arredondo")
    assert _same_member("Neftali Arredondo Mora", "Neftali Arredondo-Mota")
    assert _same_member("Juan Garcia Ortega", "Juan Garcia")
    assert _same_member("Arnold Lyons", "Arnold J Lyons")
    assert not _same_member("Juan Garcia Ortega", "Maria Garcia")
    assert not _same_member("Beatriz Ibarra Almanza", "Beatriz Elena Ibarra Morales")
