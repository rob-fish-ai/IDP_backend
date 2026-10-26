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
