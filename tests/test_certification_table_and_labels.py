"""From J-PACI-08412, 08417, 08421 and 08422 (2026-10-26).

08421/08422: the Nevada alternate certification page, labelled TIC by the
model, was relabelled a student certification on the student questions it
carries, and the packets went out without a certification form. 08412: the
TIC's Part III income rows, keyed by member number, were not returned by
the model's read, so the records carried the questionnaires' figures and no
certification amount. 08417: statements read with no owner were skipped by
the consumer as matching no member.
"""

from app.core.config import Settings
from app.schemas.extraction import DocumentGroup
from app.services import extractor as ex
from app.services import two_pass_classifier as cls

NEVADA_CERT = (
    "EXHIBIT NV-1c\nLOW INCOME HOUSING TAX CREDIT PROGRAM\nALTERNATE CERTIFICATION\n"
    "Property Name: Cheyenne Crossing BIN #: NV0851004 Unit #: 04-129\n"
    "Certification Effective Date: 2023/3/16 Move-In Date: 2020/3/16\n"
    "Household Income at move-in: $33,280.00 Gross Household Income: $58,280.00\n"
    "Max Income Limit: $49,080.00\n"
    "List all occupants of the unit and indicate if full-time student(s)\n"
    "Answer only if all members are full-time students:\n"
    "If yes, is the household comprised of a single parent with a child(ren)? Yes [ ] No [ ]\n"
    "If yes, has any household member previously been part of a foster care program? Yes [ ] No [ ]\n"
    "If yes, are the students enrolled in a job training program? Yes [ ] No [ ]\n"
)

STUDENT_FORM = (
    "STUDENT STATUS CERTIFICATION\n"
    "Will you be a student this calendar year? Yes [ ] No [X]\n"
    "full-time student single parent with a child foster care job training program\n"
)


def test_alternate_certification_keeps_its_label_despite_the_student_questions():
    assert cls._label_by_printed_title("Tenant Income Certification (TIC)", NEVADA_CERT) is None


def test_a_real_student_form_labelled_as_the_certification_is_still_relabelled():
    found = cls._label_by_printed_title("Tenant Income Certification (TIC)", STUDENT_FORM)
    assert found and found[0] == "Student Status Certification"


TIC_TABLES = (
    "<table><tr><th>HH Mbr #</th><th>Last Name</th><th>First Name & Middle Initial</th>"
    "<th>Relationship to Head of Household</th><th>Date of Birth (MM/DD/YYYY)</th></tr>"
    "<tr><td>1</td><td>Cuenca-Peralta</td><td>Juan</td><td>HEAD</td><td>06/24/1958</td></tr>"
    "<tr><td>2</td><td>Martinez-Esparza</td><td>Maria I</td><td>SPOUSE</td><td>02/01/1964</td></tr>"
    "<tr><td>3</td><td>Vasquez</td><td>Johanna Jazmin</td><td>ADULT</td><td>05/09/1998</td></tr></table>\n"
    "## PART III. GROSS ANNUAL INCOME (USE ANNUAL AMOUNTS)\n"
    "<table><tr><th>HH Mbr #</th><th>(A) Employment or Wages</th><th>(B) Social Security/Pensions</th>"
    "<th>(C) Public Assistance</th><th>(D) Other Income</th></tr>"
    "<tr><td>1</td><td>12,480.00</td><td>0.00</td><td>0.00</td><td>0.00</td></tr>"
    "<tr><td>3</td><td>13,244.08</td><td>0.00</td><td>0.00</td><td>0.00</td></tr></table>\n"
    "TOTAL INCOME (E): $25,724.08\n"
)


def _tic():
    return DocumentGroup(document_type="Tenant Income Certification (TIC)", category="include",
                         pages=[1], page_range="1", combined_text="--- Page 1 ---\n" + TIC_TABLES)


def test_tic_income_rows_are_read_by_member_number_with_the_names_from_part_ii():
    lines = ex._certification_table_lines([_tic()], {1: TIC_TABLES}, [])
    assert [(l["memberNumber"], l["memberName"], l["incomeType"], l["amount"], l["amountPeriod"]) for l in lines] == [
        ("1", "Juan Cuenca-Peralta", "Non-Federal Wage", "12480.00", "annual"),
        ("3", "Johanna Jazmin Vasquez", "Non-Federal Wage", "13244.08", "annual"),
    ]
    assert all(l["documentType"] == "Tenant Income Certification (TIC)" and l["page"] == 1 for l in lines)


def test_tic_rows_the_model_already_returned_are_not_added_twice():
    already = [{"memberName": "Juan Cuenca-Peralta", "memberNumber": "1", "amount": "12480.00", "page": 1}]
    lines = ex._certification_table_lines([_tic()], {1: TIC_TABLES}, already)
    assert [l["memberNumber"] for l in lines] == ["3"]


def test_a_questionnaire_table_is_not_read_as_the_certifications():
    q = DocumentGroup(document_type="Application / Housing Questionnaire", category="include",
                      pages=[4], page_range="4", combined_text=TIC_TABLES)
    assert ex._certification_table_lines([q], {4: TIC_TABLES}, []) == []


def test_unowned_asset_record_is_attributed_to_the_documents_person(monkeypatch):
    def fake_llm(system_prompt, prompt, settings):
        return {"assetInformation": [{
            "documentType": "Bank Statement — Savings", "assetOwner": None, "sourceName": "Truist",
            "accountType": "Savings", "accountNumber": "4411", "currentBalance": "0.00",
            "bankStatment": [{"statementDate": "2026-09-30", "balance": "0.00", "accountNumber": "4411"}],
            "sourcePages": [116], "evidence": {"currentBalance": "Ending Balance $0.00"},
        }]}

    monkeypatch.setattr(ex, "call_llm_json", fake_llm)
    g = DocumentGroup(document_type="Bank Statement", category="include", pages=[116], page_range="116",
                      combined_text="--- Page 116 ---\nTruist Savings 4411\nEnding Balance $0.00\n",
                      person_name="Sean Yoder")
    result = ex.extract_assets([g], Settings(), "MI", ["Sean Yoder", "Kaarina West"])
    assert [(a.assetOwner, a.sourceName, a.accountType) for a in result.assetInformation] == [("Sean Yoder", "Truist", "Savings")]
