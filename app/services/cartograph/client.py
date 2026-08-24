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

logger = logging.getLogger(__name__)


class CartographNotConfigured(RuntimeError):
    """Raised when delivery is attempted without a URL or secret."""


def is_configured(settings: Settings) -> bool:
    return bool(settings.cartograph_ingest_url and settings.cartograph_ingest_secret)


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
