"""Operational rules on the Cartograph path: transient failures are retried,
packets are kept and pruned."""
import os
import time
from pathlib import Path

import pytest

from app.core.exceptions import ExtractionUnavailableError
from app.services.cartograph.maintenance import is_retryable_error
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
    assert out["stored"]["J-VRC-06406"] == {"verdicts": 1, "manual_findings": 1, "corrections": 0}
    assert out["stored"]["J-VRC-06407"] == {"verdicts": 1, "manual_findings": 0, "corrections": 0}
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
    assert out == {"ok": True, "received": "J-VRC-06406", "stored": {"verdicts": 1, "manual_findings": 0, "corrections": 0}}


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


def test_checklist_note_calls_out_a_blank_signature_date_but_not_one_it_could_not_read():
    """A date slot left blank is a defect in the file, so the note says
    "undated". Handwriting the OCR could not resolve is the engine's
    limit, not the file's, and the note just says "Signed": Cartograph
    found the old "date unclear" on most rows and asked for it to go."""
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
    assert out[1]["note"].endswith("HUD 92006 present, page 40. Signed.")
    assert out[2]["note"].endswith("Signed, undated.")
    read = _build_entry(g("HUD 9887", [1], "Signature of Head of Household: Yolanda Bribiesca   Date: 09/16/2026"))
    assert (read.signatureDate, read.signatureDateState) == ("2026-09-16", "read")


# ---------------------------------------------------------------------------
# Cases in flight when the engine stopped
# ---------------------------------------------------------------------------

def _resume_settings(tmp_path):
    from types import SimpleNamespace
    return SimpleNamespace(output_dir=tmp_path, audit_resume_on_start=True,
                           audit_resume_delay_seconds=0, audit_pipeline_concurrency=2)


def test_cases_in_flight_at_the_last_stop_are_claimed_and_audited_again(monkeypatch, tmp_path):
    """Fifteen cases of one morning's batch were mid-extraction when the pod
    went down; at the next start the watchdog failed them and Cartograph
    took each failure for a result. Now they are claimed before the
    watchdog's first sweep and run again from the request as received and
    the packet kept — a row with no request cannot be run and is failed at
    once instead."""
    import json
    from app.services.audit.job_store import JobStore
    from app.services.cartograph import resume
    store = JobStore(tmp_path / "jobs.db")
    settings = _resume_settings(tmp_path)

    def in_flight(case, state="extracting", source="cartograph", request=None):
        store.upsert_pending(case, case, "annual", "HUD 202 PRAC", None, source=source, claimed=True)
        if state == "extracting":
            store.mark_extracting(case)
        if request is not None:
            store.record_request(case, json.dumps(request), [])
        with store._connect() as conn:  # long stale, as after a two-day outage
            conn.execute("UPDATE audit_jobs SET updated_at = ? WHERE case_id = ?", (time.time() - 160_000, case))

    in_flight("J-A", request={"case_ref": "J-A", "documents": [{"url": "https://x/a.pdf", "job_document_id": 1}],
                              "cert_type": "annual", "job_id": 7, "unit_number": "406",
                              "existing_records_manifest": {"household_members": [{"id": 1}]}})
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "J-A_20261010-044732.pdf").write_bytes(b"%PDF-old")
    (tmp_path / "pdfs" / "J-A_20261010-052947.pdf").write_bytes(b"%PDF-new")
    in_flight("J-B", state="queued", request={"case_ref": "J-B", "documents": [{"url": "https://x/b.pdf"}]})
    in_flight("J-C")                                   # no request kept
    in_flight("500Rb00001", source="salesforce")       # a row left by the retired Salesforce path
    store.upsert_pending("J-D", "J-D", "annual", "HUD", None, source="cartograph", claimed=True)
    store.mark_done("J-D", findings_text="", confidence=1.0)

    posted: list = []
    monkeypatch.setattr("app.services.cartograph.client.post_failure",
                        lambda case_ref, reason, settings, **kw: posted.append((case_ref, kw.get("error_code"))))
    assert len(store.list_wedged(("queued", "extracting"), stale_seconds=1800)) == 4

    rows = resume.claim_interrupted_cases(settings, store)
    assert [r["case_id"] for r in rows] == ["J-A", "J-B"]
    a = rows[0]["kwargs"]
    assert a["fallback_packet"].endswith("J-A_20261010-052947.pdf")
    assert a["job_id"] == 7 and a["unit_number"] == "406" and a["documents"][0]["job_document_id"] == 1
    assert a["existing_records_manifest"] == {"household_members": [{"id": 1}]}
    assert rows[1]["kwargs"]["fallback_packet"] is None
    # Claimed rows are no longer wedged; the one that cannot run is failed and reported.
    assert [r["case_id"] for r in store.list_wedged(("queued", "extracting"), stale_seconds=1800)] == ["500Rb00001"]
    assert store.get("J-C")["state"] == "extraction_failed" and "re-notify" in store.get("J-C")["error"]
    assert posted == [("J-C", "engine_restarted")]
    assert store.get("500Rb00001")["state"] == "extracting" and store.get("J-D")["state"] == "done"

    ran: list = []
    done = resume.run_interrupted_cases(settings, rows, run=lambda **kw: ran.append(kw["case_ref"]), delay=0)
    assert sorted(done) == sorted(ran) == ["J-A", "J-B"]


def test_a_case_run_again_after_its_links_expired_is_audited_from_the_kept_packet(monkeypatch, tmp_path):
    from app.services.audit.job_store import JobStore
    from app.services.cartograph import tasks
    from app.services.cartograph.documents import DocumentUnavailable
    from types import SimpleNamespace
    store = JobStore(tmp_path / "jobs.db")
    store.upsert_pending("J-A", "J-A", "annual", "HUD", None, source="cartograph", claimed=True)
    kept = tmp_path / "J-A_20261010-052947.pdf"
    kept.write_bytes(b"%PDF-kept")
    settings = SimpleNamespace(output_dir=tmp_path, pdf_retention_days=14, pdf_retention_max_mb=10,
                               cartograph_extract_attempts=1, cartograph_retry_delay_seconds=0)
    seen: dict = {}

    def expired(_documents):
        raise DocumentUnavailable("403 on every presigned URL")

    def process(pdf_bytes, *_a, **_k):
        seen["bytes"] = pdf_bytes
        raise RuntimeError("stop here")

    monkeypatch.setattr(tasks, "fetch_packet", expired)
    monkeypatch.setattr(tasks, "process_pdf_full", process)
    monkeypatch.setattr(tasks, "keep_packet", lambda *a, **k: seen.setdefault("kept_again", True))
    monkeypatch.setattr(tasks, "post_failure", lambda *a, **k: None)
    tasks._audit_case(store, settings, case_ref="J-A", documents=[{"url": "https://x/a.pdf"}], cert_type="annual",
                      program=None, job_id=None, community_id=None, unit_number=None, effective_date=None,
                      fallback_packet=str(kept))
    assert seen["bytes"] == b"%PDF-kept" and "kept_again" not in seen
    assert store.get("J-A")["state"] == "extraction_failed" and "stop here" in store.get("J-A")["error"]

    # Without a kept packet the expired links fail the case as before.
    store.upsert_pending("J-B", "J-B", "annual", "HUD", None, source="cartograph", claimed=True)
    tasks._audit_case(store, settings, case_ref="J-B", documents=[{"url": "https://x/b.pdf"}], cert_type="annual",
                      program=None, job_id=None, community_id=None, unit_number=None, effective_date=None,
                      fallback_packet=str(tmp_path / "missing.pdf"))
    assert "document unavailable" in store.get("J-B")["error"]


def test_the_intake_audits_a_few_cases_at_once_and_keeps_the_waiting_ones_alive():
    """Thirty-four notifications in one hour once became twenty-six packets
    rendered together, the pod's memory cap, and a dead engine. Now a few
    run at once and the rest wait in line — with a heartbeat, so the
    watchdog does not take a long wait for a dead worker."""
    import threading
    from app.services.cartograph.intake import Intake

    gate = threading.Event()
    started: list[str] = []
    touched: list[str] = []

    def run(**kwargs):
        started.append(kwargs["case_ref"])
        gate.wait(5)

    store = type("Store", (), {"touch": staticmethod(lambda case_ref: touched.append(case_ref))})()
    intake = Intake(workers=2, run=run, store=store, heartbeat_seconds=0.05)
    futures = [intake.submit(case_ref=f"J-{i}", documents=[]) for i in range(5)]
    time.sleep(0.3)
    assert sorted(started) == ["J-0", "J-1"]
    assert intake.running() == ["J-0", "J-1"] and intake.waiting() == ["J-2", "J-3", "J-4"]
    # Only the waiting cases are kept alive; the running ones have the pipeline's own heartbeat.
    assert set(touched) == {"J-2", "J-3", "J-4"}
    gate.set()
    for f in futures:
        f.result(timeout=5)
    assert sorted(started) == [f"J-{i}" for i in range(5)]
    assert intake.running() == [] and intake.waiting() == []
    intake.shutdown()


def test_a_notification_repeating_a_delivered_case_is_not_audited_again(tmp_path):
    """During the 2026-10-29 batch Cartograph re-sent 29 of 61 cases within
    minutes of accepting their results, and each was audited and imported
    again. A notification for a case delivered inside the window, naming the
    same documents, is a repeat; a different document list, a failed case or
    an old delivery is new work."""
    import json
    from app.routers.integration import repeat_of_delivered
    from app.services.audit.job_store import JobStore
    docs = [{"url": "https://s3/a?sig=1", "filename": "packet.pdf", "job_document_id": 7, "document_class": "scan"}]
    store = JobStore(tmp_path / "jobs.db")
    store.upsert_pending("J-R", "J-R", "annual", "LIHTC", None, source="cartograph", claimed=True)
    store.record_request("J-R", json.dumps({"case_ref": "J-R", "documents": docs}), [])
    store.mark_extracting("J-R")
    store.mark_done("J-R", findings_text="", confidence=0.9)
    job = store.get("J-R")
    done_at = job["completed_at"]

    resent = [{**docs[0], "url": "https://s3/a?sig=2"}]          # fresh presigned link, same file
    assert repeat_of_delivered(job, resent, 7200, now=done_at + 300) == 300
    assert repeat_of_delivered(job, resent, 0, now=done_at + 300) is None          # guard off
    assert repeat_of_delivered(job, resent, 7200, now=done_at + 7201) is None      # outside the window
    assert repeat_of_delivered(job, resent + [{"url": "u", "filename": "voa.pdf"}], 7200, now=done_at + 300) is None
    assert repeat_of_delivered(None, resent, 7200) is None

    store.mark_extraction_failed("J-R", "boom")
    assert repeat_of_delivered(store.get("J-R"), resent, 7200, now=done_at + 300) is None


def test_the_watchdog_fails_a_row_whose_worker_died_and_retention_prunes_old_rows(monkeypatch, tmp_path):
    """A row still extracting with no heartbeat for longer than the
    watchdog's patience belongs to a worker that died while the process
    lived on. It is failed and Cartograph is told to re-notify; a row from
    the retired Salesforce path is failed without a call. Terminal rows
    older than the retention window are deleted; in-flight ones never."""
    import httpx
    from types import SimpleNamespace
    from app.services.audit.job_store import JobStore
    from app.services.cartograph import maintenance
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(maintenance, "get_job_store", lambda _p: store)
    settings = SimpleNamespace(audit_job_db=tmp_path / "jobs.db", audit_watchdog_seconds=1800, audit_retention_days=7)

    def row(case, source="cartograph", age=0):
        store.upsert_pending(case, case, "annual", "HUD", None, source=source, claimed=True)
        store.mark_extracting(case)
        with store._connect() as conn:
            conn.execute("UPDATE audit_jobs SET updated_at = ? WHERE case_id = ?", (time.time() - age, case))
    row("J-DEAD", age=4000)
    store.upsert_pending("staging:J-DEAD", "J-DEAD", "annual", "HUD", None, claimed=True, environment="staging")
    store.mark_extracting("staging:J-DEAD")
    with store._connect() as conn:
        conn.execute("UPDATE audit_jobs SET updated_at = ? WHERE case_id = 'staging:J-DEAD'", (time.time() - 4000,))
    row("500Rb00001", source="salesforce", age=4000)
    row("J-SLOW", age=600)                                   # heartbeat 10 minutes ago: still working
    store.upsert_pending("J-OLD", "J-OLD", "annual", "HUD", None, claimed=True)
    store.mark_done("J-OLD", findings_text="", confidence=1.0)
    with store._connect() as conn:
        conn.execute("UPDATE audit_jobs SET updated_at = ? WHERE case_id = 'J-OLD'", (time.time() - 10 * 86400,))

    posted: list = []
    monkeypatch.setattr("app.services.cartograph.client.post_failure",
                        lambda case_ref, reason, settings, **kw: posted.append((case_ref, kw.get("error_code"), kw.get("environment"))))
    assert maintenance.watchdog_sweep(settings) == 3
    assert store.get("J-DEAD")["state"] == "extraction_failed" and "re-notify" in store.get("J-DEAD")["error"]
    assert store.get("500Rb00001")["state"] == "extraction_failed"
    assert store.get("J-SLOW")["state"] == "extracting"
    # the failure is reported to the environment the row came from
    assert sorted(posted, key=lambda t: t[2] or "") == [("J-DEAD", "engine_error", None), ("J-DEAD", "engine_error", "staging")]

    assert maintenance.retention_sweep(settings) == 1
    assert store.get("J-OLD") is None and store.get("J-DEAD") is not None

    assert maintenance.is_retryable_error(httpx.ReadTimeout("read timed out"))
    assert maintenance.is_retryable_error(RuntimeError("cannot schedule new futures after interpreter shutdown"))
    assert not maintenance.is_retryable_error(ValueError("source or target not a PDF"))


def test_the_output_sweep_removes_old_results_orphaned_work_dirs_and_aged_packets(monkeypatch, tmp_path):
    """What the engine writes under output_dir ages out by itself: the
    extraction JSON kept under results/, a work dir a crash left behind,
    and the kept packets — which otherwise only shrink when a new case
    arrives. A work dir of a case the store still owns is never touched,
    however old, and neither is anything inside the window."""
    import os
    from types import SimpleNamespace
    from app.services.audit.job_store import JobStore
    from app.services.cartograph import maintenance
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(maintenance, "get_job_store", lambda _p: store)
    settings = SimpleNamespace(audit_job_db=tmp_path / "jobs.db", output_dir=tmp_path, output_retention_days=30,
                               pdf_retention_days=14, pdf_retention_max_mb=1500)
    old = time.time() - 40 * 86400

    def aged(path):
        os.utime(path, (old, old))

    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "J-OLD_20261001-000000.json").write_text("{}")
    aged(tmp_path / "results" / "J-OLD_20261001-000000.json")
    (tmp_path / "results" / "J-NEW_20261101-000000.json").write_text("{}")
    for case in ("J-ORPHAN", "J-OWNED"):
        d = tmp_path / f"cartograph_{case}"
        d.mkdir()
        (d / "page_1.png").write_bytes(b"x")
        aged(d / "page_1.png")
        aged(d)
    store.upsert_pending("J-OWNED", "J-OWNED", "annual", "HUD", None, claimed=True)
    store.mark_extracting("J-OWNED")
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "J-OLD_20261001-000000.pdf").write_bytes(b"%PDF")
    aged(tmp_path / "pdfs" / "J-OLD_20261001-000000.pdf")
    (tmp_path / "pdfs" / "J-NEW_20261101-000000.pdf").write_bytes(b"%PDF")

    assert maintenance.output_sweep(settings) == 3
    assert sorted(p.name for p in (tmp_path / "results").iterdir()) == ["J-NEW_20261101-000000.json"]
    assert not (tmp_path / "cartograph_J-ORPHAN").exists() and (tmp_path / "cartograph_J-OWNED").is_dir()
    assert sorted(p.name for p in (tmp_path / "pdfs").iterdir()) == ["J-NEW_20261101-000000.pdf"]
    assert maintenance.output_sweep(settings) == 0
    settings.output_retention_days = 0
    aged(tmp_path / "results" / "J-NEW_20261101-000000.json")
    assert maintenance.output_sweep(settings) == 0


def test_the_extraction_sweep_clears_the_blob_from_old_terminal_rows_and_keeps_the_rows(monkeypatch, tmp_path):
    """At production volume the extraction blob is most of a row. Once a
    case is over and older than the window the blob goes and the row stays,
    with its state, findings and delivery record; a case in flight or
    inside the window keeps its blob, and 0 days keeps every blob — the
    corpus regression on a development host reads them."""
    from types import SimpleNamespace
    from app.services.audit.job_store import JobStore
    from app.services.cartograph import maintenance
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(maintenance, "get_job_store", lambda _p: store)
    settings = SimpleNamespace(audit_job_db=tmp_path / "jobs.db", extraction_retention_days=7)

    def row(case, state, age):
        store.upsert_pending(case, case, "annual", "LIHTC", None, claimed=True)
        store.mark_extracted(case, {"blob": "x" * 100})
        if state == "done":
            store.mark_done(case, findings_text="found", confidence=0.9)
        elif state == "failed":
            store.mark_comparison_failed(case, "ingest rejected")
        with store._connect() as conn:
            conn.execute("UPDATE audit_jobs SET updated_at = ? WHERE case_id = ?", (time.time() - age, case))
    row("J-OLD-DONE", "done", 10 * 86400)
    row("J-OLD-FAILED", "failed", 10 * 86400)
    row("J-NEW-DONE", "done", 86400)
    row("J-RUNNING", "extracted", 10 * 86400)

    assert maintenance.extraction_sweep(settings) == 2
    old_done = store.get("J-OLD-DONE")
    assert old_done["state"] == "done" and old_done["findings_text"] == "found" and old_done["extraction_result"] is None
    assert store.get("J-OLD-FAILED")["extraction_result"] is None
    assert store.get("J-NEW-DONE")["extraction_result"] == {"blob": "x" * 100}
    assert store.get("J-RUNNING")["extraction_result"] == {"blob": "x" * 100}
    assert maintenance.extraction_sweep(settings) == 0
    settings.extraction_retention_days = 0
    with store._connect() as conn:
        conn.execute("UPDATE audit_jobs SET updated_at = 0")
    assert maintenance.extraction_sweep(settings) == 0


def test_preflight_names_what_a_new_host_is_missing(tmp_path):
    """The preflight's offline checks: an environment with a placeholder, a
    bare host for an ingest URL or one secret used in both directions is a
    fail that names the field; a host with no environment is a warning,
    not a failure, because that is the safe state before credentials
    exist; the storage checks make the folders and open the store."""
    from app.core.config import CartographEnvironment, Settings
    from app.services import preflight

    def settings(**kw):
        return Settings(audit_job_db=tmp_path / "data" / "jobs.db", output_dir=tmp_path / "data" / "output",
                        cartograph_ingest_url="", cartograph_ingest_secret="", cartograph_callback_secret="", **kw)

    none = {n: (s, d) for n, s, d in preflight.check_environments(settings())}
    assert none["cartograph"][0] == "warn"

    envs = {
        "prod": CartographEnvironment(ingest_url="https://carto.example/webhooks/runpod_ocr_results",
                                      ingest_secret="out", callback_secret="in"),
        "staging": CartographEnvironment(ingest_url="https://staging.carto.example",
                                         ingest_secret="REPLACE_WITH_STAGING_INGEST_SECRET", callback_secret="same"),
        "dev": CartographEnvironment(ingest_url="https://dev.carto.example/webhooks/runpod_ocr_results",
                                     ingest_secret="same", callback_secret="same"),
    }
    got = {n: (s, d) for n, s, d in preflight.check_environments(settings(cartograph_environments=envs))}
    assert got["cartograph prod"] == ("ok", "delivers to carto.example")
    assert got["cartograph staging"][0] == "fail"
    assert "ingest_url is not a full https URL with a path" in got["cartograph staging"][1]
    assert "ingest_secret is a placeholder" in got["cartograph staging"][1]
    assert got["cartograph dev"] == ("fail", "ingest_secret and callback_secret are the same value")

    storage = {n: (s, d) for n, s, d in preflight.check_storage(settings(), warn_below_mb=0)}
    assert storage["output dir"][0] == "ok" and (tmp_path / "data" / "output").is_dir()
    assert storage["job store"] == ("ok", f"{tmp_path / 'data' / 'jobs.db'}: new")
    assert all(s == "ok" for s, _ in storage.values())
    assert preflight.run_all(settings(), network=False)[0][0] == "cartograph"


def test_a_case_notified_again_with_a_changed_request_is_audited_against_the_new_one(monkeypatch, tmp_path):
    """J-CASC-11421, 2026-11-23: Cartograph rebuilt the checklist rows and
    re-sent the case while the first run was extracting. The second
    notification was dropped as a duplicate, the result answered the old
    rows, and seven of them no longer existed on import. A re-send that
    changes the request now replaces the stored one; a case still in line
    starts from it, a case mid-run is run again when it ends. A re-send
    that repeats the request is still a duplicate."""
    import json
    from fastapi.testclient import TestClient
    from app.core.config import Settings
    from app.core.dependencies import get_settings
    from app.main import app
    from app.routers import integration
    from app.services.audit import job_store as js
    from app.services.cartograph import tasks
    from app.services.cartograph.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
    s = Settings(audit_job_db=tmp_path / "jobs.db", output_dir=tmp_path, cartograph_environments={},
                 cartograph_ingest_url="https://carto.example/in", cartograph_ingest_secret="out", cartograph_callback_secret="in")
    app.dependency_overrides[get_settings] = lambda: s
    monkeypatch.setattr("app.main.get_settings", lambda: s)
    monkeypatch.setattr("app.services.cartograph.maintenance.get_settings", lambda: s)
    monkeypatch.setattr(js, "_SINGLETON", js.JobStore(s.audit_job_db))
    store = js.get_job_store(s.audit_job_db)
    queued = []
    monkeypatch.setattr(integration, "enqueue_case", lambda **kw: queued.append(kw))
    docs = [{"url": "https://x/1.pdf?sig=a", "filename": "p.pdf", "job_document_id": 1}]
    first = {"case_ref": "J-1", "documents": docs, "checklist_rows": [{"finding_id": 10, "label": "HUD 50059 Completed and Accurate"}]}
    rebuilt = {"case_ref": "J-1", "documents": [{**docs[0], "url": "https://x/1.pdf?sig=b"}],
               "checklist_rows": [{"finding_id": 10, "label": "HUD 50059 Completed and Accurate"},
                                  {"finding_id": 11, "label": "HUD 92006 Form Completed, Signed, and Dated"}]}

    def post(body):
        b = json.dumps(body).encode()
        ts, sig = sign(b, "in")
        return client.post("/integration/case", content=b, headers={"Content-Type": "application/json", TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sig})

    try:
        with TestClient(app) as client:
            assert post(first).json()["status"] == "accepted"
            store.mark_extracting("J-1")
            # the same request again, fresh links: a duplicate
            assert post({**first, "documents": rebuilt["documents"]}).json()["status"] == "already_in_progress"
            assert not store.take_rerun("J-1")
            # the rebuilt checklist: stored, and a rerun marked
            r = post(rebuilt).json()
            assert r["status"] == "rerun_scheduled" and r["state"] == "extracting"
            assert [x["finding_id"] for x in json.loads(store.get("J-1")["checklist_rows"])] == [10, 11]
            assert len(queued) == 1
    finally:
        app.dependency_overrides.clear()

    # a case re-sent while still waiting in line starts from the new request, once
    ran, requeued = [], []
    monkeypatch.setattr(tasks, "get_settings", lambda: s)
    monkeypatch.setattr(tasks, "get_job_store", lambda _p: store)
    def fake_audit(store_, settings_, **kw):
        ran.append(kw)
        store_.mark_extracted("J-1", {}); store_.mark_done("J-1", findings_text="", confidence=1.0)
    monkeypatch.setattr(tasks, "_audit_case", fake_audit)
    monkeypatch.setattr("app.services.cartograph.intake.enqueue_case", lambda **kw: requeued.append(kw))
    tasks.audit_case(**queued[0])
    assert [x["finding_id"] for x in ran[0]["checklist_rows"]] == [10, 11] and requeued == []
    assert not store.take_rerun("J-1")

    # a case re-sent during its run is put back in line against the new request when the run ends
    ran.clear()
    def audit_during_which_cartograph_resends(store_, settings_, **kw):
        ran.append(kw)
        store_.request_rerun("J-1")
        store_.mark_extracted("J-1", {}); store_.mark_done("J-1", findings_text="", confidence=1.0)
    monkeypatch.setattr(tasks, "_audit_case", audit_during_which_cartograph_resends)
    tasks.audit_case(**queued[0])
    assert [x["finding_id"] for x in ran[0]["checklist_rows"]] == [10]       # the run under way answered the old rows
    assert len(requeued) == 1 and [x["finding_id"] for x in requeued[0]["checklist_rows"]] == [10, 11]
    assert requeued[0]["documents"][0]["url"].endswith("sig=b")
    assert store.get("J-1")["state"] == "queued" and not store.take_rerun("J-1")
