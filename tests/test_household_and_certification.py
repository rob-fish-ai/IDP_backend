"""Household, certification and classification rules from the Middletown
packet verifications, each pinned with a case that must fire and one that
must not."""
from types import SimpleNamespace

from app.schemas.context import PipelineContext
from app.schemas.extraction import (
    QuestionnaireDisclosures,
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


def test_different_given_names_are_different_people_whatever_the_surname():
    from app.services.name_reconciler import _name_similarity, _CLUSTER_THRESHOLD as T
    assert _name_similarity("Juan Garcia Ortega", "Maria Garcia") < T
    assert _name_similarity("Doug Ibarra Alfianza", "Aima Morales Ibarra") < T
    # Variants of one given name still cluster: a letter off, a prefix, an initial.
    assert _name_similarity("Aima Morales Ibarra", "Alma Morales") >= T
    assert _name_similarity("Maria Caldera", "Mary A. Caldera") >= T
    assert _name_similarity("Neftali Arredondo Mora", "Neftali Arredondo") >= T


def test_eiv_documents_are_three_labels_and_the_report_is_income_evidence():
    from app.services.doc_taxonomy import category_of, family_of
    from app.services.extractor import _is_income_declaration
    assert canonical_label("EIV Report")[0] == "EIV Income Report"
    assert canonical_label("Wage and Benefit Report")[0] == "EIV Income Report"
    assert canonical_label("Income Report Confirmation")[0] == "EIV Income Report Confirmation"
    assert category_of("EIV Income Report") == "include" and family_of("EIV Income Report") == "income"
    assert category_of("EIV Summary Report") == "compliance"
    assert _is_income_declaration(_group("EIV Income Report Confirmation", [8]))
    assert not _is_income_declaration(_group("EIV Income Report", [8]))
    report = ("Income Report Contract Number: Wage and Benefit Report for Household of RANDY BUCK Employment Information "
              "EIV received no Employment (W4) data. Social Security Benefits Verification Data Gross Benefit Net Monthly "
              "Benefit if Payable: $1,810.00 Date Received by EIV: 08-MAY-26")
    assert _label_by_printed_title("EIV Summary Report", report)[0] == "EIV Income Report"
    cover = ("EIV INCOME REPORT CONFIRMATION (use one form per adult member) Run income report within 90 days of move-in "
             "Retain this coversheet in file with EIV reports. Tenant Agrees / Disagrees with Income Report? "
             "If resident agrees with income report, no other 3rd party verification is necessary. Compliance Co-Op 2016")
    assert _label_by_printed_title("EIV Summary Report", cover)[0] == "EIV Income Report Confirmation"
    summary = "Summary Report Head of Household Identifiers Name: Social Security Number: Identity Verification Status Verified"
    assert _label_by_printed_title("EIV Summary Report", summary) is None
    # A three-page report whose last page carries no section heading follows the first two.
    text = {8: report, 9: "Social Security Benefits Verification Data 12/2022 1,664.00 Benefits paid Dual Entitlement EIV received no benefit data",
            10: "Report Generated By - MGGXXX Confidential Privacy Act Data"}
    out, _ = _post_group_title_relabel([_group("EIV Summary Report", [8, 9, 10], category="compliance")], text)
    assert [(g.document_type, g.pages) for g in out] == [("EIV Income Report", [8, 9, 10])]


def test_a_disclosed_ssa_benefit_is_verified_by_the_eiv_report_or_its_countersigned_confirmation():
    """A countersigned confirmation sheet stands in for the report it
    restates, so SSA disclosed on the questionnaire is not "unverified"
    when only the coversheet is filed. What is missing is the printout,
    and that is one narrow finding, not one per figure."""
    from app.services.questionnaire_extractor import validate_affirmative_responses
    from app.services.cross_doc_validator import validate_confirmation_reports
    from app.services.findings import text_of
    d = QuestionnaireDisclosures(has_ssa_benefits=True)
    assert validate_affirmative_responses(d, [_group("EIV Income Report", [8])]) == []
    assert validate_affirmative_responses(d, [_group("EIV Income Report Confirmation", [8])]) == []
    texts = validate_affirmative_responses(d, [_group("Bank Statement", [8])])
    assert any("SSA/SSI/SSDI" in text_of(t) for t in texts)

    only_sheets = [_group("EIV Income Report Confirmation", [8]), _group("EIV Income Report Confirmation", [9])]
    out = validate_confirmation_reports(only_sheets)
    assert [f.code for f in out] == ["CONFIRMATION_WITHOUT_REPORT"] and out[0].pages == [8, 9]
    assert validate_confirmation_reports(only_sheets + [_group("EIV Income Report", [10])]) == []


def test_a_disclosed_asset_with_nothing_in_the_file_is_an_asset_finding_that_asks_for_a_record():
    """The consumer keys "move to action" on category and subject: a
    disclosed asset with no record arrives as `asset` / `asset_record` with
    the correction naming the record to add, while student status stays a
    file review item. The wording is what the plain-string findings said."""
    from app.services.questionnaire_extractor import validate_affirmative_responses
    from app.services.cartograph.adapter import build_findings
    d = QuestionnaireDisclosures(has_life_insurance=True, has_real_estate=True, has_student_status=True,
                                 has_ssa_benefits=True)
    out = validate_affirmative_responses(d, [_group("Tenant Income Certification (TIC)", [1])])
    by_code = {f.code: f for f in out}
    assert set(by_code) == {"QUESTIONNAIRE_LIFE_INSURANCE_UNVERIFIED", "QUESTIONNAIRE_REAL_ESTATE_UNVERIFIED",
                            "QUESTIONNAIRE_STUDENT_UNVERIFIED", "QUESTIONNAIRE_SSA_UNVERIFIED"}
    life = by_code["QUESTIONNAIRE_LIFE_INSURANCE_UNVERIFIED"]
    assert (life.category, life.subject_type) == ("asset", "asset_record")
    assert life.correction_required.startswith("Add an asset record")
    assert life.text.startswith("Life insurance disclosed on questionnaire")
    assert by_code["QUESTIONNAIRE_REAL_ESTATE_UNVERIFIED"].subject_type == "asset_record"
    assert (by_code["QUESTIONNAIRE_SSA_UNVERIFIED"].category, by_code["QUESTIONNAIRE_SSA_UNVERIFIED"].subject_type) == ("income", "income_record")
    student = by_code["QUESTIONNAIRE_STUDENT_UNVERIFIED"]
    assert (student.category, student.subject_type) == ("file_review", None)
    # Same finding, same key on a re-scan, and the row Cartograph stores is no longer a NOTE.
    assert life.finding_key == "QUESTIONNAIRE_LIFE_INSURANCE_UNVERIFIED:case"
    extraction = SimpleNamespace(finding_records=out, findings=[f.text for f in out])
    rows = {r["code"]: r for r in build_findings(extraction, [])}
    assert "NOTE" not in rows
    assert rows["QUESTIONNAIRE_LIFE_INSURANCE_UNVERIFIED"]["category"] == "asset"
    assert rows["QUESTIONNAIRE_LIFE_INSURANCE_UNVERIFIED"]["subject_type"] == "asset_record"
    assert rows["QUESTIONNAIRE_LIFE_INSURANCE_UNVERIFIED"]["assignment"] == "client"


def test_the_hud_consent_forms_keep_their_label_when_headed_by_their_printed_title():
    """The 9887's title contains "Release of Information", an alias of the
    generic release type; the matcher takes the longest alias found, so the
    form resolves to itself and is not then reported missing."""
    from app.services.two_pass_classifier import _post_group_title_relabel
    text = {21: "Notice and Consent for the Release of Information to the U.S. Department of Housing "
                "and Urban Development (HUD) and to an Owner and Management Agent (O/A) ...",
            22: "Agencies To Provide Information. State Wage Information Collection Agencies ..."}
    out, _ = _post_group_title_relabel([_group("HUD 9887", [21, 22], category="compliance")], text)
    assert [(g.document_type, g.pages) for g in out] == [("HUD 9887", [21, 22])]
    text = {23: "Applicant's/Tenant's Consent to the Release of Information Verification by Owners ..."}
    out, _ = _post_group_title_relabel([_group("HUD 9887-A", [23], category="compliance")], text)
    assert [g.document_type for g in out] == ["HUD 9887-A"]


def test_a_certification_is_flagged_unsigned_once():
    """One finding per form per defect: the pipeline reports an unsigned
    certification outright, so the signature validator does not add a
    "could not verify a signature" note for the same form."""
    from app.schemas.extraction import CertificationInfo, DocumentInventory, DocumentInventoryEntry, HouseholdDemographics
    from app.services.signature_validator import validate_signatures
    from app.services.findings import text_of
    from types import SimpleNamespace
    hud = DocumentInventory(documents=[DocumentInventoryEntry(documentType="HUD 50059", isSigned="No")])
    fin = DocumentInventory(documents=[])
    hh = HouseholdDemographics(houseHold=[])
    ctx = SimpleNamespace(funding_program="HUD")
    unsigned = validate_signatures(hud, fin, hh, CertificationInfo(isSigned="No"), [], ctx)
    assert not any("HUD 50059 must be signed" in text_of(f) for f in unsigned)
    unknown = validate_signatures(hud, fin, hh, CertificationInfo(isSigned=None), [], ctx)
    assert any("HUD 50059 must be signed" in text_of(f) for f in unknown)


def test_a_multi_member_table_is_read_by_row_and_a_neighbours_ssn_is_no_conflict():
    """An interview checklist lists every member with their own SSN under
    one NAME column. Each row's SSN belongs to the member named in it, and
    a value attributed elsewhere to the wrong member is recognised as
    another member's SSN, not a conflict."""
    from app.schemas.extraction import HouseholdDemographics, HouseholdMember
    from app.services.identity import resolve_identities, collect_identity_claims
    hh = HouseholdDemographics(houseHold=[
        HouseholdMember(FirstName="Yolanda", LastName="Bribiesca", socialSecurityNumber="***-**-3484", relationship="Head"),
        HouseholdMember(FirstName="Jasmine", LastName="Bribiesca", socialSecurityNumber="***-**-8733"),
        HouseholdMember(FirstName="Sofia", LastName="Bribiesca-Soto", socialSecurityNumber="***-**-2006"),
    ])
    cert = ("<table><tr><th>Last Name</th><th>First Name</th><th>ID Code (SSN)</th></tr>"
            "<tr><td>Bribiesca</td><td>Yolanda</td><td>617103484</td></tr>"
            "<tr><td>Bribiesca</td><td>Jasmine</td><td>611618733</td></tr>"
            "<tr><td>Bribiesca-Soto</td><td>Sofia</td><td>609752006</td></tr></table>")
    checklist = ("NAME: Yolanda Bribiesca <table><tr><th>FAMILY MBR NO.</th><th>NAME</th><th>RELATIONSHIP TO</th>"
                 "<th>SOCIAL SECURITY NO.</th><th>BIRTHDATE</th></tr>"
                 "<tr><td>1</td><td>Yolanda Bribiesca</td><td>HEAD</td><td>617-10-3484</td><td>02/18/69</td></tr>"
                 "<tr><td>2</td><td>Jasmine Bribiesca</td><td>Other Adult</td><td>611-61-8733</td><td>12/22/06</td></tr>"
                 "<tr><td>3</td><td>Sofia Bribiesca</td><td>Child</td><td>609-75-2006</td><td>02/26/09</td></tr></table>")
    groups = [_group("HUD 50059", [2]), DocumentGroup(document_type="Application / Housing Questionnaire", category="include", pages=[6], page_range="6", combined_text="x", person_name="Yolanda Bribiesca")]
    claims = collect_identity_claims(hh.houseHold, groups, {2: cert, 6: checklist})
    by_member = {k: sorted({x["value"][-4:] for x in v["ssn"]}) for k, v in claims.items()}
    assert all(len(v) == 1 for v in by_member.values()), by_member
    assert [f.code for f in resolve_identities(hh, groups, {2: cert, 6: checklist})] == []


def test_a_surname_one_edit_from_the_certifications_spelling_takes_the_forms_spelling():
    from app.schemas.extraction import HouseholdDemographics, HouseholdMember
    from app.services.identity import resolve_identities
    hh = HouseholdDemographics(houseHold=[HouseholdMember(FirstName="Sofia", LastName="Bribiesca-Solo")])
    cert = "<tr><td>Bribiesca-Soto</td><td>Sofia</td><td>M</td><td>D</td></tr>"
    resolve_identities(hh, [_group("HUD 50059", [2])], {2: cert})
    assert hh.houseHold[0].LastName == "Bribiesca-Soto"
    # No snap when the form prints the name as extracted, or when two candidates are one edit away.
    hh = HouseholdDemographics(houseHold=[HouseholdMember(FirstName="Ana", LastName="Soto")])
    resolve_identities(hh, [_group("HUD 50059", [2])], {2: "Soto Ana; Sota Luis; Soro Eva"})
    assert hh.houseHold[0].LastName == "Soto"


def test_a_fixed_forms_labelled_figure_wins_and_a_question_is_not_an_answer():
    """J-CCAC-07076: the 50059 prints "Tenant Rent: $246" and "Assistance
    Payment: $1,665"; the model delivered 1,665 as the tenant rent. The
    printed figure beside the field's own label wins. And nine cases were
    flagged homeless because the questionnaire asks "Are you homeless?
    Yes No" — a question on every copy of a form is not an indication."""
    from app.services.extractor import _labelled_cert_amounts
    from app.services.special_scenarios import _indicated
    form = ("108. Total Tenant Payment: $286 110. Tenant Rent: $246 111. Utility Reimbursement: $0 "
            "112. Assistance Payment: $1,665 86. Total Annual Income: $11,462 31. Gross Rent: $1,951.00")
    got = _labelled_cert_amounts(form)
    assert got == {"tenantRent": "246.00", "grossRent": "1951.00", "federalRentAssistance": "1665.00", "householdIncome": "11462.00"}
    # A label printed twice with two different figures is ambiguous and not used.
    assert "tenantRent" not in _labelled_cert_amounts("Tenant Rent: $246 ... Tenant Rent: $304")
    kws = ("homeless", "no fixed address", "shelter", "unhoused")
    assert _indicated("homeless preference: are you homeless? yes no", kws) is False
    assert _indicated("are you currently homeless? ☐ yes ☒ no", kws) is False
    assert _indicated("are you currently homeless? [x] yes [ ] no", kws) is True
    assert _indicated("applicant is currently homeless and staying with a relative", kws) is True


def test_a_monthly_figure_on_the_certification_is_a_derivation_and_masked_ssns_keep_their_last_four():
    from app.services.completeness import _NOT_HOUSEHOLD_RE
    from app.services.validation import normalize_ssn
    assert _NOT_HOUSEHOLD_RE.search("A-1 $994.00 Monthly Income A-2 ")
    assert _NOT_HOUSEHOLD_RE.search("A-3 $288.20 30% of Monthly Adjusted")
    assert not _NOT_HOUSEHOLD_RE.search("1 | SS - Social security | ")
    assert normalize_ssn("***-***-2508") == "***-**-2508"
    assert normalize_ssn("***-**-2508") == "***-**-2508"
    assert normalize_ssn("XXX-XX-1234") == "***-**-1234"
    assert normalize_ssn("02/20/1959") is None


def test_a_relationship_is_read_against_the_members_age_and_a_placeholder_ssn_is_no_claim():
    """J-AFIA-07118: a TIC's code C beside a child born in 2022 came out
    as "Co-Head", and the questionnaire's instruction "if you do not have
    a SSN please enter 999-99-9999" was read as a second SSN for each
    member. A role only an adult can hold cannot belong to a minor; a
    number the SSA never issues is not evidence about a person."""
    from app.schemas.extraction import HouseholdDemographics, HouseholdMember
    from app.services.cartograph.adapter import relationship_out
    from app.services.identity import _issuable, resolve_identities
    from app.services.members import relationship_for_age
    assert relationship_for_age("Co-Head", "2022-06-23", "2026-11-01") == "Child"
    assert relationship_for_age("Co-Head", "1990-06-23", "2026-11-01") == "Co-Head"
    assert relationship_for_age("Head", "2010-01-01", "2026-11-01") == "Head"          # an emancipated minor can head
    assert relationship_for_age("Spouse", None, "2026-11-01") == "Spouse"              # no date, no judgement
    assert relationship_out("Co-Head", "2022-06-23", "2026-11-01") == "Minor Child"
    assert relationship_out("Co-Head", "1990-06-23", "2026-11-01") == "Co-Head"
    assert _issuable("999-99-9999") is False and _issuable("000-12-3456") is False
    assert _issuable("516-39-8061") is True and _issuable("***-**-8061") is True
    hh = HouseholdDemographics(houseHold=[HouseholdMember(FirstName="Harmony", LastName="Carrier", socialSecurityNumber="***-**-8061")])
    cert = "<table><tr><th>Last Name</th><th>First Name</th><th>SSN</th></tr><tr><td>Carrier</td><td>Harmony</td><td>8061</td></tr></table>"
    form = "Harmony Carrier Social Security Number (SSN): 516 - 39 - 8061 (If you do not have a SSN please enter 999-99-9999)"
    groups = [_group("Tenant Income Certification (TIC)", [5]), _group("Application / Housing Questionnaire", [15])]
    assert [f.code for f in resolve_identities(hh, groups, {5: cert, 15: form})] == []


def test_a_previous_certification_is_owed_only_where_the_packet_is_expected_to_carry_one():
    """Thirteen cases carried "previous certification missing" on packets
    that hold one certification plus continuation pages, the prior year
    living on the consumer's own record. The finding is off unless the
    packet is expected to carry the prior form."""
    hh = HouseholdDemographics(houseHold=[_member("T", "S", "1972-07-27", "***-**-5009")])
    ar = [_group("HUD 50059", [1, 2])]
    assert validate_cert_type_requirements("AR", ar, None, hh) == []
    codes = {f.code for f in validate_cert_type_requirements("AR", ar, None, hh, expect_previous_cert=True)}
    assert codes == {"AR_PREVIOUS_CERT_MISSING"}
    with_prev = ar + [_group("HUD 50059 (Previous)", [3, 4], category="ignore")]
    assert validate_cert_type_requirements("AR", with_prev, None, hh, expect_previous_cert=True) == []


def test_a_signed_certification_with_no_date_read_says_whether_the_date_is_unclear_or_absent():
    """"No signature date was found" covered a date written in handwriting
    the OCR could not resolve and a date slot left blank. The first is the
    engine's to re-read; the second is the property's undated form."""
    from app.schemas.extraction import DocumentInventoryEntry
    from app.services.signature_validator import _check_signature_date_agreement, _certification_date_state
    signed = CertificationInfo(isSigned="Yes")
    unclear = _check_signature_date_agreement(signed, "unclear")
    blank = _check_signature_date_agreement(signed, "blank")
    unknown = _check_signature_date_agreement(signed, None)
    assert [f.code for f in unclear + blank + unknown] == ["SIGNATURE_DATE_MISSING"] * 3
    assert unclear[0].label == "Signed certification, date unclear" and unclear[0].assignment == F.ASSIGN_INTERNAL
    assert blank[0].label == "Signed certification, undated" and blank[0].assignment == F.ASSIGN_CLIENT
    assert unknown[0].label == "Signed certification with no date"
    assert _check_signature_date_agreement(CertificationInfo(isSigned="Yes", signatureDate="2026-09-16"), "read") == []
    inv = DocumentInventory(documents=[DocumentInventoryEntry(documentType="HUD 9887", signatureDateState="blank"),
                                       DocumentInventoryEntry(documentType="HUD 50059", signatureDateState="unclear")])
    assert _certification_date_state(DocumentInventory(documents=[]), inv) == "unclear"
    assert _certification_date_state(None, DocumentInventory(documents=[])) is None


def test_gross_rent_is_delivered_with_the_definition_the_form_follows():
    """HUD's gross rent is contract rent + allowance; a tax credit form's is
    tenant rent + allowance. The same name on both, so the consumer gets
    the contract rent where the form prints one and which arithmetic the
    figures settle."""
    from app.services.cartograph.adapter import _gross_rent_basis
    hud = CertificationInfo(tenantRent="865.00", contractRent="1180.00", utilityAllowance="75.00", grossRent="1255.00")
    assert _gross_rent_basis(hud) == "contract_plus_allowance"
    tic = CertificationInfo(tenantRent="580.00", utilityAllowance="116.00", grossRent="696.00")
    assert _gross_rent_basis(tic) == "tenant_plus_allowance"
    assert _gross_rent_basis(CertificationInfo(tenantRent="580.00", utilityAllowance="116.00", grossRent="746.00")) is None
    assert _gross_rent_basis(CertificationInfo(grossRent="746.00")) is None
    from app.services.extractor import _labelled_cert_amounts
    read = _labelled_cert_amounts("29. Contract Rent $1,180.00  30. Utility Allowance $75.00  31. Gross Rent $1,255.00  110. Tenant Rent $865.00")
    assert read["contractRent"] == "1180.00" and read["grossRent"] == "1255.00" and read["tenantRent"] == "865.00"
