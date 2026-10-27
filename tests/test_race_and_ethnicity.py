"""Race and ethnicity per member, for Cartograph's household member records
(2026-10-27). The consumer stores 23 race strings and 3 ethnicity strings;
the engine reads the household's own Race and Ethnic Data Forms from the
page image, falls back to the HUD 50059's Section C codes, and never
infers a value."""

from app.core.config import Settings
from types import SimpleNamespace

from app.schemas.extraction import DocumentGroup, HouseholdDemographics, HouseholdMember
from app.services import extractor as ex
from app.services import race_ethnicity as re_
from app.services.cartograph.adapter import build_household_members
from app.services.validation import validate_household


# --- the vocabulary ---------------------------------------------------------

def test_printed_labels_and_50059_codes_normalise_to_the_consumers_strings():
    assert re_.normalize_race("Black or African American") == "Black/African American"
    assert re_.normalize_race("Native Hawaiian or Other Pacific Islander") == "Native Hawaiian/Other Pacific Islander"
    assert re_.normalize_race("Guamanian or Chamorro") == "Native Hawaiian/Other Pacific Islander - Guamanian or Chamorro"
    assert re_.normalize_race("Asian - Chinese") == "Asian - Chinese"
    assert re_.normalize_race("Choose not to respond") == "Decline to Answer"
    assert re_.normalize_race("3") == "Black/African American"
    assert re_.normalize_race("W") == "White"
    assert re_.normalize_race("Martian") is None
    assert re_.normalize_races("15") == ["American Indian/Alaska Native", "White"]
    assert re_.normalize_races("Asian & White") == ["Asian", "White"]
    assert re_.normalize_races(["White", "Caucasian", ""]) == ["White"]
    assert re_.normalize_races([]) is None


def test_ethnicity_labels_and_codes():
    assert re_.normalize_ethnicity("Not-Hispanic or Latino") == "Not Hispanic or Latino"
    assert re_.normalize_ethnicity("Non-Hispanic") == "Not Hispanic or Latino"
    assert re_.normalize_ethnicity("Hispanic or Latino") == "Hispanic or Latino"
    assert re_.normalize_ethnicity("2") == "Not Hispanic or Latino"
    assert re_.normalize_ethnicity("1") == "Hispanic or Latino"
    assert re_.normalize_ethnicity("Decline to answer") == "Decline to Answer"
    assert re_.normalize_ethnicity("") is None


def test_several_ticks_fold_into_the_one_string_cartograph_stores():
    assert re_.race_out(["White"]) == "White"
    assert re_.race_out(["Asian", "Asian - Chinese"]) == "Asian - Chinese"
    assert re_.race_out(["Asian - Chinese", "Asian - Japanese"]) == "Asian"
    assert re_.race_out(["Asian", "White"]) == "Asian & White"
    assert re_.race_out(["White", "Black/African American"]) == "Black/African American & White"
    assert re_.race_out(["American Indian/Alaska Native", "Black/African American"]) == \
        "American Indian/Alaska Native & Black/African American"
    assert re_.race_out(["Asian", "Black/African American"]) == "Other Multi-Racial"
    assert re_.race_out(["White", "Asian", "Black/African American"]) == "Other Multi-Racial"
    assert re_.race_out(["Decline to Answer"]) == "Decline to Answer"
    assert re_.race_out(["White", "Decline to Answer"]) == "White"
    assert re_.race_out(None) is None
    assert re_.race_out([]) is None


def test_validation_normalises_what_the_model_returned_and_keeps_null_null():
    data = validate_household({"houseHold": [
        {"FirstName": "Evelyn", "LastName": "Coleman", "race": "3", "ethnicity": "2"},
        {"FirstName": "Ana", "LastName": "Ruiz", "race": None, "ethnicity": None},
    ]})
    assert data["houseHold"][0]["race"] == ["Black/African American"]
    assert data["houseHold"][0]["ethnicity"] == "Not Hispanic or Latino"
    assert data["houseHold"][1]["race"] is None and data["houseHold"][1]["ethnicity"] is None


def test_demographics_prompt_expands_the_50059_codes_and_forbids_inference():
    assert "field 39" in ex.DEMOGRAPHICS_SYSTEM_PROMPT and "3=Black/African American" in ex.DEMOGRAPHICS_SYSTEM_PROMPT
    assert "field 40" in ex.DEMOGRAPHICS_SYSTEM_PROMPT and "NEVER infer race" in ex.DEMOGRAPHICS_SYSTEM_PROMPT


# --- reading the forms ------------------------------------------------------

def _members():
    return HouseholdDemographics(houseHold=[
        HouseholdMember(FirstName="Ollie", LastName="West", head="H", DOB="1950-01-01"),
        HouseholdMember(FirstName="Juan", LastName="Cuenca-Peralta", DOB="1958-06-24"),
        HouseholdMember(FirstName="Maria", MiddleName="I", LastName="Martinez-Esparza", DOB="1964-02-01"),
    ])


def _form(pages):
    return DocumentGroup(document_type="HUD Race and Ethnic Data Form", category="compliance",
                         pages=list(pages), page_range=str(pages[0]), combined_text="x")


def _page_texts(*pages):
    return [{"page": p, "text": "ocr", "image_path": f"/img/{p}.png"} for p in pages]


def test_form_ticks_are_set_on_the_named_member_and_a_blank_member_line_means_the_head(monkeypatch):
    answers = {
        9: {"people": [{"name": "Ollie west", "isHeadOfHousehold": True, "ethnicity": "Not-Hispanic or Latino",
                        "races": ["Black or African American"], "declined": False}]},
        10: {"people": [{"name": None, "isHeadOfHousehold": True, "ethnicity": None, "races": [], "declined": False}]},
        15: {"people": [
            {"name": "Juan Cuenca Peralta", "isHeadOfHousehold": False, "ethnicity": "Hispanic or Latino",
             "races": ["White"], "declined": False},
            {"name": "Maria Martinez", "isHeadOfHousehold": False, "ethnicity": "Hispanic or Latino",
             "races": ["Asian", "White"], "declined": False, "gender": "F"},
        ]},
    }
    calls = []

    def fake_vision(system_prompt, user_prompt, image_paths, settings, **kw):
        page = int(image_paths[0].rsplit("/", 1)[1].split(".")[0])
        calls.append(page)
        return answers[page]

    monkeypatch.setattr("app.services.llm_service.call_llm_vision_json", fake_vision)
    hh = _members()
    notes = re_.read_race_and_ethnicity(hh, [_form([9, 10]), _form([15])], _page_texts(9, 10, 15), Settings())
    assert sorted(calls) == [9, 10, 15]
    assert notes == []
    ollie, juan, maria = hh.houseHold
    assert (ollie.race, ollie.ethnicity) == (["Black/African American"], "Not Hispanic or Latino")
    assert (juan.race, juan.ethnicity) == (["White"], "Hispanic or Latino")
    assert (maria.race, maria.ethnicity, maria.gender) == (["Asian", "White"], "Hispanic or Latino", "F")


def test_a_form_for_someone_not_on_the_roster_is_not_applied_and_is_reported(monkeypatch):
    monkeypatch.setattr("app.services.llm_service.call_llm_vision_json", lambda *a, **k: {"people": [
        {"name": "Sheila Scott", "isHeadOfHousehold": False, "ethnicity": None,
         "races": ["Black or African American"], "declined": False}]})
    hh = _members()
    notes = re_.read_race_and_ethnicity(hh, [_form([79])], _page_texts(79), Settings())
    assert [n["code"] for n in notes] == ["RACE_FORM_MEMBER_UNMATCHED"] and notes[0]["pages"] == [79]
    assert all(m.race is None and m.ethnicity is None for m in hh.houseHold)


def test_the_form_wins_over_the_50059_codes_and_a_blank_form_leaves_them(monkeypatch):
    monkeypatch.setattr("app.services.llm_service.call_llm_vision_json", lambda *a, **k: {"people": [
        {"name": "Ollie West", "isHeadOfHousehold": True, "ethnicity": "Hispanic or Latino",
         "races": ["Choose not to respond"], "declined": True},
        {"name": "Juan Cuenca-Peralta", "isHeadOfHousehold": False, "ethnicity": None, "races": [], "declined": False},
    ]})
    hh = _members()
    hh.houseHold[0].race, hh.houseHold[0].ethnicity = ["White"], "Not Hispanic or Latino"
    hh.houseHold[1].race, hh.houseHold[1].ethnicity = ["Asian"], "Not Hispanic or Latino"
    re_.read_race_and_ethnicity(hh, [_form([5])], _page_texts(5), Settings())
    assert (hh.houseHold[0].race, hh.houseHold[0].ethnicity) == (["Decline to Answer"], "Hispanic or Latino")
    assert (hh.houseHold[1].race, hh.houseHold[1].ethnicity) == (["Asian"], "Not Hispanic or Latino")


def test_two_forms_disagreeing_on_one_member_keep_the_first_and_report_it(monkeypatch):
    def fake_vision(system_prompt, user_prompt, image_paths, settings, **kw):
        page = int(image_paths[0].rsplit("/", 1)[1].split(".")[0])
        return {"people": [{"name": "Ollie West", "isHeadOfHousehold": True, "ethnicity": None,
                            "races": ["White"] if page == 3 else ["Asian"], "declined": False}]}

    monkeypatch.setattr("app.services.llm_service.call_llm_vision_json", fake_vision)
    hh = _members()
    notes = re_.read_race_and_ethnicity(hh, [_form([3]), _form([7])], _page_texts(3, 7), Settings())
    assert hh.houseHold[0].race == ["White"]
    assert [n["code"] for n in notes] == ["RACE_FORM_CONFLICT"] and notes[0]["pages"] == [3, 7]


def test_without_a_page_image_the_ocr_text_is_read_instead(monkeypatch):
    seen = {}

    def fake_text(system_prompt, user_prompt, settings):
        seen["prompt"] = user_prompt
        return {"people": [{"name": "Ollie West", "isHeadOfHousehold": True, "ethnicity": "Not Hispanic or Latino",
                            "races": ["White"], "declined": False}]}

    monkeypatch.setattr("app.services.llm_service.call_llm_json", fake_text)
    monkeypatch.setattr("app.services.llm_service.call_llm_vision_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("vision must not be called")))
    hh = _members()
    re_.read_race_and_ethnicity(hh, [_form([9])], [{"page": 9, "text": "<table>White X</table>", "image_path": None}], Settings())
    assert "White X" in seen["prompt"] and hh.houseHold[0].race == ["White"]


def test_a_packet_without_the_form_makes_no_call(monkeypatch):
    monkeypatch.setattr("app.services.llm_service.call_llm_vision_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call expected")))
    hh = _members()
    tic = DocumentGroup(document_type="Tenant Income Certification (TIC)", category="include", pages=[1], page_range="1", combined_text="x")
    assert re_.read_race_and_ethnicity(hh, [tic], _page_texts(1), Settings()) == []


# --- the payload ------------------------------------------------------------

def test_payload_members_carry_the_consumers_strings_and_null_where_nothing_was_stated():
    extraction = SimpleNamespace(certification_info=None, household_demographics=HouseholdDemographics(houseHold=[
        HouseholdMember(FirstName="Ollie", LastName="West", head="H", gender="M", disabled="N",
                        race=["Black/African American"], ethnicity="Not Hispanic or Latino"),
        HouseholdMember(FirstName="Maria", LastName="Martinez", gender="F", disabled="Decline to Answer",
                        race=["Asian", "White"], ethnicity="Hispanic or Latino"),
        HouseholdMember(FirstName="Unborn", LastName="Child", relationship="Unborn Child"),
    ]))
    members = build_household_members(extraction, [])
    assert [(m["race"], m["ethnicity"], m["gender"], m["disability"]) for m in members] == [
        ("Black/African American", "Not Hispanic or Latino", "Male", "No"),
        ("Asian & White", "Hispanic or Latino", "Female", "Decline to Answer"),
        (None, None, None, None),
    ]
    assert members[0]["is_disabled"] is False and members[1]["is_disabled"] is None
