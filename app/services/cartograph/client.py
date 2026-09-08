"""Outbound client for delivering extraction results to Cartograph.

Cartograph's ingest endpoint verifies the signature, stores the whole body
in `raw_payload`, enqueues a background job, and returns 202. Its importer
does not write cert records yet — it replies "field mapping not yet
implemented" — so sending early is safe and useful: it exercises the
signature handshake and puts a real payload in their database for the
importer to be built against, which is worth more than a specification.

The result of the import arrives later on the callback endpoint, not in the
response to this call.
"""

import json
import logging

import httpx

from app.core.config import Settings
from app.services.cartograph.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)

# One definition, in the module that builds the payload the version
# describes. The second copy here was kept "in step" by hand under a comment
# describing a lazy import that was never written, so the two could disagree
# and the failure would be a consumer told the wrong schema version.
from app.services.cartograph.adapter import SCHEMA_VERSION

logger = logging.getLogger(__name__)


class CartographNotConfigured(RuntimeError):
    """Raised when delivery is attempted without a URL or secret."""


def is_configured(settings: Settings) -> bool:
    """Whether delivery can be attempted at all.

    The placeholder check is not pedantry. The shipped default is
    'https://<cartograph-host>/webhooks/runpod_ocr_results', which is a
    non-empty string, so a bare truthiness test calls it configured and the
    failure surfaces minutes later as a DNS error inside a background task
    rather than as the configuration problem it is.
    """
    url = settings.cartograph_ingest_url
    if not url or not settings.cartograph_ingest_secret:
        return False
    if "<" in url or ">" in url:
        return False
    return True


def post_failure(
    case_ref: str,
    reason: str,
    settings: Settings,
    *,
    error_code: str = "extraction_failed",
) -> dict | None:
    """Tell Cartograph a case could not be audited.

    Without this, a case whose document could not be fetched simply stops.
    Cartograph goes on waiting and nobody discovers it until a reviewer opens
    an empty checklist. The most common cause — an expired presigned URL — is
    also the most recoverable: Cartograph reissues the link and notifies
    again.

    Best-effort by design. It is called from failure paths, so it must not
    raise and mask the error it is reporting.
    """
    if not is_configured(settings):
        logger.error(
            "case_ref=%s failed (%s) and Cartograph is not configured, so the "
            "failure could not be reported: %s", case_ref, error_code, reason,
        )
        return None

    payload = {
        "schema_version": SCHEMA_VERSION,
        "case_ref": case_ref,
        "status": "failed",
        "error_code": error_code,
        "error_message": reason,
    }
    try:
        return post_extraction(payload, settings)
    except Exception:
        logger.exception(
            "case_ref=%s could not report failure to Cartograph", case_ref,
        )
        return None


def post_extraction(payload: dict, settings: Settings) -> dict:
    """POST one extraction payload. Returns Cartograph's parsed response.

    The body is serialized once and both signed and sent as the same bytes:
    signing a re-serialization would produce a different string and fail
    verification for reasons that are invisible from the 401.
    """
    if not is_configured(settings):
        raise CartographNotConfigured(
            "IDP_CARTOGRAPH_INGEST_URL / IDP_CARTOGRAPH_INGEST_SECRET are not set"
        )

    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    timestamp, signature = sign(body, settings.cartograph_ingest_secret)

    headers = {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: signature,
    }

    with httpx.Client(timeout=settings.cartograph_timeout_seconds) as client:
        response = client.post(
            settings.cartograph_ingest_url, content=body, headers=headers,
        )

    try:
        parsed = response.json()
    except ValueError:
        parsed = {"raw": response.text[:500]}

    if response.status_code >= 400:
        logger.error(
            "Cartograph ingest rejected case_ref=%s status=%d body=%s",
            payload.get("case_ref"), response.status_code, parsed,
        )
    else:
        logger.info(
            "Cartograph ingest accepted case_ref=%s status=%d scan_id=%s",
            payload.get("case_ref"), response.status_code, parsed.get("scan_id"),
        )

    return {
        "status_code": response.status_code,
        "ok": response.status_code < 400,
        "response": parsed,
        "bytes_sent": len(body),
    }
