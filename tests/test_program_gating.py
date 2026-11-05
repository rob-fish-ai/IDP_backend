"""From the 47-row corrections export of 2026-11-05: HUD rules fired on
tax-credit properties (SSN missing, the HUD forms acknowledgement, EIV
expectations), and "a signature could not be verified" notes fired on
signed forms. One test now says whether a packet is a HUD file, every HUD
rule uses it, and only the certification form keeps a signature finding."""

from types import SimpleNamespace

from app.schemas.extraction import (
    CertificationInfo, ClassificationResult, DocumentGroup, DocumentInventory,
    DocumentInventoryEntry, HouseholdDemographics, HouseholdMember, PageClassification,
)
from app.services.findings import text_of
from app.services.program import is_hud_property
from app.services.signature_validator import validate_signatures
from app.services.special_scenarios import check_special_scenarios
from app.services.two_pass_classifier import demote_hud_only_nearest_matches


def test_the_program_field_and_a_current_50059_say_hud_and_nothing_else_does():
    assert is_hud_property("HUD Section 8")
    assert is_hud_property("HUD 202 PRAC")
    assert is_hud_property("USDA Rural Development")
    assert is_hud_property("RAD PBV")
    assert not is_hud_property("LIHTC")
    assert not is_hud_property("Section 42")                       # the tax credit itself
    assert not is_hud_property("CT 8-30G Program")
    assert not is_hud_property("Colorado HFA")                     # "rad" inside a word
    assert not is_hud_property(None)
    assert is_hud_property("LIHTC", ["Paystub", "HUD 50059"])      # mixed-financed: certifies on the 50059
    assert not is_hud_property("LIHTC", ["HUD 50059 (Previous)", "Tenant Income Certification (TIC)"])


def _household():
    return HouseholdDemographics(houseHold=[
        HouseholdMember(FirstName="Ana", LastName="Ruiz", DOB="1988-03-01", socialSecurityNumber=None),
    ])


def test_a_missing_ssn_is_a_finding_on_a_hud_file_and_not_on_a_tax_credit_one():
    tic = DocumentGroup(document_type="Tenant Income Certification (TIC)", category="include", pages=[1], page_range="1", combined_text="")
    cert = CertificationInfo(effectiveDate="2026-01-01")
    lihtc = check_special_scenarios(_household(), None, cert, [tic], None, SimpleNamespace(funding_program="LIHTC"))
    assert not any(f.code == "MEMBER_SSN_MISSING" for f in lihtc)
    hud = check_special_scenarios(_household(), None, cert, [tic], None, SimpleNamespace(funding_program="HUD Section 8"))
    assert [f.code for f in hud] == ["MEMBER_SSN_MISSING"]


def test_supporting_forms_carry_no_signature_note_and_hud_forms_are_only_owed_on_a_hud_file():
    unsigned = [DocumentInventoryEntry(documentType=t, isSigned="No") for t in (
        "Acknowledgement of Receipt", "HUD 9887", "HUD 9887-A", "Tenant Release and Consent Form",
        "Student Status Certification", "HUD 92006", "HUD Model Lease", "Lead-Based Paint Certification",
    )]
    hh = HouseholdDemographics(houseHold=[HouseholdMember(FirstName="Ana", LastName="Ruiz", DOB="1988-03-01")])
    cert = CertificationInfo(effectiveDate="2026-01-01", isSigned=None)
    out = validate_signatures(DocumentInventory(documents=unsigned), DocumentInventory(documents=[]), hh, cert, [],
                              SimpleNamespace(funding_program="HUD Section 8"))
    texts = [text_of(f) for f in out]
    assert not any("could not be verified" in t or "must be signed" in t for t in texts)
    # The two per-member HUD declarations are owed on a HUD file...
    assert sorted(t.split(":")[1].split("—")[0].strip() for t in texts if t.startswith("Missing required")) == \
        ["Citizenship Declaration (Section 214)", "Race and Ethnic Data Form"]
    # ...and not on a tax-credit one.
    lihtc = validate_signatures(DocumentInventory(documents=unsigned), DocumentInventory(documents=[]), hh, cert, [],
                                SimpleNamespace(funding_program="LIHTC"))
    assert [text_of(f) for f in lihtc if "Missing required" in text_of(f)] == []
    # The certification form keeps its signature finding.
    tic_unsigned = DocumentInventory(documents=[DocumentInventoryEntry(documentType="Tenant Income Certification (TIC)", isSigned="No")])
    out = validate_signatures(DocumentInventory(documents=[]), tic_unsigned, hh, cert, [], SimpleNamespace(funding_program="LIHTC"))
    assert any("TIC must be signed" in text_of(f) for f in out)


def test_a_nearest_match_onto_a_hud_only_type_is_left_unclassified_on_a_tax_credit_packet():
    pages = [
        PageClassification(page=7, document_type="EIV Income Report", category="include", fit="nearest",
                           observed_title="Verification of Social Services"),
        PageClassification(page=8, document_type="EIV Income Report", category="include", fit="exact"),
        PageClassification(page=9, document_type="Verification of Assets (VOA)", category="include", fit="nearest",
                           observed_title="Verification of Rental History"),
    ]
    groups = [
        DocumentGroup(document_type="EIV Income Report", category="include", pages=[7], page_range="7", combined_text=""),
        DocumentGroup(document_type="EIV Income Report", category="include", pages=[8], page_range="8", combined_text=""),
        DocumentGroup(document_type="Verification of Assets (VOA)", category="include", pages=[9], page_range="9", combined_text=""),
    ]
    assert demote_hud_only_nearest_matches(ClassificationResult(pages=pages), groups) == [7]
    assert (groups[0].document_type, groups[0].category) == ("Unknown", "ignore")
    assert pages[0].fit == "none" and "'EIV Income Report' was the nearest type" in pages[0].notes
    assert groups[1].document_type == "EIV Income Report"            # an exact fit stands, even on a LIHTC file
    assert groups[2].document_type == "Verification of Assets (VOA)"  # not a HUD-only type
