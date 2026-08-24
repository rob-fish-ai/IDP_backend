"""Webhook endpoints for the Salesforce → IDP audit pipeline.

Recommended (single-webhook flow):
  POST /webhook/audit-ready    — Salesforce signals MuleSoft is done.
                                 IDP runs the full chain: download PDF,
                                 extract, compare, write findings back.

Legacy (2-webhook flow, kept for backward compat):
  POST /webhook/pdf-attached   — Salesforce signals PDF attached.
                                 IDP starts extraction.
  POST /webhook/mulesoft-done  — Salesforce signals MuleSoft completed.
                                 IDP runs comparison + builds findings.

Plus a read endpoint:
  GET  /audit/cases/{case_id}  — Inspect job state + findings text.

Each webhook returns 202 Accepted immediately and runs the heavy work in
a background task. Idempotency: repeat webhooks for the same case_id are
de-duplicated via JobStore state.
"""
from __future__ import annotations

import json
import logging

from fastapi import (
    APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request,
)
from pydantic import BaseModel, Field, field_validator

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.services.audit.job_store import get_job_store
from app.services.audit.jobs import run_audit, run_comparison, run_extraction
from app.services.audit.poller import SUPPORTED_CERT_TYPES
from app.services.cartograph.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _verify_webhook_mode(settings: Settings = Depends(get_settings)) -> None:
    """Reject webhook requests when audit_mode is set to 'poll' only."""
    if settings.audit_mode == "poll":
        raise HTTPException(
            status_code=503,
            detail=(
                "IDP is configured for polling mode (IDP_AUDIT_MODE=poll). "
                "Webhooks are disabled. Set IDP_AUDIT_MODE=webhook or =both "
                "to enable webhook reception."
            ),
        )


def _verify_webhook_token(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> None:
    """Reject webhooks lacking a matching bearer token.

    Auth is REQUIRED unless IDP_DEV_MODE=true is explicitly set. Missing
    token in production fails closed (returns 503) — never accept
    unauthenticated webhooks silently.
    """
    expected = settings.webhook_auth_token
    if not expected:
        if settings.dev_mode:
            logger.warning(
                "Webhook auth bypassed (IDP_DEV_MODE=true) — DEV ONLY",
            )
            return
        # Fail closed: configuration error, not a request error
        logger.error(
            "IDP_WEBHOOK_AUTH_TOKEN is not set in production. "
            "Refusing webhook to prevent unauthenticated access."
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook auth not configured",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if authorization.split(" ", 1)[1] != expected:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


# ---------------------------------------------------------------------------
# Payload schemas
# ---------------------------------------------------------------------------

class PdfAttachedPayload(BaseModel):
    """Payload Salesforce sends when a PDF is attached to a case.

    Salesforce supplies the cert type and funding program directly so the
    IDP doesn't need a follow-up query — those values are already on the
    Case record at the time the webhook fires.
    """
    case_id: str = Field(..., description="Salesforce Case Id (15- or 18-char)")
    case_number: str | None = Field(
        default=None,
        description="Salesforce CaseNumber (e.g. CAS570309) — used in findings text",
    )
    cert_type: str = Field(
        ...,
        description=(
            "Case.CertType__c — one of MI, AR, AR-SC, IR. Drives the "
            "extraction prompts and compliance rules. Other values "
            "(e.g. 'Certification Review', 'IC') are rejected with 422."
        ),
    )
    funding_program: str = Field(
        ...,
        description=(
            "Case.Funding_Program2__c — e.g. LIHTC, HUD, HUD 202/8, USDA. "
            "Drives the work-hours range resolution rule."
        ),
    )
    content_document_id: str | None = Field(
        default=None,
        description=(
            "Optional ContentDocumentId of the attached PDF. If omitted, "
            "the IDP will scan the case for the most recent PDF."
        ),
    )

    @field_validator("cert_type")
    @classmethod
    def _validate_cert_type(cls, v: str) -> str:
        if v not in SUPPORTED_CERT_TYPES:
            raise ValueError(
                f"cert_type={v!r} is not supported by IDP. "
                f"Allowed values: {sorted(SUPPORTED_CERT_TYPES)}"
            )
        return v


class MuleSoftDonePayload(BaseModel):
    case_id: str
    case_number: str | None = None


class AuditReadyPayload(BaseModel):
    """Payload Salesforce sends when a case is ready for full IDP audit.

    Fired AFTER MuleSoft has finished processing the PDF. IDP runs the
    full chain (download -> extract -> compare -> writeback) on receipt.
    Single-webhook design — replaces the older two-webhook flow.
    """
    case_id: str = Field(..., description="Salesforce Case Id (15- or 18-char)")
    case_number: str | None = Field(
        default=None,
        description="Salesforce CaseNumber (e.g. CAS570309) — used in findings text",
    )
    cert_type: str = Field(
        ...,
        description=(
            "Case.CertType__c — one of MI, AR, AR-SC, IR. Other values "
            "(e.g. 'Certification Review', 'IC') are rejected with 422."
        ),
    )
    funding_program: str = Field(
        ..., description="Case.Funding_Program2__c — e.g. LIHTC, HUD, USDA",
    )
    content_document_id: str | None = Field(
        default=None,
        description=(
            "Optional ContentDocumentId of the PDF MuleSoft processed. "
            "Strongly recommended — without it, IDP scans the case and "
            "picks the most recent non-review PDF, which is best-effort."
        ),
    )

    @field_validator("cert_type")
    @classmethod
    def _validate_cert_type(cls, v: str) -> str:
        if v not in SUPPORTED_CERT_TYPES:
            raise ValueError(
                f"cert_type={v!r} is not supported by IDP. "
                f"Allowed values: {sorted(SUPPORTED_CERT_TYPES)}"
            )
        return v


# ---------------------------------------------------------------------------
# Audit-ready webhook (recommended, single-signal flow)
# ---------------------------------------------------------------------------

@router.post(
    "/webhook/audit-ready",
    status_code=202,
    dependencies=[Depends(_verify_webhook_mode), Depends(_verify_webhook_token)],
)
def audit_ready(
    payload: AuditReadyPayload,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Salesforce → IDP: case is ready for full audit (MuleSoft done).

    Runs the entire audit chain in the background:
      1. Download PDF (via content_document_id if provided, else case scan)
      2. Run IDP extraction
      3. Pull MuleSoft data from Salesforce
      4. Run AI-vs-MuleSoft comparison
      5. Format findings
      6. Write findings to Case.IDP_Testing_Results__c
    """
    logger.info(
        "Webhook audit-ready: case=%s cert=%s funding=%s document=%s",
        payload.case_number or payload.case_id,
        payload.cert_type,
        payload.funding_program,
        payload.content_document_id,
    )

    store = get_job_store(settings.audit_job_db)
    upsert = store.upsert_pending(
        case_id=payload.case_id,
        case_number=payload.case_number,
        cert_type=payload.cert_type,
        funding_program=payload.funding_program,
        content_document_id=payload.content_document_id,
    )

    if upsert.get("deduplicated"):
        return {
            "status": "already_in_progress",
            "case_id": payload.case_id,
            "state": upsert.get("state"),
        }

    background_tasks.add_task(run_audit, payload.case_id)

    return {
        "status": "accepted",
        "case_id": payload.case_id,
        "case_number": payload.case_number,
        "cert_type": payload.cert_type,
        "funding_program": payload.funding_program,
    }


# ---------------------------------------------------------------------------
# Webhook 1: PDF attached
# ---------------------------------------------------------------------------

@router.post(
    "/webhook/pdf-attached",
    status_code=202,
    dependencies=[Depends(_verify_webhook_mode), Depends(_verify_webhook_token)],
)
def pdf_attached(
    payload: PdfAttachedPayload,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Salesforce → IDP: a PDF was attached to a case.

    Salesforce sends case_id + cert_type + funding_program in one payload.
    IDP enqueues the extraction job and returns immediately. PDF download
    happens in the background task.
    """
    logger.info(
        "Webhook pdf-attached: case=%s cert=%s funding=%s document=%s",
        payload.case_number or payload.case_id,
        payload.cert_type,
        payload.funding_program,
        payload.content_document_id,
    )

    store = get_job_store(settings.audit_job_db)
    upsert = store.upsert_pending(
        case_id=payload.case_id,
        case_number=payload.case_number,
        cert_type=payload.cert_type,
        funding_program=payload.funding_program,
        content_document_id=payload.content_document_id,
    )

    if upsert.get("deduplicated"):
        return {
            "status": "already_in_progress",
            "case_id": payload.case_id,
            "state": upsert.get("state"),
        }

    background_tasks.add_task(run_extraction, payload.case_id)

    return {
        "status": "accepted",
        "case_id": payload.case_id,
        "case_number": payload.case_number,
        "cert_type": payload.cert_type,
        "funding_program": payload.funding_program,
    }


# ---------------------------------------------------------------------------
# Webhook 2: MuleSoft done
# ---------------------------------------------------------------------------

@router.post(
    "/webhook/mulesoft-done",
    status_code=202,
    dependencies=[Depends(_verify_webhook_mode), Depends(_verify_webhook_token)],
)
def mulesoft_done(
    payload: MuleSoftDonePayload,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Salesforce → IDP: MuleSoft has finished extracting this case.

    IDP enqueues the comparison job. If the extraction is still running,
    the comparison waits for it to complete.
    """
    logger.info("Webhook mulesoft-done: case_id=%s", payload.case_id)

    store = get_job_store(settings.audit_job_db)
    job = store.get(payload.case_id)

    if job is None:
        # Webhook 2 arrived without webhook 1 — bootstrap a pending job
        # so the comparison can proceed once extraction is triggered.
        # (Comparison will fall through to error if extraction is never
        # triggered — that's correct behavior for an out-of-order signal.)
        logger.warning(
            "mulesoft-done received for unknown case %s — comparing anyway",
            payload.case_id,
        )

    background_tasks.add_task(run_comparison, payload.case_id)

    return {
        "status": "accepted",
        "case_id": payload.case_id,
        "current_state": job.get("state") if job else "unknown",
    }


# ---------------------------------------------------------------------------
# Read endpoints: list jobs / inspect one case
# ---------------------------------------------------------------------------

@router.get(
    "/audit/cases",
    dependencies=[Depends(_verify_webhook_token)],
)
def list_cases(
    state: str | None = None,
    limit: int = 100,
    offset: int = 0,
    settings: Settings = Depends(get_settings),
) -> dict:
    """List audit jobs from the local JobStore.

    Query params:
      state  — optional filter (done, extracting, extracted, comparing,
               pending, extraction_failed, comparison_failed,
               mulesoft_timeout). Omit to see all states.
      limit  — page size (default 100, max 500).
      offset — page offset.

    Returns lightweight rows (no findings_text or extraction_result).
    Use GET /audit/cases/{case_id} to drill into one job's findings.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    store = get_job_store(settings.audit_job_db)
    rows = store.list_all(state=state, limit=limit, offset=offset)
    total = store.count(state=state)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "state_filter": state,
        "items": rows,
    }


@router.get(
    "/audit/cases/{case_id}",
    dependencies=[Depends(_verify_webhook_token)],
)
def get_case_audit(
    case_id: str,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Return the audit state for a case, with MuleSoft data attached.

    Response shape (for the monitoring UI's 3-panel detail view):
      {
        source: "jobstore" | "salesforce",
        case_id, case_number, cert_type, funding_program, state, ...,
        findings_text: str | None,         # comparison panel
        extraction_result: dict | None,    # IDP panel
        mulesoft_data: { ... } | None,     # MuleSoft panel
        mulesoft_data_source: "snapshot" | "live" | null,
        mulesoft_error: str | None,        # set if a live fetch failed
      }

    For completed cases the MuleSoft panel is served from the snapshot
    captured at comparison time so the analyst sees exactly the data the
    findings were computed against. For in-flight cases (no snapshot yet)
    the panel falls back to a live Salesforce query.
    """
    from app.services.salesforce.client import get_salesforce_client

    store = get_job_store(settings.audit_job_db)
    job = store.get(case_id)

    # Best-effort live MuleSoft fetch. Failures don't break the panel
    # response; they're reported in mulesoft_error and the UI degrades.
    def _fetch_mulesoft() -> tuple[dict | None, str | None]:
        try:
            sf = get_salesforce_client(settings)
            return sf.get_case_audit_data(case_id), None
        except Exception as exc:
            logger.warning(
                "MuleSoft fetch failed for case %s: %s", case_id, exc,
            )
            return None, str(exc)

    # SSNs are stored as captured (full when a document shows them) for
    # compliance exports, but every audit-facing response masks them.
    from app.services.validation import mask_ssns_deep

    if job is not None:
        snapshot = job.pop("mulesoft_snapshot", None)
        if snapshot is not None:
            job["mulesoft_data"] = snapshot
            job["mulesoft_data_source"] = "snapshot"
            job["mulesoft_error"] = None
        else:
            mulesoft_data, mulesoft_error = _fetch_mulesoft()
            job["mulesoft_data"] = mulesoft_data
            job["mulesoft_data_source"] = "live" if mulesoft_data is not None else None
            job["mulesoft_error"] = mulesoft_error
        job["source"] = "jobstore"
        return mask_ssns_deep(job)

    # Local miss → check Salesforce for findings + MuleSoft data.
    try:
        sf = get_salesforce_client(settings)
        record = sf.get_case_findings(case_id)
    except Exception as exc:
        logger.exception("Salesforce fallback failed for case %s", case_id)
        raise HTTPException(
            status_code=502,
            detail=f"Case not in local store and Salesforce lookup failed: {exc}",
        )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Case {case_id} not found in JobStore or Salesforce",
        )

    mulesoft_data, mulesoft_error = _fetch_mulesoft()

    return mask_ssns_deep({
        "source": "salesforce",
        "case_id": record.get("Id"),
        "case_number": record.get("CaseNumber"),
        "cert_type": record.get("CertType__c"),
        "funding_program": record.get("Funding_Program2__c"),
        "audit_complete": bool(record.get("IDP_Audit_Complete__c")),
        "findings_text": record.get("IDP_Testing_Results__c"),
        "extraction_result": None,
        "mulesoft_data": mulesoft_data,
        "mulesoft_data_source": "live" if mulesoft_data is not None else None,
        "mulesoft_error": mulesoft_error,
    })


# ---------------------------------------------------------------------------
# Admin endpoint: clear JobStore rows so cases can be re-audited
# ---------------------------------------------------------------------------

class ResetJobsPayload(BaseModel):
    """Reset payload for /admin/audit-jobs/reset.

    Three modes — pick the smallest blast radius that solves your problem:
      - "done"   : delete only completed audits (typical case for re-running
                   after a comparator/formatter change).
      - "by_ids" : delete specific case IDs.
      - "all"    : delete every row. Use with caution — wipes in-flight work.
    """
    mode: str = Field(..., description="'done' | 'by_ids' | 'all'")
    case_ids: list[str] | None = Field(
        default=None,
        description="Required when mode='by_ids', ignored otherwise",
    )

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in ("done", "by_ids", "all"):
            raise ValueError(f"mode must be one of 'done', 'by_ids', 'all'; got {v!r}")
        return v


@router.post(
    "/admin/audit-jobs/reset",
    dependencies=[Depends(_verify_webhook_token)],
)
def reset_audit_jobs(
    payload: ResetJobsPayload,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Clear JobStore rows so cases re-enter the queue on next poll/webhook.

    Authenticated with the same bearer token as webhooks. Works regardless
    of audit_mode (poll vs webhook), since this is a maintenance op.
    """
    store = get_job_store(settings.audit_job_db)

    if payload.mode == "done":
        deleted = store.delete_by_state("done")
        logger.warning("Admin reset: deleted %d DONE jobs", deleted)
        return {"deleted": deleted, "mode": "done"}

    if payload.mode == "by_ids":
        ids = payload.case_ids or []
        if not ids:
            raise HTTPException(
                status_code=400,
                detail="case_ids is required when mode='by_ids'",
            )
        deleted = store.delete_by_case_ids(ids)
        logger.warning(
            "Admin reset: deleted %d job(s) by case_id (requested %d)",
            deleted, len(ids),
        )
        return {"deleted": deleted, "mode": "by_ids", "requested": len(ids)}

    # mode == "all"
    deleted = store.delete_all()
    logger.warning("Admin reset: WIPED %d jobs (mode=all)", deleted)
    return {"deleted": deleted, "mode": "all"}


# ---------------------------------------------------------------------------
# Cartograph integration — result callback
# ---------------------------------------------------------------------------

@router.post("/integration/import_result", status_code=200)
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
