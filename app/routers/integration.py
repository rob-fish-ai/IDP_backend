"""Cartograph-facing endpoints.

Separate from the Salesforce webhook router on purpose. Salesforce and
MuleSoft retire at the end of 2026, and when they do, `webhook.py` deletes
almost entirely while this file stands alone unchanged. Keeping the two
integrations in one module would make that a surgical edit instead of a
deletion.

Two endpoints, one per direction:

  POST /integration/case           Cartograph signals a case is ready.
                                   HMAC auth, JSON with document URLs, 202
                                   immediately. The engine fetches the files
                                   itself.
  POST /integration/import_result  Cartograph reports how the import went.
                                   HMAC auth, JSON.

The delivery leg — engine to Cartograph — is not here. It is an outbound
call, made by the background task in `cartograph.client`.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import verify_cartograph_signature
from app.core.config import Settings
from app.core.dependencies import get_settings
from app.services.audit.job_store import get_job_store
from app.services.cartograph.tasks import audit_case

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integration", tags=["Cartograph"])


# ---------------------------------------------------------------------------
# Cartograph -> Engine: a case is ready to audit
# ---------------------------------------------------------------------------

class CaseDocument(BaseModel):
    """One file belonging to a case."""
    url: str = Field(..., description="Presigned URL, fetched on receipt")
    filename: str | None = None
    job_document_id: int | None = None
    document_class: str | None = None


class AuditRequest(BaseModel):
    """What Cartograph sends when a case is ready to audit."""
    case_ref: str = Field(..., description="Cartograph Job#ref_number")
    documents: list[CaseDocument] = Field(..., min_length=1)
    event_type: str = "audit_request"
    schema_version: str = "1.0"
    cert_type: str | None = Field(None, description="MI, AR, AR-SC or IR")
    program: str | None = Field(None, description="Funding program")
    job_id: int | None = None
    community_id: int | None = None
    unit_number: str | None = None
    effective_date: str | None = None


@router.post(
    "/case",
    status_code=202,
    dependencies=[Depends(verify_cartograph_signature)],
)
async def receive_case(
    payload: AuditRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Accept an audit notification, acknowledge, and work in the background.

    Returns 202 without doing anything expensive. An extraction takes
    minutes — OCR on every page plus several model calls — and Cartograph
    holds no connection open for it. The result arrives later as a signed
    POST to their ingest endpoint, not in the response to this call.

    The engine fetches the documents itself as the first act of the
    background task, so the presigned URLs only need to outlive the queue
    rather than the whole audit.
    """
    # Idempotency lives in the job store rather than in the signature window.
    # Cartograph is expected to retry, and a retry is indistinguishable from
    # a replay — both should cost one audit, not two.
    store = get_job_store(settings.audit_job_db)
    upsert = store.upsert_pending(
        case_id=payload.case_ref,
        case_number=payload.case_ref,
        cert_type=payload.cert_type,
        funding_program=payload.program,
        content_document_id=None,
        # Marks the row so Salesforce-only maintenance skips it. A
        # Cartograph case_ref is not a Salesforce Case Id.
        source="cartograph",
    )
    if upsert.get("deduplicated"):
        logger.info(
            "Case %s already in state %s — ignoring duplicate notification",
            payload.case_ref, upsert.get("state"),
        )
        return {
            "status": "already_in_progress",
            "case_ref": payload.case_ref,
            "state": upsert.get("state"),
        }

    logger.info(
        "Cartograph audit request case_ref=%s cert=%s program=%s documents=%d",
        payload.case_ref, payload.cert_type, payload.program,
        len(payload.documents),
    )

    background_tasks.add_task(
        audit_case,
        case_ref=payload.case_ref,
        documents=[d.model_dump() for d in payload.documents],
        cert_type=payload.cert_type,
        program=payload.program,
        job_id=payload.job_id,
        community_id=payload.community_id,
        unit_number=payload.unit_number,
        effective_date=payload.effective_date,
    )

    return {"status": "accepted", "case_ref": payload.case_ref}


# ---------------------------------------------------------------------------
# Cartograph -> Engine: how the import went
# ---------------------------------------------------------------------------

@router.post(
    "/import_result",
    status_code=200,
    dependencies=[Depends(verify_cartograph_signature)],
)
async def cartograph_import_result(request: Request) -> dict:
    """Receive the outcome of a Cartograph import.

    Cartograph's ingest endpoint returns 202 before its background job runs
    the inserts, so the HTTP response to our POST cannot say what happened.
    Without this callback an import that fails validation on their side
    fails silently on ours.

    The body is read rather than declared as a model: the signature covers
    the exact bytes, and the shape is Cartograph's to change. Fields are
    pulled out defensively so an added key never turns into a 422 they
    cannot see.
    """
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    case_ref = payload.get("case_ref") or payload.get("job_id")
    status = payload.get("status")
    warnings = payload.get("warnings") or []
    counts = payload.get("counts_created") or payload.get("created") or {}

    # Log at a level that matches the outcome: a failed import or a payload
    # that produced warnings is something to act on, not background noise.
    if status == "ok" and not warnings:
        logger.info(
            "Cartograph import ok case_ref=%s scan_id=%s created=%s",
            case_ref, payload.get("scan_id"), counts,
        )
    else:
        logger.warning(
            "Cartograph import status=%s case_ref=%s scan_id=%s created=%s "
            "warnings=%s error=%s",
            status, case_ref, payload.get("scan_id"), counts,
            warnings, payload.get("error_message"),
        )

    return {"ok": True, "received": case_ref}
