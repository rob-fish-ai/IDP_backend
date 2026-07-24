"""Polling-mode trigger for the IDP audit pipeline.

Alternative to webhooks: instead of Salesforce pushing signals to IDP,
IDP queries Salesforce on a fixed interval for cases that are ready
(MuleSoft processing complete) and runs the audit on each.

In polling mode there's no separate "webhook 2" — by the time the poller
finds a case, MuleSoft is already done. So extraction and comparison run
back-to-back as part of one job.

Two ways to run:
  1. Standalone — `python -m scripts.run_audit_poller` (separate process)
  2. Embedded — auto-started inside FastAPI lifespan when audit_mode is
     "poll" or "both". See `start_poller_thread` and `stop_poller_thread`.
"""
from __future__ import annotations

import logging
import threading
import time

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.services.audit.job_store import EXTRACTED, get_job_store
from app.services.audit.jobs import (
    retention_sweep,
    revisit_failed_cases,
    run_comparison,
    run_extraction,
    watchdog_sweep,
)
from app.services.salesforce.client import _escape_soql, get_salesforce_client

logger = logging.getLogger(__name__)


# IDP only audits these cert types. Anything else on Case.CertType__c —
# blank stubs, "Certification Review", "IC", and any future values — is
# out of scope and must NOT enter the audit pipeline. Filtered at SOQL
# level (poll mode) and validated at the webhook (push mode).
SUPPORTED_CERT_TYPES: frozenset[str] = frozenset({"MI", "AR", "AR-SC", "IR"})


def _build_ready_query(limit: int) -> str:
    """Build the SOQL query for cases ready for IDP audit.

    Salesforce-side flow (per Scott):
      Status = 'Certification Approval'
        -> creates Certification_Review__c, sets IDP_File_Process_Status__c
           = 'Ready to Process' (calls MuleSoft)
      MuleSoft returns
        -> Meez_Review_Status__c = 'Ready for Review'
        -> 5-min cooldown via IDP_Review_Clean_Up_Time__c
        -> IDP_Phase_1_Review__c = TRUE   <<< definitive 'audit ready'

    So `IDP_Phase_1_Review__c = TRUE` is the right entry signal — by then
    MuleSoft has settled. `IDP_Audit_Complete__c = FALSE` excludes cases
    we've already written findings for (set by writeback). CertType__c
    filter limits to the 4 supported cert types.
    """
    cert_list = ", ".join(f"'{ct}'" for ct in sorted(SUPPORTED_CERT_TYPES))
    return f"""
SELECT Id, CaseNumber, CertType__c, Funding_Program2__c
FROM Case
WHERE IDP_Phase_1_Review__c = TRUE
  AND IDP_Audit_Complete__c = FALSE
  AND CertType__c IN ({cert_list})
ORDER BY LastModifiedDate ASC
LIMIT {limit}
""".strip()


def fetch_ready_cases(settings: Settings, limit: int) -> list[dict]:
    """Query Salesforce for cases ready for IDP audit."""
    sf = get_salesforce_client(settings)
    result = sf.sf.query(_build_ready_query(limit))
    return result.get("records", [])


def process_case(case_record: dict, settings: Settings) -> None:
    """Run the full audit for one case: extraction → comparison.

    Sequential by design — in poll mode there's no separate MuleSoft signal,
    so we trigger comparison immediately after extraction completes.
    """
    case_id = case_record["Id"]
    case_number = case_record.get("CaseNumber")
    cert_type = case_record.get("CertType__c")
    funding = case_record.get("Funding_Program2__c")

    # CertType__c gating — only audit MI / AR / AR-SC / IR cases. Anything
    # else (missing, "Certification Review", "IC", etc.) is out of scope.
    # The poller's SOQL filter handles the common case; this branch is
    # defense-in-depth for SOQL drift or direct calls bypassing the poller.
    if cert_type not in SUPPORTED_CERT_TYPES:
        logger.info(
            "Skipping case=%s — CertType__c=%r not in supported set %s",
            case_number or case_id, cert_type, sorted(SUPPORTED_CERT_TYPES),
        )
        return

    logger.info(
        "Polling: processing case=%s cert=%s funding=%s",
        case_number, cert_type, funding,
    )

    store = get_job_store(settings.audit_job_db)
    upsert = store.upsert_pending(
        case_id=case_id,
        case_number=case_number,
        cert_type=cert_type,
        funding_program=funding,
        content_document_id=None,    # poller resolves PDF via case scan
    )
    if upsert.get("deduplicated"):
        logger.info("Case %s already in progress — skipping", case_number)
        return

    # In poll mode MuleSoft is already done — record the signal so the
    # post-extraction comparison fires automatically.
    store.mark_mulesoft_done(case_id)

    try:
        run_extraction(case_id)
    except Exception:
        logger.exception("Extraction failed for case %s in poll mode", case_number)
        return

    # Extraction triggers comparison automatically when mulesoft_done_at
    # was already set (see run_extraction). If for some reason it didn't,
    # call comparison explicitly as a safety net.
    refreshed = store.get(case_id)
    if refreshed and refreshed["state"] == EXTRACTED:
        run_comparison(case_id)


def poll_once(settings: Settings) -> int:
    """Run one polling cycle. Returns count of cases processed.

    Dedup is handled by Salesforce — the SOQL filter excludes cases where
    IDP_Audit_Complete__c=TRUE. process_case() then defers to JobStore's
    upsert_pending for in-flight dedup. Writeback failures auto-retry on
    the next cycle (the SF flag stays FALSE until writeback succeeds).

    Before fetching new work, the watchdog re-queues (or finalizes) any
    rows wedged in extracting/extracted/comparing past the threshold.
    """
    # Watchdog first — recover rows wedged by previous worker deaths so
    # they don't permanently block the same case_ids from progressing.
    try:
        recovered = watchdog_sweep(settings)
        if recovered:
            logger.info("Watchdog acted on %d wedged job(s)", recovered)
    except Exception:
        logger.exception("Watchdog sweep failed — continuing with poll cycle")

    cases = fetch_ready_cases(
        settings,
        settings.audit_poll_batch_size,
    )
    if not cases:
        return 0

    logger.info("Poll cycle: fetched %d ready case(s)", len(cases))

    # No local pre-filter needed — Salesforce's IDP_Audit_Complete__c
    # filter excludes already-audited cases, and process_case() defers
    # to JobStore.upsert_pending for in-flight dedup. If a writeback
    # previously failed, the SOQL re-fetches and we auto-retry.
    #
    # Cases run concurrently (bounded pool): pipelines are network-bound
    # (external OCR, LLM calls), so one large packet no longer serializes
    # the whole batch behind it.
    def _one(case: dict) -> bool:
        try:
            process_case(case, settings)
            return True
        except Exception:
            logger.exception(
                "Unhandled error processing case %s",
                case.get("CaseNumber") or case.get("Id"),
            )
            return False

    workers = max(1, settings.audit_pipeline_concurrency)
    if workers == 1 or len(cases) == 1:
        return sum(_one(c) for c in cases)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(_one, cases))


def run_poll_loop(stop_event: threading.Event | None = None) -> None:
    """Main entry point — runs until stop_event is set (or forever).

    When called from a standalone script, leave stop_event as None — the
    loop runs until process termination. When called from FastAPI lifespan,
    pass a threading.Event so shutdown can request a clean exit.
    """
    settings = get_settings()
    interval = settings.audit_poll_interval_seconds
    logger.info(
        "IDP audit poller started (mode=%s, interval=%ds, batch=%d)",
        settings.audit_mode, interval, settings.audit_poll_batch_size,
    )

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("Poller stop requested — exiting loop")
            return
        try:
            n = poll_once(settings)
            if n > 0:
                logger.info("Poll cycle processed %d case(s)", n)
        except Exception:
            logger.exception("Poll cycle failed")

        # Sleep in short ticks so a stop request is respected promptly
        # rather than waiting up to a full interval.
        if stop_event is not None:
            if stop_event.wait(interval):
                logger.info("Poller stop requested during sleep — exiting")
                return
        else:
            time.sleep(interval)


# ---------------------------------------------------------------------------
# Embedded mode — start/stop the poller as a background thread inside
# the FastAPI process so a single Render service can serve both webhooks
# AND poll Salesforce on a schedule.
# ---------------------------------------------------------------------------

_THREAD: threading.Thread | None = None
_STOP_EVENT: threading.Event | None = None


def start_poller_thread() -> bool:
    """Start the poll loop on a daemon thread. Idempotent.

    Returns True if a new thread was started, False if one was already
    running or the configured mode does not enable polling.
    """
    global _THREAD, _STOP_EVENT

    settings = get_settings()
    if settings.audit_mode not in ("poll", "both"):
        logger.info(
            "Poller not started (audit_mode=%s) — set IDP_AUDIT_MODE=poll "
            "or =both to enable in-process polling",
            settings.audit_mode,
        )
        return False

    if _THREAD is not None and _THREAD.is_alive():
        logger.warning("start_poller_thread called but poller already running")
        return False

    _STOP_EVENT = threading.Event()
    _THREAD = threading.Thread(
        target=run_poll_loop,
        args=(_STOP_EVENT,),
        name="idp-audit-poller",
        daemon=True,
    )
    _THREAD.start()
    logger.info("Audit poller thread started (daemon)")
    return True


def stop_poller_thread(timeout: float = 5.0) -> None:
    """Signal the poll loop to stop and wait briefly for it to exit."""
    global _THREAD, _STOP_EVENT
    if _STOP_EVENT is None or _THREAD is None:
        return
    _STOP_EVENT.set()
    _THREAD.join(timeout=timeout)
    if _THREAD.is_alive():
        logger.warning(
            "Poller thread did not exit within %.1fs — leaving as daemon",
            timeout,
        )
    else:
        logger.info("Audit poller thread stopped cleanly")
    _THREAD = None


# ---------------------------------------------------------------------------
# Maintenance thread — retention cleanup + watchdog. Runs regardless of
# audit_mode so pure-webhook deployments still get DB pruning and wedge
# recovery (the poller thread only runs in poll/both modes).
# ---------------------------------------------------------------------------

_MAINT_THREAD: threading.Thread | None = None
_MAINT_STOP: threading.Event | None = None


def _run_maintenance_loop(stop_event: threading.Event) -> None:
    settings = get_settings()
    interval = settings.audit_maintenance_interval_seconds
    logger.info(
        "Maintenance thread started (retention=%dd, interval=%ds)",
        settings.audit_retention_days, interval,
    )
    while not stop_event.is_set():
        try:
            retention_sweep(settings)
            watchdog_sweep(settings)
            revisit_failed_cases(settings)
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
        target=_run_maintenance_loop,
        args=(_MAINT_STOP,),
        name="idp-audit-maintenance",
        daemon=True,
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
    _STOP_EVENT = None
