"""Identity proofs per member for the DOB and SSN checklist rows."""
from types import SimpleNamespace

from app.schemas.extraction import CertificationInfo, DocumentGroup


def g(label, pages, person=None, text="x", category="include"):
    return DocumentGroup(document_type=label, category=category, pages=pages, page_range=str(pages[0]),
                         combined_text=text, person_name=person)


MEMBERS = [
    {"ref": "m1", "first_name": "Clerida", "last_name": "Jaramillo", "date_of_birth": "1961-03-14", "ssn_last4": "3849"},
    {"ref": "m2", "first_name": "Wilbur", "last_name": "Perez Matos", "date_of_birth": "1990-07-02", "ssn_last4": "1122"},
    {"ref": "m3", "first_name": "Ana", "last_name": "Jaramillo", "date_of_birth": "2015-01-09", "ssn_last4": None},
]
SPANS = [{"job_document_id": 501, "first_page": 1, "last_page": 30},
         {"job_document_id": 502, "first_page": 31, "last_page": 60}]


def _extraction():
    return SimpleNamespace(
        document_groups=[
            g("Tenant Income Certification (TIC)", [1, 2]),
            g("Application / Housing Questionnaire", [3, 4, 5]),
            g("Birth Certificate", [12], person="Clerida Jaramillo", text="CERTIFICATE OF LIVE BIRTH Clerida Jaramillo 03/14/1961"),
            g("Social Security Card", [38], person=None, text="818-47-3849 THIS NUMBER HAS BEEN ESTABLISHED FOR CLERIDA JARAMILLO"),
            g("Driver License / State ID", [40], person="Wilbur Perez Matos", text="DRIVER LICENSE PEREZ MATOS WILBUR DOB 07/02/1990"),
            g("Social Security Card", [41], person="Rosa Delgado", text="SOCIAL SECURITY 123-45-9999 ROSA DELGADO"),
        ],
        document_inventory_hud=None, document_inventory_financial=None,
        certification_info=CertificationInfo(certificationType="AR"),
    )


def test_the_new_identity_labels_are_known_and_routed_to_the_household_extractor():
    from app.services.doc_taxonomy import ROUTE_DEMO, labels_for_route, prompt_type_list
    from app.services.cartograph.identity import METHOD_OF_LABEL
    for label in METHOD_OF_LABEL:
        assert label in labels_for_route(ROUTE_DEMO), label
        assert label in prompt_type_list()


def test_hud_rows_want_the_card_and_the_certificate_and_say_which_page_and_document():
    from app.services.cartograph.checklist import match_checklist
    rows = [
        {"finding_id": 1, "label": "Date of Birth Verified", "subject_id": 8382, "subject_label": "CLERIDA JARAMILLO", "funding_program": "Section 8"},
        {"finding_id": 2, "label": "SSN Verified", "subject_id": 8382, "subject_label": "CLERIDA JARAMILLO", "funding_program": "Section 8"},
        {"finding_id": 3, "label": "Date of Birth Verified", "subject_id": 8383, "subject_label": "WILBUR PEREZ MATOS", "funding_program": "Section 8"},
        {"finding_id": 4, "label": "SSN Verified", "subject_id": 8383, "subject_label": "WILBUR PEREZ MATOS", "funding_program": "Section 8"},
        {"finding_id": 5, "label": "HUD 50059 Completed and Accurate"},
    ]
    out = {m["finding_id"]: m for m in match_checklist(rows, _extraction(), members=MEMBERS, document_spans=SPANS, program="Section 8")}
    dob = out[1]
    assert dob["found"] and dob["accepted"] and dob["document_type"] == "birth_certificate"
    assert dob["pages"] == [12] and dob["document_id"] == 501 and dob["member_ref"] == "m1" and dob["household_member_id"] == 8382
    assert "matched by name and number" in dob["note"] and dob["confidence"] == 0.95
    # The card names nobody the classifier could read, but prints her number.
    ssn = out[2]
    assert ssn["found"] and ssn["document_type"] == "ssn_card" and ssn["pages"] == [38] and ssn["document_id"] == 502
    assert "matched by number" in ssn["note"]
    # A license is in the file but is not a HUD proof of DOB: the row is
    # not met, and the license is reported rather than hidden.
    lic = out[3]
    assert not lic["found"] and not lic["accepted"] and lic["document_found"]
    assert lic["document_type"] == "drivers_license" and lic["pages"] == [40]
    assert "not an accepted proof" in lic["note"]
    # No SSN proof for Wilbur; the stray card on page 41 is pointed out.
    assert not out[4]["found"] and out[4]["pages"] == [] and "page 41 shows a Social Security card that could not be matched" in out[4]["note"]
    assert out[4]["confidence"] == 0.8 and out[4]["note_source"] == "scan"
    # Ordinary rows are untouched by the identity logic.
    assert out[5]["found"] is False and "HUD 50059" in out[5]["note"]


def test_a_tax_credit_file_accepts_the_application_and_a_combined_row_needs_both_concepts():
    from app.services.cartograph.checklist import match_checklist
    rows = [
        {"finding_id": 10, "label": "Date of Birth and SSN Provided", "subject_id": 1, "subject_label": "Wilbur Perez Matos", "funding_program": "LIHTC "},
        {"finding_id": 11, "label": "Date of Birth and SSN Provided", "subject_id": 3, "subject_label": "Ana Jaramillo", "funding_program": "LIHTC "},
    ]
    out = {m["finding_id"]: m for m in match_checklist(rows, _extraction(), members=MEMBERS, document_spans=SPANS, program="LIHTC")}
    w = out[10]
    assert w["found"] and w["accepted"] and w["document_type"] is None
    assert [(p["concept"], p["document_type"], p["pages"]) for p in w["proofs"]] == \
        [("dob", "drivers_license", [40]), ("ssn", "self_declaration", [3, 4, 5])]
    assert w["pages"] == [3, 4, 5, 40] and w["confidence"] == 0.6
    # Ana has a declared DOB but no SSN anywhere: the combined row is not met.
    a = out[11]
    assert not a["found"] and [p["concept"] for p in a["proofs"]] == ["dob"] and "SSN: no accepted proof" in a["note"]


def test_the_request_lists_override_the_defaults_and_odd_spellings_are_understood():
    from app.services.cartograph.identity import accepted_methods, concepts_of, normalise_method
    assert accepted_methods(None, "HOME")["ssn"] == ("ssn_card",)
    assert "self_declaration" in accepted_methods(None, "LIHTC")["ssn"]
    sent = {"dob": ["Driver's License", "passport"], "ssn": ["SSN card", "bogus"]}
    assert accepted_methods(sent, "Section 8") == {"dob": ("drivers_license", "passport"), "ssn": ("ssn_card",)}
    assert normalise_method("Birth-Certificate") == "birth_certificate" and normalise_method("state id") == "state_id"
    assert concepts_of({"label": "Date of Birth and SSN Provided"}) == {"dob", "ssn"}
    assert concepts_of({"label": "SSN Provided"}) == {"ssn"} and concepts_of({"label": "DOB Verified"}) == {"dob"}
    # A benefit letter row mentions Social Security without being about identity.
    assert concepts_of({"label": "Social Security Benefit Letter on file"}) == frozenset()
    assert concepts_of({"label": "HUD 50059 Completed and Accurate"}) == frozenset()


def test_the_request_model_reads_verification_methods_and_the_store_keeps_manual_corrections(tmp_path):
    from app.routers.integration import AuditRequest
    from app.services.audit.job_store import JobStore
    req = AuditRequest(case_ref="J-1", documents=[{"url": "https://x/y.pdf"}],
                       verification_methods={"dob": ["passport"], "ssn": ["ssn_card"]})
    assert req.verification_methods == {"dob": ["passport"], "ssn": ["ssn_card"]}
    store = JobStore(tmp_path / "jobs.db")
    counts = store.record_findings_feedback("J-1", {
        "scan_id": 77,
        "verdicts": [{"finding_key": "INC_X:1", "verdict": "valid"}],
        "paystubs": [{"id": 900, "manually_corrected": True, "field": "gross_pay"},
                     {"id": 901, "manually_corrected": "false"},
                     {"manually_corrected": True}],
        "bank_statements": [{"record_id": "bs-5", "manually_corrected": False}],
    })
    assert counts == {"verdicts": 1, "manual_findings": 0, "corrections": 3}
    with store._connect() as conn:
        rows = conn.execute("SELECT record_type, record_id, manually_corrected FROM record_corrections ORDER BY record_id").fetchall()
    assert [tuple(r) for r in rows] == [("paystub", "900", 1), ("paystub", "901", 0), ("bank_statement", "bs-5", 0)]


def test_a_row_naming_no_member_needs_every_member_and_names_who_lacks_proof():
    from app.services.cartograph.checklist import match_checklist
    rows = [{"finding_id": 20, "label": "Copy of SSN Card or Other Approved SSN Verification"}]
    out = match_checklist(rows, _extraction(), members=MEMBERS, document_spans=SPANS, program="Section 8")[0]
    # Clerida's card is on page 38; Wilbur and Ana have none.
    assert not out["found"] and not out["document_found"] and out["member_ref"] is None
    assert [(p["member_ref"], p["document_type"]) for p in out["proofs"]] == [("m1", "ssn_card")]
    assert "Wilbur Perez Matos: SSN: no accepted proof" in out["note"] and "Ana Jaramillo: SSN: no accepted proof" in out["note"]
    # With one member the row is judged for that member alone and met.
    out = match_checklist(rows, _extraction(), members=MEMBERS[:1], document_spans=SPANS, program="Section 8")[0]
    assert out["found"] and out["document_type"] == "ssn_card" and out["pages"] == [38]


def test_self_declaration_points_at_the_application_not_another_affidavit():
    from app.services.cartograph.identity import _declaration_pages
    ext = _extraction()
    assert _declaration_pages(ext) == [3, 4, 5]
    ext.document_groups = [g("Tenant Income Certification (TIC)", [1, 2]), g("Asset Self-Certification", [22])]
    assert _declaration_pages(ext) == [1, 2]


def test_rows_about_age_or_a_50059_packet_are_read_as_the_audit_reads_them():
    from app.services.cartograph.checklist import normalise_rows
    from app.services.cartograph.identity import accepted_methods, concepts_of
    assert concepts_of({"label": "Student Status Certification for Members Age 18 and Over"}) == frozenset()
    assert concepts_of({"label": "Copy of Passport or Birth Certificate Provided"}) == {"dob"}
    assert concepts_of({"label": "Proof of Age Provided"}) == {"dob"}
    # A tax-credit request whose packet certifies on a HUD 50059 takes HUD's proofs.
    assert accepted_methods(None, "LIHTC", ["HUD 50059", "Paystub"])["ssn"] == ("ssn_card",)
    assert "self_declaration" in accepted_methods(None, "LIHTC", ["HUD 50059 (Previous)"])["ssn"]
    rows = normalise_rows({"checklist_rows": [{"finding_id": 1, "label": "SSN Provided", "funding_program": "LIHTC "}]})
    assert rows[0]["funding_program"] == "LIHTC "
