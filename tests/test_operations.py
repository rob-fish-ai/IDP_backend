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
