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
  POST /integration/findings_feedback  Reviewer verdicts on our findings and
                                   the findings added by hand. HMAC auth, JSON,
                                   one case or a nightly batch of cases.
                                   Also accepted on /import_result when the
                                   body says event_type: findings_feedback.

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
from app.services.cartograph.checklist import normalise_rows
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
    # Carried as sent. The checklist rows are read out of them by
    # cartograph.checklist; the manifest is kept with the request for now.
    requirements: list | None = None
    checklist_rows: list | None = None
    existing_records_manifest: dict | list | None = None


@router.post(
    "/case",
    status_code=202,
    dependencies=[Depends(verify_cartograph_signature)],
)
async def receive_case(
    payload: AuditRequest,
    request: Request,
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
        # The work is queued below rather than left for a poller, so the row
        # is created already owned. Without this the row sits in `pending`
        # until a thread picks the task up — and a Cartograph retry inside
        # that window is not recognized as a duplicate, so the case is
        # fetched, extracted and delivered twice. The window is milliseconds
        # when the engine is idle and as long as another case's extraction
        # when every worker thread is busy, which is exactly when a retry is
        # most likely.
        claimed=True,
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

    # The request as received is kept with the job, and any top-level key
    # the model does not know is logged: the shape is Cartograph's to
    # change, and a field the engine ignores must never disappear quietly.
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    unknown = sorted(set(body) - set(AuditRequest.model_fields)) if isinstance(body, dict) else []
    if unknown:
        logger.warning("Cartograph audit request case_ref=%s carries keys the engine does not read: %s",
                       payload.case_ref, unknown)
    checklist_rows = normalise_rows(body if isinstance(body, dict) else {})
    try:
        store.record_request(payload.case_ref, raw, checklist_rows)
    except Exception:
        logger.exception("Could not keep the request for case_ref=%s", payload.case_ref)

    logger.info(
        "Cartograph audit request case_ref=%s cert=%s program=%s documents=%d checklist_rows=%d",
        payload.case_ref, payload.cert_type, payload.program,
        len(payload.documents), len(checklist_rows),
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
        checklist_rows=checklist_rows,
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
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    case_ref = payload.get("case_ref") or payload.get("job_id")
    status = payload.get("status")

    # The nightly feedback may arrive here too, told apart by its event
    # field. It must not be stored as an import result: that would replace
    # the record of how the import went with a list of verdicts.
    if (payload.get("event_type") or payload.get("event") or "").strip().lower() == "findings_feedback":
        return _store_findings_feedback(payload, raw)

    # Store it against the job before logging. A case that was delivered and
    # then rejected during their import is otherwise indistinguishable here
    # from one that imported cleanly — both leave the job row saying "done".
    if case_ref:
        try:
            get_job_store(get_settings().audit_job_db).record_import_result(
                str(case_ref), payload,
            )
        except Exception:
            # Never let bookkeeping turn a report we asked for into an error
            # they have to retry.
            logger.exception(
                "Could not store import result for case_ref=%s", case_ref,
            )

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


_REJECTED_BODY_CHARS = 2000


def _describe_rejected_body(raw: bytes, payload=None) -> str:
    """What to log about a body the feedback endpoint could not accept.

    The nightly push was rejected on every night so far and the engine kept
    no record of what arrived, so nobody could say whether the case
    reference sits under another name or the event is a batch. The shape
    is what matters: the top-level keys when it parsed, the first bytes
    when it did not. Verdict text is reviewer commentary and not secret,
    but the whole body is not needed, so it is cut at a fixed length.
    """
    shape: str
    if isinstance(payload, dict):
        shape = f"object with keys {sorted(payload)}"
    elif isinstance(payload, list):
        first = payload[0] if payload else None
        inner = f" of objects with keys {sorted(first)}" if isinstance(first, dict) else ""
        shape = f"list of {len(payload)}{inner}"
    elif payload is not None:
        shape = type(payload).__name__
    else:
        shape = "unparseable"
    text = raw.decode("utf-8", errors="replace")
    if len(text) > _REJECTED_BODY_CHARS:
        text = text[:_REJECTED_BODY_CHARS] + f"... [{len(raw)} bytes]"
    return f"shape={shape} body={text!r}"


def _normalise_feedback_case(case: dict) -> dict:
    """Map the names Cartograph's nightly export uses onto the documented
    per-case event shape, so the job store reads one vocabulary.

    The export says `finding_verdicts` / `reason` / `missed_findings` /
    `checklist_item_key` + `checklist_item_name`; the documented event says
    `verdicts` / `verdict_reason` / `manual_findings` /
    `matched_checklist_item`. A body already in the documented shape passes
    through untouched. The originals are kept beside the mapped names so
    the raw JSON stored per row still shows what arrived.
    """
    out = dict(case)
    if out.get("verdicts") is None and isinstance(out.get("finding_verdicts"), list):
        verdicts = []
        for v in out["finding_verdicts"]:
            if not isinstance(v, dict):
                continue
            v = dict(v)
            if v.get("verdict_reason") is None and "reason" in v:
                v["verdict_reason"] = v["reason"]
            verdicts.append(v)
        out["verdicts"] = verdicts
    if out.get("manual_findings") is None and isinstance(out.get("missed_findings"), list):
        manual = []
        for m in out["missed_findings"]:
            if not isinstance(m, dict):
                continue
            m = dict(m)
            key, name = m.get("checklist_item_key"), m.get("checklist_item_name")
            if m.get("matched_checklist_item") is None and (key or name):
                m["matched_checklist_item"] = {"key": key, "name": name}
            manual.append(m)
        out["manual_findings"] = manual
    if out.get("scan_id") is None and out.get("cert_review_id") is not None:
        # The export carries no scan id; the cert review id is the closest
        # thing to one and is what the reviewer would quote back to us.
        out["scan_id"] = out["cert_review_id"]
    return out


def _store_one_feedback_case(case: dict) -> tuple[str, dict]:
    case_ref = case.get("case_ref") or case.get("job_id")
    case = _normalise_feedback_case(case)
    try:
        counts = get_job_store(get_settings().audit_job_db).record_findings_feedback(
            str(case_ref), case,
        )
    except Exception:
        logger.exception("Could not store findings feedback for case_ref=%s", case_ref)
        raise HTTPException(status_code=500, detail="could not store feedback")
    logger.info(
        "Findings feedback case_ref=%s scan_id=%s verdicts=%d manual=%d",
        case_ref, case.get("scan_id"), counts["verdicts"], counts["manual_findings"],
    )
    return str(case_ref), counts


def _store_findings_feedback(payload, raw: bytes = b"") -> dict:
    """Store a findings_feedback body, which is either one case with a
    top-level case_ref or a batch envelope with a `cases` list.

    Cartograph's nightly push is the batch form: every case reviewed that
    day under one `generated_at`, re-sent each night until accepted. A
    batch is stored case by case; the ones without a case reference are
    skipped and named in the response, and the whole batch is only
    rejected when none of its cases can be stored.
    """
    if not isinstance(payload, dict):
        logger.warning("Findings feedback rejected, body is not an object: %s",
                       _describe_rejected_body(raw, payload))
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    if payload.get("case_ref") or payload.get("job_id"):
        case_ref, counts = _store_one_feedback_case(payload)
        return {"ok": True, "received": case_ref, "stored": counts}

    cases = payload.get("cases")
    if isinstance(cases, list):
        stored: dict[str, dict] = {}
        skipped = 0
        for case in cases:
            if isinstance(case, dict) and (case.get("case_ref") or case.get("job_id")):
                case_ref, counts = _store_one_feedback_case(case)
                stored[case_ref] = counts
            else:
                skipped += 1
        if stored:
            if skipped:
                logger.warning("Findings feedback batch: %d case(s) skipped, no case_ref", skipped)
            return {"ok": True, "received": sorted(stored), "stored": stored, "skipped": skipped}

    logger.warning("Findings feedback rejected, no case_ref: %s",
                   _describe_rejected_body(raw, payload))
    raise HTTPException(status_code=400, detail="case_ref is required")


@router.post(
    "/findings_feedback",
    status_code=200,
    dependencies=[Depends(verify_cartograph_signature)],
)
async def cartograph_findings_feedback(request: Request) -> dict:
    """Receive a reviewer's verdicts on our findings, and the findings the
    reviewer added by hand, for one case.

    Sent nightly. The documented form is one event per reviewed case:
      {"event_type": "findings_feedback", "case_ref": ..., "scan_id": ...,
       "verdicts": [{"finding_key", "verdict": "valid"|"invalid", "verdict_reason"}],
       "manual_findings": [{"description", "page", "subject_label",
                            "source": "manual", "matched_checklist_item": {"key", "name"}}]}
    What Cartograph actually sends is a batch of that day's reviews:
      {"event": "findings_feedback", "generated_at": ...,
       "cases": [{"case_ref", "cert_review_id",
                  "finding_verdicts": [{"finding_key", "scan_id", "verdict", "reason"}],
                  "missed_findings": [{"source", "description", "page", "subject_label",
                                       "checklist_item_key", "checklist_item_name"}]}]}
    Both are accepted; the batch is stored case by case with its field
    names mapped onto the documented ones. The same body on /import_result,
    carrying the event_type, is accepted identically. A later event for the
    same case replaces earlier verdicts.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("Findings feedback rejected, invalid JSON: %s", _describe_rejected_body(raw))
        raise HTTPException(status_code=400, detail="invalid JSON body")
    return _store_findings_feedback(payload, raw)
