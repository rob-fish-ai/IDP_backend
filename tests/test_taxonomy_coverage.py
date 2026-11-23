"""The taxonomy and the checklist row mapper against what the field sends.

The HUD 92006 lacked a title alias and a hint, so the classifier never
filed the form under its label and the row asking for it was reported
missing on every HUD scan. These tests pin the two sides of that gap: a
label with a form number must carry a printed title or a hint, and every
checklist row label the job store has ever received must map to a type,
be a question about a form, or be on the explicit list of rows the engine
does not answer.
"""
import json
import re
from pathlib import Path

import pytest

from app.services.cartograph.checklist import document_type_for, is_attribute_row, labels_satisfying, match_checklist
from app.services.cartograph.identity import concepts_of
from app.services.doc_taxonomy import TAXONOMY, known_labels
from app.services.two_pass_classifier import _label_by_printed_title

FIXTURE = Path(__file__).parent / "fixtures" / "checklist_row_labels.json"

# Rows that ask a question the packet's contents cannot answer by a form
# being present: these are left to the findings or to the reviewer. A new
# unanswered row is a decision, not a silent miss, so the list is explicit.
QUESTION_ROWS = {
    "Acknowledgement of Community Service form Completed for All Adult Members",
    "All Questions and Sections of the Questionnaire/Application  are fully completed",
    "Does the Household have Someone with Power of Attorney who Signs for them - is that relationship verified",
    "Household Member Listed on Applicaiton or Questionnaire",
    'If Certification is Past Due, Are "True and Accurate as of the Certification Date (or before) Included',
    "If the Household have Someone with Power of Attorney who Signs for them - is that relationship verified",
    "Is the Conversion to Cash Cost Verified on the Account ?",
    "Pharmacy Expenses Verified Showing Actual Amounts Paid",
    "Proof of Attempt to Obtain 3rd Party Verification Provided",
    "Proof of Community Service Completion Provided (If Applicable)",
    "Proof of Verification Receipt Received - Fax Header, Email, Date Stamped Received",
    "Verificatiom Form Received for Income",
}

# The rows Cartograph sends most, and the label each must map to.
EXPECTED = {
    "HUD 92006 Form Completed, Signed, and Dated": "HUD 92006",
    "EIV Existing Tenant Search Completed for all adult members": "EIV Existing Tenant Search",
    "HUD9887 Completed and Signed by All Adult Members": "HUD 9887",
    "HUD 9887a Signed and Dated by Adult Member": "HUD 9887-A",
    "HOTMA Note to File Present": "HOTMA Note to File",
    "Benefit or Budget Letter Received for Income": "SSA Benefit Letter",
    "Lease provided - signed and dated by all adult members": "Lease Agreement",
    "Lease Signed and Dated by All Adult Members": "Lease Agreement",
    "Asset under 50,000 Form Completed and Signed by All Adult Members": "Asset Self-Certification",
    "Asset Verification Form Completed and Signed": "Verification of Assets (VOA)",
    "Screening Report Completed": "Background Screening Report",
    "Third Party Verification Completed for Asset Source": "Verification of Assets (VOA)",
    "Third Party Verification of Income Provided or Proof of Exhaustion": "Verification of Income (VOI)",
    "EIV Acknowledgment Form completed": "EIV Acknowledgement Form",
    "Lease Term and Signature Pages ONLY": "Lease Agreement",
    "HUD Model Lease Provided (at least, Lease Term and Signature Pages)": "HUD Model Lease",
    "Lead Based Paint Disclosure (Built Pre-1978 Only) Included/Accurate": "Lead-Based Paint Certification",
    "Lead Based Paint Disclosure (Pre - 1978 Construction)": "Lead-Based Paint Certification",
    "Acknowledgement of HUD Brochures/Pamphlets-Included/Accurate": "Acknowledgement of Receipt",
    "Acknowledgement of HUD Handouts": "Acknowledgement of Receipt",
    "Disposal of Assets Form Completed and Signed": "Disposal of Assets Certification",
    "General Release Form Signed by All Adult Members": "Third-Party Authorization / Release",
    "Signed Copy of VAWA Lease Addendum Provided": "VAWA Lease Addendum",
    "Previous HUD 50059 Certification Form": "HUD 50059",
}


def test_every_received_row_label_is_answered_or_listed():
    labels = json.loads(FIXTURE.read_text())
    assert len(labels) >= 80
    unresolved = []
    for label in labels:
        row = {"label": label}
        if concepts_of(row) or is_attribute_row(row):
            continue
        if document_type_for(row)[0] is None:
            unresolved.append(label)
    assert set(unresolved) == QUESTION_ROWS, sorted(set(unresolved) ^ QUESTION_ROWS)


@pytest.mark.parametrize("label,expected", sorted(EXPECTED.items()))
def test_frequent_rows_map_to_the_right_type(label, expected):
    assert document_type_for({"label": label})[0] == expected


def test_row_families_accept_every_member():
    row = {"label": "Benefit or Budget Letter Received for Income"}
    assert "VA Benefit Letter" in labels_satisfying(row, document_type_for(row)[0])
    row = {"label": "Lease provided - signed and dated by all adult members"}
    assert labels_satisfying(row, document_type_for(row)[0]) == ("Lease Agreement", "HUD Model Lease")
    assert labels_satisfying({"label": "HUD 92006 Form"}, "HUD 92006") == ("HUD 92006",)


def test_numbered_labels_carry_a_printed_title_or_hint():
    for label, spec in TAXONOMY.items():
        if not re.search(r"\d{3,5}", label):
            continue
        titled = [a for a in spec["aliases"] if not re.search(r"\d{3,5}", a)]
        assert spec["hint"] or titled, f"{label}: nothing but its number for the classifier to go on"


def test_known_labels_include_the_new_forms():
    for l in ("HUD 92006", "EIV Existing Tenant Search", "HOTMA Note to File",
              "HUD 92458 Rent Schedule", "Authorization to Assist with Forms",
              "EIV Acknowledgement Form", "Certification Clarification Record", "HOME Lease Compliance Form",
              "Charges, Deposits and Fees Disclosure", "State Lease Disclosure", "Applicant Appeal Rights Notice"):
        assert l in known_labels()


def test_92006_printed_title_relabels_the_generic_form():
    page = ("Supplementary and Optional Contact Information for HUD-Assisted Housing Applicants "
            "OMB Control # 2502-0581 SUPPLEMENT TO APPLICATION FOR FEDERALLY ASSISTED HOUSING "
            "This form is to be provided to each applicant for federally assisted housing ... "
            "Form HUD-92006 (05/09)")
    assert _label_by_printed_title("Compliance — Other Signed HUD Form", page)[0] == "HUD 92006"
    tenant_match = "EIV Existing Tenant Search ... PIH/MF Tenant Match Results for the household"
    assert _label_by_printed_title("EIV Income Report", tenant_match)[0] == "EIV Existing Tenant Search"
    eiv_ack = ("EIV Acknowledgement Form Community: Catalpa Shade Household: Hurst Effective Date: 7.1.23 "
               "The above named property accesses the Enterprise Income Verification system ...")
    assert _label_by_printed_title("Compliance — Other Signed HUD Form", eiv_ack)[0] == "EIV Acknowledgement Form"
    for title in ("CERTIFICATION CLARIFICATION RECORD Property: Catalpa Shade Applicant/Resident: Jay Robbins",
                  "HOME LEASE COMPLIANCE FORM Property: Catalpa Shade Units are: Fixed - OR - Floating",
                  "Applicable Charges, Deposits and Fees Disclosure for HUD Units Cascade Management"):
        assert _label_by_printed_title("Compliance — Other Signed HUD Form", title)[0] != "Compliance — Other Signed HUD Form", title


def test_lease_family_reports_the_label_the_row_names():
    row = {"label": "Lease Term and Signature Pages ONLY"}
    assert labels_satisfying(row, document_type_for(row)[0]) == ("Lease Agreement", "HUD Model Lease")
    row = {"label": "HUD Model Lease Provided (at least, Lease Term and Signature Pages)"}
    assert labels_satisfying(row, document_type_for(row)[0]) == ("HUD Model Lease", "Lease Agreement")
    row = {"label": "EIV Acknowledgment Form completed"}
    assert labels_satisfying(row, document_type_for(row)[0])[0] == "EIV Acknowledgement Form"


def test_row_family_is_satisfied_by_a_sibling_and_a_screening_report_counts():
    from app.schemas.extraction import DocumentGroup
    groups = [
        DocumentGroup(document_type="VA Benefit Letter", category="include", person_name="Ana Ruiz", pages=[4], page_range="4", combined_text=""),
        DocumentGroup(document_type="Background Screening Report", category="ignore", person_name=None, pages=[9], page_range="9", combined_text=""),
        DocumentGroup(document_type="HUD Model Lease", category="include", person_name=None, pages=[12, 13], page_range="12-13", combined_text=""),
    ]
    ext = type("E", (), {"document_groups": groups, "finding_records": [], "document_inventory_hud": None,
                         "document_inventory_financial": None, "certification_info": None, "household_demographics": None})()
    rows = [
        {"finding_id": 1, "label": "Benefit or Budget Letter Received for Income"},
        {"finding_id": 2, "label": "Screening Report Completed"},
        {"finding_id": 3, "label": "Lease provided - signed and dated by all adult members"},
        {"finding_id": 4, "label": "HUD 92006 Form Completed, Signed, and Dated"},
    ]
    out = {m["finding_id"]: m for m in match_checklist(rows, ext)}
    assert out[1]["found"] is True and out[1]["note"].startswith("VA Benefit Letter present, page 4")
    assert out[2]["found"] is True and out[2]["pages"] == [9]
    assert out[3]["found"] is True and out[3]["note"].startswith("HUD Model Lease present, pages 12-13")
    assert out[4]["found"] is False and out[4]["note"] == "No HUD 92006 in the packet."
