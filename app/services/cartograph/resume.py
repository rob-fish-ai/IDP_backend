"""Pick up the cases that were in flight when the engine last stopped.

A Cartograph case runs on a worker of the process that received the
notification. When that process stops — a pod restart, a deploy, a
crash — its job row stays in `queued` or `extracting`, nothing runs it
again, and the watchdog fails it half an hour later with a request to
re-notify. Fifteen cases of one morning's batch ended that way, two days
late, and Cartograph's import took each failure notice for a result.

Everything needed to run the case again is kept: the request as received
(`cartograph_request`: the document URLs, checklist rows and manifest) and
the packet itself under output_dir/pdfs. So at start the in-flight
Cartograph rows are claimed at once — which keeps the watchdog's first
sweep off them — and run again in the order they arrived, the kept packet
standing in when the presigned URLs have expired. A row whose request was
not kept cannot be re-run and is failed with that reason, so Cartograph
hears at start rather than at the first sweep.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from app.services.audit.job_store import _OWNED_STATES

logger = logging.getLogger(__name__)

_THREAD: threading.Thread | None = None


def interrupted_cases(store) -> list[dict]:
    """The Cartograph rows some worker owned when the last process stopped,
    oldest first. At start nothing in this process owns a row, so every
    owned Cartograph row is one that was interrupted."""
    rows: list[dict] = []
    for state in _OWNED_STATES:
        rows.extend(r for r in store.list_by_state(state) if (r.get("source") or "") == "cartograph")
    rows.sort(key=lambda r: r.get("created_at") or 0)
    return rows


def kept_packet(case_ref: str, settings) -> Path | None:
    """The newest packet kept for the case under output_dir/pdfs, if any."""
    folder = Path(settings.output_dir) / "pdfs"
    if not folder.is_dir():
        return None
    candidates = sorted(folder.glob(f"{case_ref}_*.pdf"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def resume_kwargs(row: dict, settings) -> dict | None:
    """The arguments the notification handler passed to audit_case, rebuilt
    from the request as received. None when no request was kept."""
    from app.services.cartograph.checklist import normalise_rows
    raw = row.get("cartograph_request")
    if not raw:
        return None
    try:
        body = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return None
    if not isinstance(body, dict) or not body.get("documents"):
        return None
    packet = kept_packet(row["case_id"], settings)
    return {
        "case_ref": row["case_id"],
        "documents": body["documents"],
        "cert_type": body.get("cert_type"),
        "program": body.get("program"),
        "job_id": body.get("job_id"),
        "community_id": body.get("community_id"),
        "unit_number": body.get("unit_number"),
        "effective_date": body.get("effective_date"),
        "checklist_rows": normalise_rows(body),
        "existing_records_manifest": body.get("existing_records_manifest"),
        "fallback_packet": str(packet) if packet else None,
    }


def claim_interrupted_cases(settings, store) -> list[dict]:
    """Stamp every interrupted row as alive so the watchdog leaves it to
    this process, and fail the ones that cannot be run again. Returns the
    rows to run, each with its rebuilt arguments under "kwargs"."""
    from app.services.cartograph.client import post_failure
    rows = interrupted_cases(store)
    if not rows:
        return []
    runnable: list[dict] = []
    for row in rows:
        kwargs = resume_kwargs(row, settings)
        if kwargs is None:
            reason = ("the engine restarted while this case was being audited and the request "
                      "was not kept — re-notify the case to audit it again")
            logger.error("Resume: case_ref=%s was in '%s' at the last stop and has no stored request — failed",
                         row["case_id"], row.get("state"))
            store.mark_extraction_failed(row["case_id"], reason)
            try:
                post_failure(row["case_id"], reason, settings, error_code="engine_restarted")
            except Exception:
                logger.exception("Resume: could not report case_ref=%s to Cartograph", row["case_id"])
            continue
        store.touch(row["case_id"])
        runnable.append({**row, "kwargs": kwargs})
    logger.warning(
        "Resume: %d case(s) were in flight at the last stop and will be audited again: %s",
        len(runnable), ", ".join(r["case_id"] for r in runnable),
    )
    return runnable


def run_interrupted_cases(settings, rows: list[dict], *, run=None, delay: float | None = None) -> list[str]:
    """Audit the claimed rows again through the intake, after the delay.
    They share its workers with whatever Cartograph sends meanwhile, so
    the bound on cases running at once holds across both."""
    from app.services.cartograph.intake import Intake, get_intake
    delay = settings.audit_resume_delay_seconds if delay is None else delay
    if not rows:
        return []
    if delay > 0:
        time.sleep(delay)
    intake = get_intake(settings) if run is None else Intake(workers=settings.audit_pipeline_concurrency, run=run)
    done: list[str] = []
    futures = {}
    for row in rows:
        kwargs = row["kwargs"]
        logger.info("Resume: auditing case_ref=%s again (packet %s)", row["case_id"],
                    "kept" if kwargs.get("fallback_packet") else "to be fetched")
        futures[intake.submit(**kwargs)] = row["case_id"]
    for future, case_ref in futures.items():
        try:
            future.result()
        except Exception:
            logger.exception("Resume: case_ref=%s failed while being audited again", case_ref)
        done.append(case_ref)
    if run is not None:
        intake.shutdown()
    return done


def start_resume_thread(settings, store) -> list[str]:
    """Claim the interrupted cases now and run them on a daemon thread.
    Returns the case refs claimed."""
    global _THREAD
    if not settings.audit_resume_on_start:
        return []
    try:
        rows = claim_interrupted_cases(settings, store)
    except Exception:
        logger.exception("Resume: could not read the interrupted cases")
        return []
    if not rows:
        return []
    _THREAD = threading.Thread(
        target=run_interrupted_cases, args=(settings, rows), name="idp-resume", daemon=True,
    )
    _THREAD.start()
    return [r["case_id"] for r in rows]
