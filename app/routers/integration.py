"""Cartograph-facing endpoints.

Separate from the Salesforce webhook router on purpose. Salesforce and
MuleSoft retire at the end of 2026, and when they do, `webhook.py` deletes
almost entirely while this file stands alone unchanged. Keeping the two
integrations in one module would make that a surgical edit instead of a
deletion.

Two endpoints, one per direction:

  POST /integration/case           Cartograph uploads a packet to audit.
                                   Bearer auth, multipart, 202 immediately.
  POST /integration/import_result  Cartograph reports how the import went.
                                   HMAC auth, JSON.

The delivery leg — engine to Cartograph — is not here. It is an outbound
call, made by the background task in `cartograph.client`.
"""
from __future__ import annotations

import json
import logging

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request,
    UploadFile,
)

from app.core.auth import verify_cartograph_upload_token
from app.core.config import Settings
from app.core.dependencies import get_settings
from app.core.exceptions import InvalidFileError
from app.services.audit.job_store import get_job_store
from app.services.cartograph.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify,
)
from app.services.cartograph.tasks import audit_uploaded_case

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integration", tags=["Cartograph"])


# ---------------------------------------------------------------------------
# Cartograph -> Engine: a case is ready to audit
# ---------------------------------------------------------------------------

@router.post(
    "/case",
    status_code=202,
    dependencies=[Depends(verify_cartograph_upload_token)],
)
async def receive_case(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="The certification packet"),
    case_ref: str = Form(..., description="Cartograph Job#ref_number"),
    cert_type: str | None = Form(None, description="MI, AR, AR-SC or IR"),
    program: str | None = Form(None, description="Funding program"),
    job_id: int | None = Form(None),
    community_id: int | None = Form(None),
    unit_number: str | None = Form(None),
    effective_date: str | None = Form(None),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Accept a packet, acknowledge, and audit it in the background.

    Returns 202 without doing the work. An extraction takes minutes — OCR on
    every page plus several model calls — and Cartograph must not hold a
    connection open for that. The result arrives later as a signed POST to
    their ingest endpoint, not in the response to this call.

    Cartograph should send this from a background job rather than a
    controller action: Heroku's router terminates an inbound request at 30
    seconds, so a web request that uploads a large packet dies regardless of
    how quickly this responds.
    """
    if file.content_type not in settings.allowed_content_types:
        raise InvalidFileError(
            f"Unsupported content type '{file.content_type}'. "
            "Only PDF files are accepted."
        )

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise InvalidFileError("Uploaded file is empty.")

    # Idempotency lives in the job store rather than in a timestamp window.
    # A bearer token has no replay protection, and a retry from Cartograph is
    # indistinguishable from a replay — both should cost one audit, not two.
    store = get_job_store(settings.audit_job_db)
    upsert = store.upsert_pending(
        case_id=case_ref,
        case_number=case_ref,
        cert_type=cert_type,
        funding_program=program,
        content_document_id=None,
    )
    if upsert.get("deduplicated"):
        logger.info(
            "Case %s already in state %s — ignoring duplicate upload",
            case_ref, upsert.get("state"),
        )
        return {
            "status": "already_in_progress",
            "case_ref": case_ref,
            "state": upsert.get("state"),
        }

    logger.info(
        "Cartograph upload accepted case_ref=%s cert=%s program=%s bytes=%d "
        "filename=%s",
        case_ref, cert_type, program, len(pdf_bytes), file.filename,
    )

    background_tasks.add_task(
        audit_uploaded_case,
        pdf_bytes=pdf_bytes,
        case_ref=case_ref,
        cert_type=cert_type,
        program=program,
        job_id=job_id,
        community_id=community_id,
        unit_number=unit_number,
        effective_date=effective_date,
    )

    return {"status": "accepted", "case_ref": case_ref}


# ---------------------------------------------------------------------------
# Cartograph -> Engine: how the import went
# ---------------------------------------------------------------------------

@router.post("/import_result", status_code=200)
async def cartograph_import_result(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Receive the outcome of a Cartograph import.

    Cartograph's ingest endpoint returns 202 before its background job runs
    the inserts, so the HTTP response to our POST cannot say what happened.
    Without this callback an import that fails validation on their side
    fails silently on ours.

    Authenticated with the same HMAC scheme we use outbound, keyed on a
    separate inbound secret. Fails closed: an unset secret is rejected
    rather than accepted.
    """
    body = await request.body()
    ok, reason = verify(
        body,
        settings.cartograph_callback_secret,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
    )
    if not ok:
        logger.warning("Cartograph callback rejected: %s", reason)
        raise HTTPException(status_code=401, detail=reason)

    try:
        payload = json.loads(body)
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
