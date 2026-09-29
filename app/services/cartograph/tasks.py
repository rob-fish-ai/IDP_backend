"""The background chain for a case Cartograph has flagged as audit-ready.

Fetch, extract, adapt, deliver. This joins the four parts that already
existed separately — the document fetcher, the extraction pipeline, the
payload adapter, and the signed outbound client.

The notification that starts this returned 202 long before any of it runs.
Cartograph holds no connection open; the result reaches it later as a signed
POST to its ingest endpoint.

Deliberately no Salesforce anywhere in this path. The equivalent chain in
`audit/jobs.py` downloads from Salesforce, compares against MuleSoft, and
carries a decade of Salesforce-shaped assumptions. This one starts from a
URL and ends at an HTTP POST, and it is what survives the migration.
"""

import logging
from pathlib import Path
import shutil
from datetime import datetime

from app.core.dependencies import get_settings
from app.services.audit.job_store import get_job_store
from app.services.cartograph.adapter import build_payload, cert_type_from_cartograph
from app.services.cartograph.client import (
    CartographNotConfigured,
    is_configured,
    post_extraction,
    post_failure,
)
from app.services.cartograph.documents import DocumentUnavailable, fetch_packet
from app.services.pdf_service import process_pdf_full
from app.services.audit.jobs import is_retryable_error

logger = logging.getLogger(__name__)


def run_with_retry(fn, *, attempts: int, delay: float, is_retryable, on_retry=None):
    """Call fn; on a transient failure wait and call it again, up to
    `attempts` calls in all. A permanent failure, or the last transient
    one, is raised as it came."""
    import time
    attempts = max(1, int(attempts or 1))
    for n in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if n >= attempts or not is_retryable(exc):
                raise
            if on_retry:
                on_retry(n, exc)
            if delay > 0:
                time.sleep(delay)


def keep_packet(pdf_bytes: bytes, case_ref: str, settings) -> Path | None:
    """Write the packet under output_dir/pdfs and prune the folder.

    Kept for `pdf_retention_days` days and at most `pdf_retention_max_mb`
    in total, oldest first. Returns the path written, or None when keeping
    is disabled."""
    if not settings.pdf_retention_days or settings.pdf_retention_days <= 0:
        return None
    folder = Path(settings.output_dir) / "pdfs"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"{case_ref}_{stamp}.pdf"
    path.write_bytes(pdf_bytes)
    prune_packets(folder, settings.pdf_retention_days, settings.pdf_retention_max_mb)
    return path


def prune_packets(folder: Path, days: int, max_mb: int) -> int:
    """Delete packets older than `days`, then the oldest until the folder
    is within `max_mb`. Returns the number deleted."""
    import time
    files = sorted(folder.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - days * 86400
    deleted = 0
    for f in list(files):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            files.remove(f)
            deleted += 1
    total = sum(f.stat().st_size for f in files)
    while files and total > max_mb * 1024 * 1024:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)
        deleted += 1
    if deleted:
        logger.info("Packet retention: removed %d packet(s) from %s", deleted, folder)
    return deleted


def audit_case(
    *,
    case_ref: str,
    documents: list[dict],
    cert_type: str | None = None,
    program: str | None = None,
    job_id: int | None = None,
    community_id: int | None = None,
    unit_number: str | None = None,
    effective_date: str | None = None,
) -> None:
    """Run one notified case end to end.

    Every failure is recorded on the job row and reported to Cartograph
    rather than only logged. A case that fails silently here is a case
    Cartograph is still waiting on, and nobody discovers it until a reviewer
    opens a blank checklist. The inner function handles the failures it
    expects; this wrapper catches whatever it did not, because an
    exception that escapes a background task leaves the job row in
    "extracting" and every re-notification is then ignored as a duplicate
    (a PNG attachment did exactly that on J-CCAC-06750).
    """
    settings = get_settings()
    store = get_job_store(settings.audit_job_db)
    try:
        _audit_case(
            store, settings, case_ref=case_ref, documents=documents, cert_type=cert_type,
            program=program, job_id=job_id, community_id=community_id,
            unit_number=unit_number, effective_date=effective_date,
        )
    except Exception as exc:
        logger.exception("Unhandled failure auditing case_ref=%s", case_ref)
        row = store.get(case_ref) or {}
        reason = f"unexpected error: {exc}"
        if row.get("state") in (None, "pending", "queued", "extracting"):
            store.mark_extraction_failed(case_ref, reason)
        else:
            store.mark_comparison_failed(case_ref, reason)
        try:
            post_failure(case_ref, reason, settings, error_code="engine_error")
        except Exception:
            logger.exception("Could not report the failure of case_ref=%s to Cartograph", case_ref)


def _audit_case(
    store,
    settings,
    *,
    case_ref: str,
    documents: list[dict],
    cert_type: str | None,
    program: str | None,
    job_id: int | None,
    community_id: int | None,
    unit_number: str | None,
    effective_date: str | None,
) -> None:

    # Their vocabulary, translated before it reaches anything that keys on
    # ours. An untranslated "annual" disables every cert-type rule, and
    # AR-SC is the type where that costs most: its whole rule set exists
    # because the certification form is the source of truth and no
    # third-party wage verification is expected.
    engine_cert_type = cert_type_from_cartograph(cert_type)
    if cert_type and engine_cert_type is None:
        logger.warning(
            "case_ref=%s arrived with cert_type=%r, which is not a type the "
            "engine recognizes; determining it from the documents instead",
            case_ref, cert_type,
        )

    store.mark_extracting(case_ref)

    # Fetch first, before anything expensive. Presigned URLs expire, so the
    # window between the notification and the download is kept as small as
    # the queue allows.
    try:
        pdf_bytes, packet_warnings = fetch_packet(documents)
    except DocumentUnavailable as exc:
        logger.error("case_ref=%s document unavailable: %s", case_ref, exc)
        store.mark_extraction_failed(case_ref, f"document unavailable: {exc}")
        # Recoverable on their side: reissue the link and notify again.
        post_failure(case_ref, str(exc), settings,
                     error_code="document_unavailable")
        return
    for warning in packet_warnings:
        logger.warning("case_ref=%s packet warning: %s", case_ref, warning)

    # The packet itself is kept for a while: every contested finding so
    # far has needed the rendered page, and the client had to be asked for
    # the file each time.
    try:
        keep_packet(pdf_bytes, case_ref, settings)
    except Exception:
        logger.exception("Could not keep the packet for case_ref=%s", case_ref)

    # Per-job work dir: page images and texts are named by page number only,
    # so concurrent extractions must not share one.
    work_dir = settings.output_dir / f"cartograph_{case_ref}"
    try:
        result = run_with_retry(
            lambda: process_pdf_full(
                pdf_bytes,
                settings,
                funding_program=program,
                certification_type=engine_cert_type,
                work_dir=work_dir,
                # Proves the case is alive while it runs. A packet scanned
                # sideways takes tens of minutes legitimately, and without this
                # the watchdog cannot tell that from a dead worker — it would
                # fail a running case, which releases the dedupe and lets a
                # re-notification audit the same case twice.
                heartbeat=lambda: store.touch(case_ref),
            ),
            attempts=settings.cartograph_extract_attempts,
            delay=settings.cartograph_retry_delay_seconds,
            is_retryable=is_retryable_error,
            on_retry=lambda n, exc: (
                logger.warning("Extraction attempt %d for case_ref=%s failed for a transient reason (%s) — retrying",
                               n, case_ref, exc),
                store.touch(case_ref),
            ),
        )
    except Exception as exc:
        logger.exception("Extraction failed for case_ref=%s", case_ref)
        store.mark_extraction_failed(case_ref, str(exc))
        post_failure(case_ref, str(exc), settings, error_code="extraction_failed")
        return
    finally:
        # The work directory (page renders, OCR text) is deleted, but the
        # extraction JSON is kept under output/results/ so a run can be
        # inspected as a file after the fact; the job store holds it too.
        try:
            src = work_dir / "extraction_result.json"
            if src.exists():
                keep_dir = settings.output_dir / "results"
                keep_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(src, keep_dir / f"{case_ref}_{stamp}.json")
        except Exception:
            logger.exception("Could not keep the extraction result for case_ref=%s", case_ref)
        shutil.rmtree(work_dir, ignore_errors=True)

    extraction = result["extraction"]
    extraction_dict = (
        extraction.model_dump()
        if hasattr(extraction, "model_dump") else extraction
    )
    store.mark_extracted(case_ref, extraction_dict)

    adapted = build_payload(
        extraction,
        settings,
        case_ref=case_ref,
        job_id=job_id,
        community_id=community_id,
        unit_number=unit_number,
    )

    # An attachment left out of the packet is something the reviewer must
    # know about: the audit covered less than was sent.
    adapted.warnings.extend(packet_warnings)
    logger.info(
        "Extraction complete case_ref=%s members=%d warnings=%d",
        case_ref, adapted.member_count, len(adapted.warnings),
    )
    for warning in adapted.warnings:
        logger.warning("case_ref=%s payload warning: %s", case_ref, warning)

    if not is_configured(settings):
        # Extraction is finished and stored, so nothing is lost — but the
        # result has nowhere to go. Say so plainly rather than leaving the
        # job looking complete.
        logger.error(
            "case_ref=%s extracted but not delivered: "
            "IDP_CARTOGRAPH_INGEST_URL / _SECRET are not configured",
            case_ref,
        )
        store.mark_comparison_failed(
            case_ref, "Cartograph delivery not configured"
        )
        return

    try:
        response = post_extraction(adapted.payload, settings)
    except CartographNotConfigured as exc:
        logger.error("case_ref=%s delivery skipped: %s", case_ref, exc)
        store.mark_comparison_failed(case_ref, str(exc))
        return
    except Exception as exc:
        logger.exception("Delivery to Cartograph failed for case_ref=%s", case_ref)
        store.mark_comparison_failed(case_ref, f"delivery failed: {exc}")
        return

    # Recorded before the outcome is judged, so a rejection keeps the reason
    # their endpoint gave rather than only the status code.
    store.record_delivery(case_ref, response)

    if not response["ok"]:
        # Their ingest rejected it. Record enough to reproduce without
        # re-running the extraction.
        store.mark_comparison_failed(
            case_ref,
            f"ingest rejected: HTTP {response['status_code']} "
            f"{response['response']}",
        )
        return

    # Record the findings on the job row too. Cartograph holds the structured
    # copy, but the job store is what /audit/cases and the analyst export
    # read, and a delivered case with a blank row looks like a failed one.
    #
    # Confidence here is extraction quality alone. The old score blended it
    # with agreement against MuleSoft; that term has no counterparty on this
    # path, so it is left out rather than silently reweighted.
    scores = getattr(extraction, "field_scores", None)
    store.mark_done(
        case_ref,
        findings_text="\n".join(extraction.findings),
        confidence=scores.overall_composite if scores else 0.0,
    )
    logger.info(
        "case_ref=%s delivered to Cartograph (%d bytes)",
        case_ref, response["bytes_sent"],
    )
