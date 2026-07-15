"""Background jobs for the IDP audit pipeline.

Two jobs:
  run_extraction(case_id) — webhook 1 trigger; downloads PDF + runs IDP.
  run_comparison(case_id) — webhook 2 trigger; pulls MuleSoft data + diffs.

Both are designed to be idempotent and safe to retry. State is persisted
in the JobStore so repeated webhooks don't double-run work.
"""
from __future__ import annotations

import logging
import time
import traceback
from datetime import date

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.services.audit.comparator import compare
from app.services.audit.formatter import format_findings
from app.services.audit.job_store import (
    COMPARING,
    DONE,
    EXTRACTED,
    EXTRACTING,
    EXTRACTION_FAILED,
    JobStore,
    get_job_store,
)
from app.services.pdf_service import process_pdf_full
from app.services.salesforce.client import get_salesforce_client

logger = logging.getLogger(__name__)


def _store(settings: Settings) -> JobStore:
    return get_job_store(settings.audit_job_db)


def _format_failure_marker(case_number: str, stage: str, error: str) -> str:
    """Render a clear failure message that gets written to
    IDP_Testing_Results__c when an audit can't complete. Salesforce sees
    a real entry instead of an empty field; the case drops out of the
    poller's SOQL once IDP_Audit_Complete__c flips to TRUE.
    """
    return (
        "--- AI FILE AUDIT FAILED ---\n"
        f"Case: {case_number}\n"
        f"Processed: {date.today().isoformat()}\n"
        f"Stage: {stage}\n"
        f"Error: {error}\n"
        "\n"
        "The audit could not be completed automatically. "
        "Manual review required."
    )


def _is_retryable_error(exc: BaseException) -> bool:
    """Return True if the exception looks transient — auto-retry on next cycle.

    Conservative: only known-transient classes (rate limits, server errors,
    network/timeout, billing). Permanent issues (auth, schema, bugs in our
    code) get the failure-marker treatment so they drop out of the queue
    instead of looping.

    Detected by both Anthropic SDK exception type AND error-message text,
    since some 400s carry billing/throttling info despite the strict type.
    """
    msg = str(exc).lower()

    # Anthropic SDK typed exceptions — most precise signal
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
            # Credit balance / quota exhaustion comes back as 400 — retryable
            # because adding funds restores service. Other 400s (schema,
            # malformed prompt) are bugs we should surface, not retry.
            return (
                "credit balance" in msg
                or "quota" in msg
                or "rate limit" in msg
            )
    except ImportError:
        pass

    # requests-based clients (simple_salesforce, OCR proxy, etc.) —
    # transient TCP/network failures. RemoteDisconnected from a stale
    # keep-alive socket surfaces here as ConnectionError, which the
    # message-substring path below would miss.
    try:
        import requests
        if isinstance(exc, (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        )):
            return True
    except ImportError:
        pass

    # Generic transient-error fingerprints (httpx/network/RunPod proxy)
    transient_markers = (
        "rate limit",
        "credit balance",
        "overloaded",
        "service unavailable",
        "gateway timeout",
        "timeout",
    )
    if any(marker in msg for marker in transient_markers):
        return True

    return False


def _finalize_case(
    case_id: str,
    case_number: str,
    settings: Settings,
    text_for_salesforce: str,
) -> None:
    """Write outcome to Salesforce. Local JobStore row is left intact so the
    dashboard can render the full audit history (findings + AI extraction +
    MuleSoft snapshot) without re-querying Salesforce per case.

    Used by both success and failure paths. Manual cleanup of completed rows
    is available via POST /admin/audit-jobs/reset {"mode":"done"}.
    """
    try:
        sf = get_salesforce_client(settings)
        sf.update_case_findings(case_id, text_for_salesforce)
    except Exception:
        logger.exception(
            "Salesforce writeback failed for case %s — local row kept in "
            "terminal state for inspection (no auto-retry)",
            case_id,
        )
        return

    logger.info("Salesforce writeback complete for case=%s", case_number)


# ---------------------------------------------------------------------------
# Watchdog — recover cases wedged in transient states
# ---------------------------------------------------------------------------

# States that should be transient. If a row sits in any of these past
# the watchdog threshold, the worker that started it almost certainly
# died (deploy mid-extraction, OOM, etc.) — we need to either retry it
# or surface it as failed.
_WEDGE_PRONE_STATES: tuple[str, ...] = (EXTRACTING, EXTRACTED, COMPARING)


def retention_sweep(settings: Settings) -> int:
    """Delete terminal-state rows older than audit_retention_days.

    No-op when retention is set to 0. Only touches done / *_failed /
    mulesoft_timeout rows — in-flight states are protected.
    """
    days = settings.audit_retention_days
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    deleted = _store(settings).delete_terminal_older_than(cutoff)
    if deleted:
        logger.info(
            "Retention sweep deleted %d terminal row(s) older than %d day(s)",
            deleted, days,
        )
    return deleted


def watchdog_sweep(settings: Settings) -> int:
    """Re-queue (or finalize as failed) jobs that have been wedged for too long.

    Why this is needed: when a service restart kills a worker mid-extraction,
    the JobStore row stays in `extracting`/`comparing` forever. The next
    poll cycle fetches the case (Salesforce flag still FALSE), upsert_pending
    sees the in-flight state and skips. The case is stuck.

    On every poll cycle, before fetching from Salesforce, this scans for
    rows older than `audit_watchdog_seconds` in wedge-prone states and:
      - retry_count < cap  -> reset row to PENDING (next poll re-runs)
      - retry_count >= cap -> write failure marker to SF + delete row

    Returns count of rows acted on.
    """
    store = _store(settings)
    stale = settings.audit_watchdog_seconds
    cap = settings.audit_watchdog_max_retries

    wedged = store.list_wedged(_WEDGE_PRONE_STATES, stale_seconds=stale)
    if not wedged:
        return 0

    logger.warning(
        "Watchdog: %d wedged job(s) detected (>%ds in %s)",
        len(wedged), stale, list(_WEDGE_PRONE_STATES),
    )

    for job in wedged:
        case_id = job["case_id"]
        case_number = job.get("case_number") or case_id
        retries = int(job.get("retry_count") or 0)
        state = job["state"]
        age_seconds = int(time.time() - (job.get("updated_at") or 0))

        if retries < cap:
            # Re-queue. retry_count auto-increments inside reset_to_pending.
            store.reset_to_pending(case_id)
            logger.warning(
                "Watchdog: re-queued case=%s (was %s for %ds, attempt %d/%d)",
                case_number, state, age_seconds, retries + 1, cap,
            )
        else:
            # Permanently failed — likely a broken case (giant PDF, OCR
            # service down, etc.). Write failure marker so it drops out
            # of the Salesforce queue and stops looping.
            err = (
                f"Wedged in '{state}' state for {age_seconds // 60}m after "
                f"{retries} retries. Worker died mid-flight repeatedly — "
                f"likely caused by service restarts during a slow extraction "
                f"or a permanently failing dependency."
            )
            logger.error(
                "Watchdog: case=%s exceeded retry cap (%d/%d) — finalizing as failed",
                case_number, retries, cap,
            )
            _finalize_case(
                case_id, case_number, settings,
                text_for_salesforce=_format_failure_marker(
                    case_number, state, err,
                ),
            )
            # Terminalize the local row. Without this it stays in a
            # wedge-prone state, gets re-listed by every sweep (re-writing
            # the SF marker each time), and the retention sweep — which
            # only deletes terminal states — can never remove it.
            if state == EXTRACTING:
                store.mark_extraction_failed(case_id, err)
            else:
                store.mark_comparison_failed(case_id, err)

    return len(wedged)


# ---------------------------------------------------------------------------
# Extraction job (triggered by webhook 1: PDF attached)
# ---------------------------------------------------------------------------

def run_extraction(case_id: str) -> None:
    """Download PDF, run IDP pipeline, store extraction result.

    Idempotent: if the job is already in extracting/extracted/comparing/done,
    this is a no-op. Run via FastAPI BackgroundTasks.
    """
    settings = get_settings()
    store = _store(settings)

    job = store.get(case_id)
    if job is None:
        logger.warning("run_extraction: no job entry for %s — aborting", case_id)
        return
    if job["state"] in (EXTRACTING, EXTRACTED, COMPARING, DONE):
        logger.info(
            "run_extraction: %s already in state %s — skipping",
            case_id, job["state"],
        )
        return

    sf = get_salesforce_client(settings)
    store.mark_extracting(case_id)

    try:
        # Resolve PDF: prefer the content_document_id from the webhook,
        # fall back to scanning the case for the most recent attachment.
        cd_id = job.get("content_document_id")
        if cd_id:
            pdf_bytes = sf.download_pdf(cd_id)
        else:
            pdf_bytes, cd_id = sf.get_pdf_for_case(case_id)
            logger.info("Resolved PDF for %s via case scan: %s", case_id, cd_id)

        cert_type = job.get("cert_type") or None
        funding = job.get("funding_program") or None

        logger.info(
            "Running IDP extraction for case=%s cert=%s funding=%s",
            job.get("case_number"), cert_type, funding,
        )
        result = process_pdf_full(
            pdf_bytes,
            settings,
            funding_program=funding,
            certification_type=cert_type,
        )

        # Convert pydantic ExtractionResult to dict for storage
        extraction = result["extraction"]
        if hasattr(extraction, "model_dump"):
            extraction_dict = extraction.model_dump()
        else:
            extraction_dict = extraction

        store.mark_extracted(case_id, extraction_dict)
        logger.info(
            "Extraction complete for %s — %d pages, overall_flag=%s",
            case_id,
            len((extraction_dict.get("classification") or {}).get("pages") or []),
            (extraction_dict.get("field_scores") or {}).get("overall_flag"),
        )

        # If MuleSoft already finished (webhook 2 arrived during extraction),
        # the job will have mulesoft_done_at set — kick off comparison now.
        refreshed = store.get(case_id)
        if refreshed and refreshed.get("mulesoft_done_at"):
            run_comparison(case_id)

    except Exception as exc:
        logger.exception("Extraction failed for case %s", case_id)
        error_str = f"{type(exc).__name__}: {exc}"
        store.mark_extraction_failed(case_id, error_str)
        case_number = (job.get("case_number") if job else None) or case_id

        if _is_retryable_error(exc):
            # Transient (rate limit, credit balance, server error, timeout).
            # Don't write a failure marker or flip IDP_Audit_Complete__c —
            # leave the SF flag FALSE so the next poll cycle re-fetches and
            # tries again — but under the shared retry budget. Without a
            # cap, a deterministically-"transient" failure (e.g. a PDF that
            # always times out) re-runs at full OCR+LLM cost every poll
            # tick forever, and because failures never advance the case's
            # LastModifiedDate, it pins the ORDER BY ... ASC poll batch and
            # starves newer cases.
            attempts = store.increment_retry(case_id)
            cap = settings.audit_watchdog_max_retries
            if attempts < cap:
                logger.warning(
                    "Retryable extraction error for case=%s (attempt %d/%d) "
                    "— leaving for next poll cycle (no Salesforce "
                    "writeback): %s",
                    case_number, attempts, cap, error_str,
                )
                return
            logger.error(
                "Retryable extraction error for case=%s exceeded retry cap "
                "(%d/%d) — finalizing as permanently failed",
                case_number, attempts, cap,
            )

        # Permanent failure — write marker + flip flag. Local row remains
        # in EXTRACTION_FAILED state so the dashboard can surface it.
        _finalize_case(
            case_id, case_number, settings,
            text_for_salesforce=_format_failure_marker(
                case_number, "extraction", error_str,
            ),
        )


# ---------------------------------------------------------------------------
# Unified audit job (triggered by /webhook/audit-ready when MuleSoft is done)
# ---------------------------------------------------------------------------

def run_audit(case_id: str) -> None:
    """End-to-end audit chain: extraction -> comparison -> writeback.

    Used by the single-webhook design where the trigger fires after
    MuleSoft finishes. We pre-mark mulesoft_done so run_extraction
    auto-chains to run_comparison once extraction completes — no
    separate signal, no racing logic, no waiting loop.

    Caller must have already created the JobStore row via
    upsert_pending() (the webhook handler does this).
    """
    settings = get_settings()
    store = _store(settings)

    # Setting mulesoft_done_at BEFORE extraction means the auto-chain at
    # the end of run_extraction will fire run_comparison without needing
    # a separate webhook 2 signal.
    store.mark_mulesoft_done(case_id)

    run_extraction(case_id)
    # run_extraction sees mulesoft_done_at is set and calls run_comparison
    # internally. run_comparison handles writeback. Nothing more to do.


# ---------------------------------------------------------------------------
# Comparison job (triggered by webhook 2: MuleSoft done — legacy 2-webhook flow)
# ---------------------------------------------------------------------------

def run_comparison(case_id: str) -> None:
    """Pull MuleSoft data, compare against stored IDP extraction, build findings.

    If the IDP extraction isn't done yet, polls until it completes (or
    times out after audit_extraction_wait_seconds).

    Note: writing findings back to Salesforce is intentionally NOT done here
    yet — the formatted text is logged and stored in JobStore. Once the
    Salesforce writeback is approved, add the sf.update() call at the bottom.
    """
    settings = get_settings()
    store = _store(settings)

    # Mark MuleSoft signal received (does NOT change state — just records timing)
    store.mark_mulesoft_done(case_id)

    job = store.get(case_id)
    if job is None:
        logger.warning("run_comparison: no job entry for %s — aborting", case_id)
        return
    if job["state"] in (COMPARING, DONE):
        logger.info(
            "run_comparison: %s already in state %s — skipping",
            case_id, job["state"],
        )
        return

    # Wait for extraction to complete
    if job["state"] != EXTRACTED:
        deadline = time.time() + settings.audit_extraction_wait_seconds
        logger.info(
            "run_comparison: %s not yet extracted (state=%s) — waiting",
            case_id, job["state"],
        )
        while time.time() < deadline:
            time.sleep(settings.audit_extraction_poll_seconds)
            job = store.get(case_id)
            if job is None:
                return
            if job["state"] == EXTRACTED:
                break
            if job["state"] == EXTRACTION_FAILED:
                logger.error(
                    "run_comparison: extraction failed for %s — cannot compare",
                    case_id,
                )
                return
        else:
            logger.warning(
                "run_comparison: extraction wait timeout for %s (state=%s)",
                case_id, job["state"],
            )
            return

    # Atomic claim — prevents duplicate comparisons when webhook 2 and
    # the post-extraction trigger race. Only one caller wins.
    if not store.try_claim_for_comparison(case_id):
        logger.info(
            "run_comparison: another worker claimed comparison for %s — "
            "this caller bails out", case_id,
        )
        return

    try:
        sf = get_salesforce_client(settings)
        sf_data = sf.get_case_audit_data(case_id)

        extraction = job["extraction_result"]
        if not isinstance(extraction, dict):
            raise ValueError("Stored extraction is not a dict")

        comparison = compare(extraction, sf_data)
        case_number = job.get("case_number") or case_id

        findings_text = format_findings(
            comparison, case_number=case_number, processed_date=date.today(),
        )

        store.mark_done(
            case_id,
            findings_text=findings_text,
            confidence=comparison.confidence.case_confidence,
            mulesoft_snapshot=sf_data,
        )

        logger.info(
            "Audit complete for case=%s confidence=%.2f flag=%s findings=%d",
            case_number,
            comparison.confidence.case_confidence,
            comparison.confidence.flag,
            len(comparison.findings),
        )

        # Print the full findings to the console for the rollout window —
        # makes it easy to inspect output without opening Salesforce or
        # hitting the GET endpoint. Remove once the integration is stable.
        logger.info(
            "Findings for case %s:\n%s\n%s\n%s",
            case_number, "=" * 60, findings_text, "=" * 60,
        )

        # Push findings to Salesforce. Local row stays so the dashboard
        # can keep rendering the AI extraction + MuleSoft snapshot for
        # this case. _finalize_case logs and bails out if SF write fails.
        _finalize_case(
            case_id, case_number, settings,
            text_for_salesforce=findings_text,
        )

    except Exception as exc:
        logger.exception("Comparison failed for case %s", case_id)
        error_str = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        store.mark_comparison_failed(case_id, error_str)
        case_number = (job.get("case_number") if job else None) or case_id

        if _is_retryable_error(exc):
            # Same bounded retry budget as the extraction path — see the
            # comment there for why an uncapped retryable loop starves the
            # poll batch.
            attempts = store.increment_retry(case_id)
            cap = settings.audit_watchdog_max_retries
            if attempts < cap:
                logger.warning(
                    "Retryable comparison error for case=%s (attempt %d/%d) "
                    "— leaving for next poll cycle (no Salesforce "
                    "writeback): %s",
                    case_number, attempts, cap,
                    f"{type(exc).__name__}: {exc}",
                )
                return
            logger.error(
                "Retryable comparison error for case=%s exceeded retry cap "
                "(%d/%d) — finalizing as permanently failed",
                case_number, attempts, cap,
            )

        _finalize_case(
            case_id, case_number, settings,
            text_for_salesforce=_format_failure_marker(
                case_number, "comparison", f"{type(exc).__name__}: {exc}",
            ),
        )
