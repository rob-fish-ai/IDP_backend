"""From J-PACI-08404, 08406 and 08407 (Nevada LIHTC packets, 2026-10-26).

08404 went out with no income at all: the questionnaire prints "$ 30 000"
and the employer form "7.200", neither a number token nor a digit stream
with cents, so both were dropped as not printed; the employer form's
employee-name line was OCR garbage, so the record had no name and was
dropped. 08406 sent unit "147" for a form printing "06-147". 08407, a
self-certification whose income line reads "$0.00", seeded a self-declared
record with the move-in year's 18,635.21 because "Gross Household Income"
was not a label the engine read.
"""

from types import SimpleNamespace

from app.core.config import Settings
from app.schemas.extraction import DocumentGroup
from app.services import extractor as ex
from app.services import pipeline


# --- amounts printed with a space or period as the thousands separator ---

def test_whole_amounts_printed_with_space_or_period_separators_count_as_printed():
    assert ex._amount_on_pages("30000.00", {4: "\\$ 30 000 a. Annual employment income"}, [4])
    assert ex._amount_on_pages("7200.00", {11: "Year-to-date gross earnings S 7.200 1/1/20 through 3/27/20"}, [11])
    assert ex._amount_on_pages("7200.00", {11: "YTD $7,200"}, [11])
    assert ex._amount_on_pages("600.00", {11: "Current Gross Wages/Salary S 600 (check one below)"}, [11])


def test_a_dollars_and_cents_figure_is_not_a_thousands_figure():
    assert not ex._amount_on_pages("1200.00", {1: "Fee $12.00 per month"}, [1])
    assert not ex._amount_on_pages("30000.00", {1: "Rent $300.00"}, [1])
    assert not ex._amount_on_pages("30000.00", {1: "nothing here 31 000"}, [1])


# --- a verification record with no readable name is the document's person ---

VOI_TEXT = (
    "--- Page 10 ---\nTHIS SECTION TO BE COMPLETED BY EMPLOYER\nEmployee Name 2- 2- 2- 2- 2- 2-\n"
    "Current Gross Wages/Salary S 600 (check one below) Weekly\n"
    "Year-to-date gross earnings S 7.200 1/1/20 through 3/27/20\n"
)


def test_unnamed_verification_record_is_attributed_to_the_documents_person(monkeypatch):
    def fake_llm(system_prompt, prompt, settings):
        return {"sourceIncome": {"verificationIncome": [{
            "memberName": None, "sourceName": None, "incomeType": "Non-Federal Wage",
            "rateOfPay": "600.00", "payFrequency": "Weekly", "ytdAmount": "7200.00",
            "sourcePages": [10], "evidence": {"rateOfPay": "S 600", "ytdAmount": "S 7.200"},
        }], "payStub": []}}

    monkeypatch.setattr(ex, "call_llm_json", fake_llm)
    monkeypatch.setattr(ex, "_extract_declared_income", lambda *a, **k: [])
    g = DocumentGroup(document_type="Verification of Income (VOI)", category="include", pages=[10],
                      page_range="10", combined_text=VOI_TEXT, person_name="Mario Lopez Rosales")
    result = ex.extract_income([g], Settings(), "AR", ["Mario Lopez-Rosales"])
    vis = result.sourceIncome.verificationIncome
    assert [(v.memberName, v.rateOfPay, v.ytdAmount) for v in vis] == [("Mario Lopez Rosales", "600.00", "7200.00")]


# --- the certification's income line, not the move-in year's ---

NEVADA_TIC = (
    "LOW INCOME HOUSING TAX CREDIT PROGRAM ALTERNATE CERTIFICATION\n"
    "Certification Effective Date: 2021/8/21\nMove-In Date: 2018/8/21\nTenant Paid Rent: $0.00\n"
    "Household Income at move-in: $18,635.21\nUtility Allowance: $90.00\n"
    "Gross Household Income: $0.00\nMax Income Limit: $37,800.00\nTotal Gross Rent: $90.00\n"
)


def test_gross_household_income_is_the_labelled_income_and_the_move_in_figure_is_not():
    assert ex._labelled_cert_amounts(NEVADA_TIC)["householdIncome"] == "0.00"
    move_in_only = "Household Income at move-in: $18,635.21\nUtility Allowance: $90.00\n"
    assert "householdIncome" not in ex._labelled_cert_amounts(move_in_only)
    assert ex._labelled_cert_amounts("Part III Total Annual Income $24,960.00")["householdIncome"] == "24960.00"


# --- the self-certified income's type ---

def _declared(*rows):
    return SimpleNamespace(declared=[SimpleNamespace(incomeType=t, amount=a) for t, a in rows])


def test_self_certified_income_takes_the_one_kind_the_household_declared():
    assert pipeline._self_declared_income_type(_declared(("Non-Federal Wage", "20400.00"))) == "Non-Federal Wage"
    assert pipeline._self_declared_income_type(_declared(("Zero Income", "0.00"))) == "Self-Declared"
    assert pipeline._self_declared_income_type(_declared(("Non-Federal Wage", "20400.00"), ("Social Security", "9000.00"))) == "Self-Declared"
    assert pipeline._self_declared_income_type(SimpleNamespace(declared=[])) == "Self-Declared"


# --- the unit number as printed ---

def test_unit_number_prompts_keep_the_building_prefix():
    for prompt in (ex.CERT_INFO_SYSTEM_PROMPT, ex._CERT_INFO_RETRY_PROMPT):
        assert "Strip any building prefix" not in prompt
        assert "exactly as the form prints it" in prompt
