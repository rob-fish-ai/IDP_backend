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
            _store_findings_feedback({"event_type": "findings_feedback", "cases": [{"case_ref": "J-1"}]},
                                     b'{"event_type": "findings_feedback", "cases": [{"case_ref": "J-1"}]}')
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e:
            _store_findings_feedback([{"case_ref": "J-1"}], b'[{"case_ref": "J-1"}]')
        assert e.value.status_code == 400
    msgs = [r.getMessage() for r in caplog.records]
    assert any("no case_ref" in m and "keys ['cases', 'event_type']" in m and "J-1" in m for m in msgs)
    assert any("not an object" in m and "list of 1 of objects with keys ['case_ref']" in m for m in msgs)
    long = _describe_rejected_body(b"x" * 5000)
    assert "shape=unparseable" in long and "[5000 bytes]" in long and len(long) < 2200
