"""The background chain for a case uploaded by Cartograph.

Extract, build the payload, deliver it. This is the piece that joins the
three parts that already existed separately — the extraction pipeline, the
payload adapter, and the signed outbound client.

Deliberately no Salesforce anywhere in this path. The equivalent chain in
`audit/jobs.py` downloads from Salesforce, compares against MuleSoft, and
carries a decade of Salesforce-shaped assumptions. This one starts from bytes
in hand and ends at an HTTP POST, and it is what survives the migration.
"""

import logging
import shutil

from app.core.dependencies import get_settings
from app.services.audit.job_store import get_job_store
from app.services.cartograph.adapter import build_payload
from app.services.cartograph.client import (
    CartographNotConfigured,
    is_configured,
    post_extraction,
)
from app.services.pdf_service import process_pdf_full

logger = logging.getLogger(__name__)


def audit_uploaded_case(
    *,
    pdf_bytes: bytes,
    case_ref: str,
    cert_type: str | None = None,
    program: str | None = None,
    job_id: int | None = None,
    community_id: int | None = None,
    unit_number: str | None = None,
    effective_date: str | None = None,
) -> None:
    """Run one uploaded packet end to end.

    Every failure is recorded on the job row rather than only logged. A case
    that fails silently here is a case Cartograph is still waiting on, and
    nobody discovers it until a reviewer opens a blank checklist.
    """
    settings = get_settings()
    store = get_job_store(settings.audit_job_db)

    store.mark_extracting(case_ref)

    # Per-job work dir: page images and texts are named by page number only,
    # so concurrent extractions must not share one.
    work_dir = settings.output_dir / f"cartograph_{case_ref}"
    try:
        result = process_pdf_full(
            pdf_bytes,
            settings,
            funding_program=program,
            certification_type=cert_type,
            work_dir=work_dir,
        )
    except Exception as exc:
        logger.exception("Extraction failed for case_ref=%s", case_ref)
        store.mark_extraction_failed(case_ref, str(exc))
        return
    finally:
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

    if not response["ok"]:
        # Their ingest rejected it. The payload is the evidence, so record
        # enough to reproduce without re-running the extraction.
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
