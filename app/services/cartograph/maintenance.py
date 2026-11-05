"""Housekeeping on the job store and the output folder, and the
transient-failure test the audit task retries on.

The maintenance thread runs three sweeps on an interval: retention deletes
terminal rows older than `audit_retention_days`, the output sweep deletes
what the engine wrote under output_dir more than `output_retention_days`
ago, and the watchdog fails a row whose worker died without the process
stopping. A row whose process
stopped is not the watchdog's: the resume path claims every owned
Cartograph row at start, before the first sweep, and runs it again.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.core.exceptions import ClassificationUnavailableError, ExtractionUnavailableError
from app.services.audit.job_store import _OWNED_STATES, EXTRACTED, EXTRACTING, QUEUED, get_job_store

logger = logging.getLogger(__name__)


def is_retryable_error(exc: BaseException) -> bool:
    """True when the failure looks transient, so the audit is tried again
    under the retry budget. Conservative: only known-transient classes
    (rate limits, server errors, network and timeouts, billing). Anything
    else — auth, schema, a bug — is reported rather than looped on."""
    msg = str(exc).lower()

    # No classified pages or no extraction: the model call behind them
    # failed, which is transient far more often than not.
    if isinstance(exc, (ClassificationUnavailableError, ExtractionUnavailableError)):
        return True

    # A restart landed mid-run. Says nothing about the case.
    if "interpreter shutdown" in msg or "cannot schedule new futures" in msg:
        return True

    try:
        import anthropic
        if isinstance(exc, (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        )):
            return True
        if isinstance(exc, anthropic.BadRequestError):
            # Credit exhaustion comes back as a 400 and clears when funds
            # are added; other 400s are bugs to surface.
            return "credit balance" in msg or "quota" in msg or "rate limit" in msg
    except ImportError:
        pass

    # The OCR service, the document fetch and the delivery all go through
    # httpx; a dropped connection or a timeout there is worth one more try.
    try:
        import httpx
        if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
            return True
    except ImportError:
        pass

    transient_markers = (
        "rate limit",
        "credit balance",
        "overloaded",
        "service unavailable",
        "gateway timeout",
        "timeout",
    )
    return any(marker in msg for marker in transient_markers)


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

# A row sitting in one of these past the watchdog threshold without its
# heartbeat moving belongs to a worker that died. The heartbeat is what
# makes that inference sound: the audit touches `updated_at` as it works,
# so the threshold measures time since the last sign of life, not time
# since the case started — a 42-minute packet is not a dead worker.
_WEDGE_PRONE_STATES: tuple[str, ...] = (QUEUED, EXTRACTING, EXTRACTED)


def retention_sweep(settings: Settings) -> int:
    """Delete terminal rows older than audit_retention_days; 0 disables."""
    days = settings.audit_retention_days
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    deleted = get_job_store(settings.audit_job_db).delete_terminal_older_than(cutoff)
    if deleted:
        logger.info("Retention sweep deleted %d terminal row(s) older than %d day(s)", deleted, days)
    return deleted


def watchdog_sweep(settings: Settings) -> int:
    """Fail the rows whose worker died mid-audit, and tell Cartograph.

    Nothing in this process re-runs such a row — its presigned links have
    expired by the time it is noticed — so it is terminalised, which is
    also what lets a fresh notification through: a failed row is no longer
    owned. Returns the number of rows acted on."""
    store = get_job_store(settings.audit_job_db)
    stale = settings.audit_watchdog_seconds
    wedged = store.list_wedged(_WEDGE_PRONE_STATES, stale_seconds=stale)
    if not wedged:
        return 0

    logger.warning("Watchdog: %d wedged job(s) detected (>%ds in %s)", len(wedged), stale, list(_WEDGE_PRONE_STATES))
    from app.services.cartograph.client import post_failure
    for job in wedged:
        case_id = job["case_id"]
        case_number = job.get("case_number") or case_id
        state = job["state"]
        age_seconds = int(time.time() - (job.get("updated_at") or 0))
        err = (
            f"Wedged in '{state}' state for {age_seconds // 60}m. The worker "
            f"died mid-flight — re-notify the case to audit it again."
        )
        logger.error("Watchdog: case_ref=%s wedged in %s for %ds — marking failed (re-notification required)",
                     case_number, state, age_seconds)
        store.mark_extraction_failed(case_id, err)
        if (job.get("source") or "") != "cartograph":
            continue        # a row from a retired integration; nobody to tell
        try:
            post_failure(case_number, err, settings, error_code="engine_error",
                         environment=job.get("environment"))
        except Exception:
            logger.exception("Watchdog: could not report wedged case_ref=%s to Cartograph", case_number)
    return len(wedged)


def output_sweep(settings: Settings) -> int:
    """Delete what the engine wrote under output_dir more than
    `output_retention_days` ago: the extraction JSON kept under results/,
    and a cartograph_<case_ref> work dir a crash left behind (a run deletes
    its own on the way out, so one older than the window is an orphan; a
    case still owned by the store is left alone regardless). The kept
    packets under pdfs/ are pruned by their own rule here too, so they age
    out even when no new case arrives to trigger it. Returns the number of
    entries removed; 0 days disables the sweep."""
    days = settings.output_retention_days
    if days <= 0:
        return 0
    root = Path(settings.output_dir)
    cutoff = time.time() - days * 86400
    removed = 0

    results = root / "results"
    if results.is_dir():
        for f in results.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1

    store = get_job_store(settings.audit_job_db)
    for d in root.glob("cartograph_*"):
        if not d.is_dir() or d.stat().st_mtime >= cutoff:
            continue
        row = store.get(d.name[len("cartograph_"):])
        if row and row.get("state") in _OWNED_STATES:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed += 1

    pdfs = root / "pdfs"
    if pdfs.is_dir() and settings.pdf_retention_days > 0:
        from app.services.cartograph.tasks import prune_packets
        removed += prune_packets(pdfs, settings.pdf_retention_days, settings.pdf_retention_max_mb)

    if removed:
        logger.info("Output sweep removed %d entr(y/ies) older than %d day(s) under %s", removed, days, root)
    return removed


# ---------------------------------------------------------------------------
# The maintenance thread
# ---------------------------------------------------------------------------

_MAINT_THREAD: threading.Thread | None = None
_MAINT_STOP: threading.Event | None = None


def _run_maintenance_loop(stop_event: threading.Event) -> None:
    settings = get_settings()
    interval = settings.audit_maintenance_interval_seconds
    logger.info("Maintenance thread started (rows=%dd, output=%dd, interval=%ds)",
                settings.audit_retention_days, settings.output_retention_days, interval)
    while not stop_event.is_set():
        try:
            retention_sweep(settings)
            output_sweep(settings)
            watchdog_sweep(settings)
        except Exception:
            logger.exception("Maintenance sweep failed — will retry next interval")
        if stop_event.wait(interval):
            return


def start_maintenance_thread() -> bool:
    """Start the maintenance thread on a daemon. Idempotent."""
    global _MAINT_THREAD, _MAINT_STOP
    if _MAINT_THREAD is not None and _MAINT_THREAD.is_alive():
        logger.warning("start_maintenance_thread: already running")
        return False
    _MAINT_STOP = threading.Event()
    _MAINT_THREAD = threading.Thread(
        target=_run_maintenance_loop, args=(_MAINT_STOP,), name="idp-audit-maintenance", daemon=True,
    )
    _MAINT_THREAD.start()
    return True


def stop_maintenance_thread(timeout: float = 5.0) -> None:
    global _MAINT_THREAD, _MAINT_STOP
    if _MAINT_STOP is None or _MAINT_THREAD is None:
        return
    _MAINT_STOP.set()
    _MAINT_THREAD.join(timeout=timeout)
    _MAINT_THREAD = None
