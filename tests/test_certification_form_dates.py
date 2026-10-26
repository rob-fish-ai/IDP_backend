"""Dates and dates of birth are the certification form's own.

From J-PACI-08356, a Nevada alternate TIC: the form prints its dates as
"2026/11/1" and the head of household's date of birth as "1984/5/8", and
the household's handwritten questionnaire reads "05/08/1988" for the same
person. The audit delivered the questionnaire's date of birth, dropped the
move-in date as "not printed", and reported the certification undated
although "Date: 10/23/26" is printed beside both signatures.
"""

from app.core.config import Settings
from app.schemas.extraction import DocumentGroup
from app.services import extractor as ex
from app.services import pipeline

TIC = (
    "--- Page 2 ---\n"
    "LOW INCOME HOUSING TAX CREDIT PROGRAM\nALTERNATE CERTIFICATION\n"
    "Certification Effective Date: 2026/11/1\nMove-In Date: 2024/11/1\n"
    "Household Member Name | Birth of Date | Full-time Student\n"
    "Hernandez Arevalo, Dulce A | 1984/5/8 | No\n"
    "Morales, Esteban | 2008/4/29 | No\n"
    "Signature: [signed] Date: 10/23/26\n"
)

QUESTIONNAIRE = (
    "--- Page 3 ---\n"
    "Sworn Statement of Anticipated Income and Assets\n"
    "Name Dace Hernandez\nDate of Birth 05/08/1988 Social Security # 6335\n"
)


def _members():
    return [
        {"FirstName": "Dulce", "LastName": "Hernandez Arevalo", "DOB": "1988-05-08",
         "socialSecurityNumber": "***-**-6335", "relationship": "Head", "head": "H"},
        {"FirstName": "Esteban", "LastName": "Morales", "DOB": "2008-04-29",
         "socialSecurityNumber": "***-**-3997", "relationship": "Son", "head": None},
    ]


def test_year_first_dates_without_zero_padding_count_as_printed():
    assert ex._date_on_text("2026-11-01", "Certification Effective Date: 2026/11/1")
    assert ex._date_on_text("2024-11-01", "Move-In Date: 2024/11/1")
    assert ex._date_on_text("1984-05-08", "Hernandez Arevalo, Dulce A | 1984/5/8")
    assert not ex._date_on_text("1988-05-08", TIC)


def test_dob_the_form_does_not_print_is_replaced_by_the_forms_row(monkeypatch):
    asked: list = []

    def fake_retry(texts, gaps, shortfall, cert_type, settings):
        asked.extend(gaps)
        return [{"FirstName": "Dulce", "LastName": "Hernandez Arevalo", "DOB": "1984-05-08"}]

    monkeypatch.setattr(ex, "retry_member_fields", fake_retry)
    members = _members()
    replaced = ex.dobs_from_certification_form(members, [TIC], TIC, "AR", Settings())
    assert asked == [{"name": "dulce hernandez arevalo", "missing": ["DOB"]}]   # Esteban's is printed
    assert members[0]["DOB"] == "1984-05-08"
    assert members[1]["DOB"] == "2008-04-29"
    assert replaced == ["Dulce Hernandez Arevalo: 1988-05-08 -> 1984-05-08"]


def test_dob_is_kept_when_the_form_prints_no_date_for_the_row(monkeypatch):
    # The re-read answers with a date the form does not print either (or
    # nothing): the first read stays, nothing is blanked.
    monkeypatch.setattr(ex, "retry_member_fields",
                        lambda *a: [{"FirstName": "Dulce", "LastName": "Hernandez Arevalo", "DOB": "1990-01-01"}])
    members = _members()
    assert ex.dobs_from_certification_form(members, [TIC], TIC, "AR", Settings()) == []
    assert members[0]["DOB"] == "1988-05-08"


def test_extract_demographics_takes_the_dob_from_the_certification_form(monkeypatch):
    calls: list[str] = []

    def fake_llm(system_prompt, prompt, settings):
        calls.append(system_prompt[:40])
        if system_prompt is ex.DEMOGRAPHICS_SYSTEM_PROMPT:
            return {"houseHold": _members()}
        return {"houseHold": [{"FirstName": "Dulce", "LastName": "Hernandez Arevalo", "DOB": "1984-05-08"}]}

    monkeypatch.setattr(ex, "call_llm_json", fake_llm)
    groups = [
        DocumentGroup(document_type="Tenant Income Certification (TIC)", category="include",
                      pages=[2], page_range="2", combined_text=TIC),
        DocumentGroup(document_type="Application / Housing Questionnaire", category="include",
                      pages=[3], page_range="3", combined_text=QUESTIONNAIRE),
    ]
    result = ex.extract_demographics(groups, Settings(), "AR")
    assert [m.DOB for m in result.houseHold] == ["1984-05-08", "2008-04-29"]
    assert len(calls) == 2


def test_recovered_form_date_is_kept_only_when_the_transcript_prints_it():
    transcript = TIC.lower()
    assert pipeline._recovered_cert_value("signatureDate", "10/23/26", transcript) == "2026-10-23"
    assert pipeline._recovered_cert_value("moveInDate", "2024/11/1", transcript) == "2024-11-01"
    assert pipeline._recovered_cert_value("effectiveDate", "2026-11-01", transcript) == "2026-11-01"
    assert pipeline._recovered_cert_value("signatureDate", "2026-10-24", transcript) is None
    assert pipeline._recovered_cert_value("tenantRent", "$1,314.00", transcript) == "1314.00"
    assert pipeline._recovered_cert_value("unitNumber", " 11-281 ", transcript) == "11-281"
    assert pipeline._recovered_cert_value("signatureDate", None, transcript) is None


# --- identity resolution: the form's date cell must be read as a date -------
# J-PACI-08384: the TIC's boxed "MM DD YYYY" cell comes through OCR as
# "08 06 1988". Unread as a date, the form made no claim, the questionnaire's
# handwritten "8/10/88" was the top claim, and the delivered date of birth
# changed from the form's to the questionnaire's.

def _tic_group(pages, text):
    return DocumentGroup(document_type="Tenant Income Certification (TIC)", category="include",
                         pages=pages, page_range="-".join(str(p) for p in pages), combined_text=text)


def _questionnaire_group(page, text, person):
    return DocumentGroup(document_type="Application / Housing Questionnaire", category="include",
                         pages=[page], page_range=str(page), combined_text=text, person_name=person)


def test_identity_reads_the_forms_boxed_date_cell_and_keeps_it_over_the_questionnaire():
    from app.schemas.extraction import HouseholdDemographics, HouseholdMember
    from app.services.identity import resolve_identities
    tic = ("<table><tr><th>HH Mbr #</th><th>Last Name</th><th>First Name & Middle Initial</th>"
           "<th>Relationship to Head of Household</th><th>Date of Birth (MM DD YYYY)</th></tr>"
           "<tr><td>1</td><td>Johnson</td><td>Samantha Caroline</td><td>HEAD</td><td>08 06 1988</td></tr></table>")
    questionnaire = "Name Samantha Johnson\nDate of Birth 8/10/88 Social Security # 0880 Marital Status Single"
    hh = HouseholdDemographics(houseHold=[HouseholdMember(FirstName="Samantha", LastName="Johnson", DOB="1988-08-06", relationship="Head")])
    groups = [_tic_group([1], tic), _questionnaire_group(4, questionnaire, "Samantha Johnson")]
    findings = resolve_identities(hh, groups, {1: tic, 4: questionnaire})
    assert hh.houseHold[0].DOB == "1988-08-06"
    assert [f.code for f in findings] == ["MEMBER_IDENTITY_CONFLICT"]


def test_identity_reads_a_year_first_date_cell_with_single_digit_parts():
    from app.schemas.extraction import HouseholdDemographics, HouseholdMember
    from app.services.identity import resolve_identities
    tic = ("<table><tr><th>Household Member Name</th><th>Birth of Date</th><th>Full-time Student Y/N</th></tr>"
           "<tr><td>Hernandez Arevalo, Dulce A</td><td>1984/5/8</td><td>No</td></tr></table>")
    questionnaire = "Name Dulce Hernandez\nDate of Birth 05/08/1988 Social Security # 6335"
    hh = HouseholdDemographics(houseHold=[HouseholdMember(FirstName="Dulce", LastName="Hernandez Arevalo", DOB="1988-05-08", relationship="Head")])
    groups = [_tic_group([2], tic), _questionnaire_group(3, questionnaire, "Dulce Hernandez")]
    resolve_identities(hh, groups, {2: tic, 3: questionnaire})
    assert hh.houseHold[0].DOB == "1984-05-08"
