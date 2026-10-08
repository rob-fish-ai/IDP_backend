"""Operational rules on the Cartograph path: transient failures are retried,
packets are kept and pruned."""
import os
import time
from pathlib import Path

import pytest

from app.core.exceptions import ExtractionUnavailableError
from app.services.audit.jobs import is_retryable_error
from app.services.cartograph.tasks import keep_packet, prune_packets, run_with_retry


def test_a_transient_failure_is_retried_and_a_permanent_one_is_not():
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise ExtractionUnavailableError("model unavailable")
        return "ok"
    retries = []
    assert run_with_retry(flaky, attempts=2, delay=0, is_retryable=is_retryable_error,
                          on_retry=lambda n, e: retries.append(n)) == "ok"
    assert calls == [1, 1] and retries == [1]
    # A second transient failure on the last attempt is raised.
    def always():
        raise ExtractionUnavailableError("still down")
    with pytest.raises(ExtractionUnavailableError):
        run_with_retry(always, attempts=2, delay=0, is_retryable=is_retryable_error)
    # A permanent failure is raised at once.
    n = []
    def bug():
        n.append(1); raise ValueError("schema")
    with pytest.raises(ValueError):
        run_with_retry(bug, attempts=3, delay=0, is_retryable=is_retryable_error)
    assert len(n) == 1


def test_packets_are_kept_and_pruned_by_age_and_size(tmp_path):
    class S:
        output_dir = tmp_path
        pdf_retention_days = 14
        pdf_retention_max_mb = 1
    p = keep_packet(b"%PDF-1.4 x" * 10, "J-TEST-1", S)
    assert p and p.exists() and p.parent == tmp_path / "pdfs"
    old = tmp_path / "pdfs" / "J-OLD_20260101-000000.pdf"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))
    big = tmp_path / "pdfs" / "J-BIG_20260901-000000.pdf"
    big.write_bytes(b"0" * (1024 * 1024 + 1))
    os.utime(big, (time.time() - 3600, time.time() - 3600))
    deleted = prune_packets(tmp_path / "pdfs", 14, 1)
    assert not old.exists() and not big.exists() and p.exists() and deleted == 2
    # Keeping disabled: nothing written.
    class Off(S):
        pdf_retention_days = 0
    assert keep_packet(b"x", "J-TEST-2", Off) is None


def test_a_rejected_findings_feedback_body_is_logged_with_its_shape(caplog):
    """Cartograph's nightly feedback push was answered 400 on every night
    and the engine kept nothing about what arrived. A rejection now logs
    the top-level shape and the body, cut to a fixed length."""
    import logging
    import pytest
    from fastapi import HTTPException
    from app.routers.integration import _store_findings_feedback, _describe_rejected_body
    with caplog.at_level(logging.WARNING, logger="app.routers.integration"):
        with pytest.raises(HTTPException) as e:
            _store_findings_feedback({"event_type": "findings_feedback", "cases": [{"cert_review_id": 1}]},
                                     b'{"event_type": "findings_feedback", "cases": [{"cert_review_id": 1}]}')
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e:
            _store_findings_feedback([{"case_ref": "J-1"}], b'[{"case_ref": "J-1"}]')
        assert e.value.status_code == 400
    msgs = [r.getMessage() for r in caplog.records]
    assert any("no case_ref" in m and "keys ['cases', 'event_type']" in m and "cert_review_id" in m for m in msgs)
    assert any("not an object" in m and "list of 1 of objects with keys ['case_ref']" in m for m in msgs)
    long = _describe_rejected_body(b"x" * 5000)
    assert "shape=unparseable" in long and "[5000 bytes]" in long and len(long) < 2200


def test_cartograph_nightly_feedback_batch_is_stored_case_by_case(tmp_path, monkeypatch):
    """Cartograph's nightly push is a batch envelope, not the documented
    one-case event: `cases` at the top level, `finding_verdicts` with
    `reason`, `missed_findings` with flat checklist fields. It was answered
    400 every night. Now each case is stored under the documented names."""
    from app.routers import integration
    from app.services.audit.job_store import JobStore
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(integration, "get_job_store", lambda _path: store)
    body = {
        "event": "findings_feedback",
        "generated_at": "2026-09-25T02:00:02-04:00",
        "cases": [
            {"case_ref": "J-VRC-06406", "cert_review_id": 1753,
             "finding_verdicts": [{"finding_key": "AR_PREVIOUS_CERT_MISSING:case", "scan_id": None,
                                   "verdict": "invalid", "reason": None}],
             "missed_findings": [{"source": "manual", "description": "this cert type is a Move In",
                                  "page": None, "subject_label": None,
                                  "checklist_item_key": "cert_type", "checklist_item_name": "Cert type"}]},
            {"case_ref": "J-VRC-06407", "cert_review_id": 1754,
             "finding_verdicts": [{"finding_key": "INCOME_DECLARED_NOT_VERIFIED:rebecca_knott:benefit_letter",
                                   "scan_id": None, "verdict": "invalid", "reason": "form 3560 is USDA/RD"}],
             "missed_findings": []},
            {"cert_review_id": 9999, "finding_verdicts": []},
        ],
    }
    out = integration._store_findings_feedback(body, b"{}")
    assert out["ok"] is True
    assert out["received"] == ["J-VRC-06406", "J-VRC-06407"]
    assert out["stored"]["J-VRC-06406"] == {"verdicts": 1, "manual_findings": 1}
    assert out["stored"]["J-VRC-06407"] == {"verdicts": 1, "manual_findings": 0}
    assert out["skipped"] == 1

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT case_id, scan_id, source, finding_key, verdict, verdict_reason, description, matched_checklist_item "
            "FROM finding_feedback ORDER BY case_id, source"
        ).fetchall()
    rows = [tuple(r) for r in rows]
    assert rows == [
        ("J-VRC-06406", "1753", "manual", "", None, None, "this cert type is a Move In",
         '{"key": "cert_type", "name": "Cert type"}'),
        ("J-VRC-06406", "1753", "runpod", "AR_PREVIOUS_CERT_MISSING:case", "invalid", None, "", None),
        ("J-VRC-06407", "1754", "runpod", "INCOME_DECLARED_NOT_VERIFIED:rebecca_knott:benefit_letter",
         "invalid", "form 3560 is USDA/RD", "", None),
    ]
    summary = {r["code"]: r for r in store.feedback_summary()}
    assert summary["AR_PREVIOUS_CERT_MISSING"]["verdicts"] == 1
    assert summary["AR_PREVIOUS_CERT_MISSING"]["valid"] == 0

    # The documented one-case shape still works and replaces the verdict.
    out = integration._store_findings_feedback(
        {"event_type": "findings_feedback", "case_ref": "J-VRC-06406", "scan_id": 42,
         "verdicts": [{"finding_key": "AR_PREVIOUS_CERT_MISSING:case", "verdict": "valid"}]}, b"{}")
    assert out == {"ok": True, "received": "J-VRC-06406", "stored": {"verdicts": 1, "manual_findings": 0}}


def test_attachments_are_assembled_by_what_they_are_not_what_they_are_called():
    """A PNG "VOI scan" becomes a page; a text file is left out with a
    warning; a case with nothing pageable is a document-unavailable
    failure, not an exception that leaves the job wedged."""
    import fitz
    import pytest
    from app.services.cartograph.documents import DocumentUnavailable, assemble_packet, sniff_kind
    pdf = fitz.open(); pdf.new_page().insert_text((72, 72), "certification"); pdf_bytes = pdf.tobytes(); pdf.close()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), False); pix.clear_with(200); png = pix.tobytes("png")
    assert sniff_kind(pdf_bytes) == "pdf" and sniff_kind(png) == "png" and sniff_kind(b"hello world") is None

    merged, warnings = assemble_packet([("file-review-upload.pdf", pdf_bytes), ("VOI scan.png", png), ("force-delete-test.txt", b"hello")])
    with fitz.open(stream=merged, filetype="pdf") as out:
        assert out.page_count == 2
    assert warnings == ["attachment force-delete-test.txt is not a PDF or an image and was left out of the audit"]

    with pytest.raises(DocumentUnavailable):
        assemble_packet([("notes.txt", b"hello"), ("data.csv", b"a,b\n1,2")])


def test_an_unexpected_failure_in_the_audit_task_is_recorded_and_reported(monkeypatch, tmp_path):
    """Whatever escapes the inner task marks the job failed and tells
    Cartograph, so a re-notification is accepted instead of ignored."""
    from app.services.cartograph import tasks
    from app.services.audit.job_store import JobStore
    store = JobStore(tmp_path / "jobs.db")
    store.upsert_pending("J-X", "J-X", "annual", "LIHTC", None, source="cartograph")
    store.mark_extracting("J-X")
    posted: list = []
    monkeypatch.setattr(tasks, "get_job_store", lambda _p: store)
    monkeypatch.setattr(tasks, "post_failure", lambda case_ref, reason, settings, **kw: posted.append((case_ref, reason, kw.get("error_code"))))
    def boom(*a, **k): raise RuntimeError("source or target not a PDF")
    monkeypatch.setattr(tasks, "_audit_case", boom)
    tasks.audit_case(case_ref="J-X", documents=[{"url": "https://example.invalid/x"}])
    assert store.get("J-X")["state"] == "extraction_failed"
    assert posted == [("J-X", "unexpected error: source or target not a PDF", "engine_error")]


def test_checklist_rows_are_read_from_both_request_shapes():
    """Today's nested items (one entry, several finding ids) and the flat
    rows that replace them (one row per finding id, with a subject)
    produce the same normalised rows; a finding id seen twice is one row."""
    from app.services.cartograph.checklist import normalise_rows
    body = {
        "case_ref": "J-CCAC-06832",
        "requirements": [{"community_defaults": {"checklist_items": [
            {"checklist_item_id": 12, "item_code": "HUDS8-FORM-50059", "label": "HUD 50059 Completed and Accurate", "case_finding_ids": [501]},
            {"checklist_item_id": 13, "item_code": "HUDS8-FORM-9887A", "label": "HUD 9887-A signed by each adult", "case_finding_ids": [502, 503]},
        ]}}],
        "checklist_rows": [
            {"finding_id": 502, "checklist_item_id": 13, "item_code": "HUDS8-FORM-9887A", "label": "HUD 9887-A signed by each adult",
             "subject_type": "household_member", "subject_id": 77, "subject_label": "Elise Dodd"},
        ],
    }
    rows = normalise_rows(body)
    assert [(r["finding_id"], r["item_code"], r.get("subject_label")) for r in rows] == [
        (502, "HUDS8-FORM-9887A", "Elise Dodd"), (501, "HUDS8-FORM-50059", None), (503, "HUDS8-FORM-9887A", None)]
    assert normalise_rows({}) == []


def test_checklist_rows_map_to_document_types_by_form_number_then_title_then_words():
    from app.services.cartograph.checklist import document_type_for
    assert document_type_for({"label": "HUD 50059 Completed and Accurate", "item_code": "HUDS8-FORM-50059"}) == ("HUD 50059", "form_number")
    assert document_type_for({"label": "HUD 9887 Notice and Consent signed", "item_code": "HUDS8-FORM-9887"}) == ("HUD 9887", "form_number")
    assert document_type_for({"label": "HUD 9887-A signed by each adult", "item_code": "HUDS8-FORM-9887A"}) == ("HUD 9887-A", "form_number")
    assert document_type_for({"label": "Race and Ethnic Data Reporting Form", "item_code": "HUDS8-FORM-RACE"})[0] == "HUD Race and Ethnic Data Form"
    assert document_type_for({"label": "Verification of Assets on file for each account", "item_code": "GEN-VOA"})[0] == "Verification of Assets (VOA)"
    assert document_type_for({"label": "Rent reasonableness memo", "item_code": "X-1"}) == (None, None)


def test_checklist_matches_say_found_pages_signature_and_confidence():
    from types import SimpleNamespace
    from app.schemas.extraction import CertificationInfo, DocumentGroup, DocumentInventory, DocumentInventoryEntry
    from app.services.cartograph.checklist import match_checklist
    def g(label, pages, person=None, notes=None, category="include"):
        return DocumentGroup(document_type=label, category=category, pages=pages, page_range=str(pages[0]), combined_text="x", person_name=person, notes=notes)
    ex = SimpleNamespace(
        document_groups=[g("HUD 50059", [2, 3]), g("HUD 50059 (Previous)", [4], category="ignore"), g("HUD 9887", [38, 39], category="compliance"),
                         g("HUD 9887-A", [40, 41], person="Yolanda Bribiesca", category="compliance"),
                         g("HUD 9887-A", [42], person="Jasmine Bribiesca", category="compliance"),
                         g("Student Status Certification", [27, 28], person="Jasmine Bribiesca", notes="nearest match — page titled 'Student Certification'")],
        document_inventory_hud=DocumentInventory(documents=[
            DocumentInventoryEntry(documentType="HUD 9887", isSigned="Yes", signedBy="Yolanda Bribiesca", signatureDate="2026-09-16"),
            DocumentInventoryEntry(documentType="HUD 9887-A", personName="Yolanda Bribiesca", isSigned="No"),
            DocumentInventoryEntry(documentType="Student Status Certification", personName="Jasmine Bribiesca", isSigned="No")]),
        document_inventory_financial=DocumentInventory(documents=[]),
        certification_info=CertificationInfo(certificationType="AR", isSigned="No"),
    )
    rows = [
        {"finding_id": 1, "item_code": "HUDS8-FORM-50059", "label": "HUD 50059 Completed and Accurate"},
        {"finding_id": 2, "item_code": "HUDS8-FORM-9887", "label": "HUD 9887 Notice and Consent signed by all adults"},
        {"finding_id": 3, "item_code": "HUDS8-FORM-9887A", "label": "HUD 9887-A", "subject_label": "Yolanda Bribiesca"},
        {"finding_id": 4, "item_code": "HUDS8-FORM-9887A", "label": "HUD 9887-A", "subject_label": "Sofia Bribiesca-Soto"},
        {"finding_id": 5, "item_code": "GEN-STUDENT", "label": "Student Status Certification", "subject_label": "Jasmine Bribiesca"},
        {"finding_id": 6, "item_code": "HUDS8-FORM-RACE", "label": "Race and Ethnic Data Form"},
        {"finding_id": 7, "item_code": "X-MEMO", "label": "Rent reasonableness memo"},
    ]
    out = {m["finding_id"]: m for m in match_checklist(rows, ex)}
    assert out[1]["found"] and out[1]["pages"] == [2, 3] and "Not signed: the signature lines are blank" in out[1]["note"]
    assert out[2]["found"] and "Signed by Yolanda Bribiesca on 2026-09-16" in out[2]["note"] and out[2]["confidence"] == 0.9
    assert out[3]["found"] and out[3]["pages"] == [40, 41] and "check visually" in out[3]["note"] and out[3]["confidence"] == 0.6
    assert out[4]["found"] and out[4]["pages"] == [40, 41, 42]          # no row for Sofia: the adults' forms, lower certainty is the reviewer's call
    assert out[5]["found"] and out[5]["confidence"] < 0.9               # placed by nearest match
    assert out[6] == {"finding_id": 6, "found": False, "pages": [], "confidence": 0.8, "note": "No HUD Race and Ethnic Data Form in the packet.", "note_source": "scan"}
    # The scan's notes are told from staff notes by a field, not a marker
    # in the text staff would have to delete.
    assert all(m["note_source"] == "scan" and not m["note"].startswith("[") for m in out.values())
    assert 7 not in out                                                  # unmappable row left untouched


def test_checklist_rows_about_a_forms_properties_or_its_previous_version_are_handled_as_such():
    """J-CCAC-06832: "HUD 50059 Includes the Correct Income Limits" got a
    presence note, and "Previous HUD 50059" was matched to the current
    form. A row about a property of a form is left untouched; a row asking
    for the previous form finds the previous form; a misspelled label
    still finds its type; an incomplete form says so."""
    from types import SimpleNamespace
    from app.schemas.extraction import CertificationInfo, DocumentGroup, DocumentInventory, DocumentInventoryEntry, Finding
    from app.services.cartograph.checklist import document_type_for, is_attribute_row, match_checklist
    assert is_attribute_row({"label": "HUD 50059 Includes the Correct Income Limits"}) is True
    assert is_attribute_row({"label": "HUD 50059 Completed and Accurate"}) is False
    assert document_type_for({"label": "Appliication Questionnaire complete, initialed/signed, and dated", "item_code": "HUDS8-FORM-APPLICATION"})[0] == "Application / Housing Questionnaire"
    assert document_type_for({"label": "Acknowledgement of HUD Handouts", "item_code": "HUDS8-FORM-HUD-HANDOUTS"})[0] == "Acknowledgement of Receipt"
    def g(label, pages, category="include", notes=None):
        return DocumentGroup(document_type=label, category=category, pages=pages, page_range=str(pages[0]), combined_text="x", notes=notes)
    ex = SimpleNamespace(
        document_groups=[g("HUD 50059", [13, 14]), g("HUD 50059 (Previous)", [18, 19], category="ignore", notes="Previous cert, move-in 9/2/2025"),
                         g("HUD 9887", [37, 38], category="compliance")],
        document_inventory_hud=DocumentInventory(documents=[DocumentInventoryEntry(documentType="HUD 9887", isSigned="Yes")]),
        document_inventory_financial=DocumentInventory(documents=[]),
        certification_info=CertificationInfo(certificationType="AR", isSigned="No"),
        finding_records=[Finding(code="HUD_9887_INCOMPLETE", text="HUD 9887 (pages 37, 38) is missing its agencies / expiry page — the form is incomplete.",
                                 subject_type="document", subject_ref={"document_type": "HUD 9887", "pages": [37, 38]})],
    )
    rows = [
        {"finding_id": 1, "item_code": "HUDS8-INC-LIMIT-50059", "label": "HUD 50059 Includes the Correct Income Limits"},
        {"finding_id": 2, "item_code": "HUDS8-RECERT-PRIOR-50059", "label": "Previous HUD 50059 Certification Form"},
        {"finding_id": 3, "item_code": "HUDS8-FORM-9887", "label": "HUD9887 Completed and Signed by All Adult Members"},
    ]
    out = {m["finding_id"]: m for m in match_checklist(rows, ex, expect_previous=True)}
    assert 1 not in out
    assert out[2]["found"] and out[2]["pages"] == [18, 19] and out[2]["note"].startswith("Previous HUD 50059 present, pages 18-19.")
    assert out[3]["found"] and "Incomplete: missing its agencies / expiry page." in out[3]["note"]
    # A case holds one certification and the prior year lives on its own
    # record, so by default a previous-form row is not the packet's to
    # answer and is left untouched.
    assert 2 not in {m["finding_id"] for m in match_checklist(rows, ex)}


def test_checklist_note_says_whether_a_missing_signature_date_is_unreadable_or_absent():
    """"Date not read" covered two different things: handwriting the OCR
    could not resolve and a date slot left blank. The note says which."""
    from types import SimpleNamespace
    from app.schemas.extraction import DocumentGroup, DocumentInventory, DocumentInventoryEntry
    from app.services.cartograph.checklist import match_checklist
    from app.services.inventory_builder import _build_entry
    def g(label, pages, text):
        return DocumentGroup(document_type=label, category="compliance", pages=pages, page_range=str(pages[0]), combined_text=text)
    unclear = g("HUD 92006", [40], "Signature of Head of Household: Yolanda Bribiesca   Date: 9/lb/2o")
    blank = g("HUD 9887", [41], "Signature of Head of Household: Yolanda Bribiesca   Date: ______________")
    entries = [_build_entry(unclear), _build_entry(blank)]
    assert [e.signatureDateState for e in entries] == ["unclear", "blank"]
    ex = SimpleNamespace(document_groups=[unclear, blank],
                         document_inventory_hud=DocumentInventory(documents=entries),
                         document_inventory_financial=DocumentInventory(documents=[]), certification_info=None)
    rows = [{"finding_id": 1, "item_code": "HUDS8-FORM-92006", "label": "HUD 92006"},
            {"finding_id": 2, "item_code": "HUDS8-FORM-9887", "label": "HUD 9887"}]
    out = {m["finding_id"]: m for m in match_checklist(rows, ex)}
    assert out[1]["note"].endswith("Signed, date unclear.")
    assert out[2]["note"].endswith("Signed, undated.")
    read = _build_entry(g("HUD 9887", [1], "Signature of Head of Household: Yolanda Bribiesca   Date: 09/16/2026"))
    assert (read.signatureDate, read.signatureDateState) == ("2026-09-16", "read")
