"""Request authentication for the Cartograph endpoints."""

import logging

from fastapi import Depends, HTTPException, Request

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.services.cartograph.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify,
)

logger = logging.getLogger(__name__)


async def verify_cartograph_signature(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Verify an HMAC-signed request from Cartograph.

    Both inbound Cartograph endpoints use this — the audit notification and
    the import-result callback. The secret is per *direction*, not per
    endpoint: everything Cartograph sends us is signed with the same inbound
    secret, and everything we send them uses the outbound one. Two secrets
    total, however many endpoints each direction grows.

    Reading the body here is free for the handler: Starlette caches it, so a
    later `await request.body()` returns the same bytes rather than a second
    read of the stream.
    """
    body = await request.body()
    ok, reason = verify(
        body,
        settings.cartograph_callback_secret,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
    )
    if not ok:
        logger.warning("Cartograph request rejected: %s", reason)
        raise HTTPException(status_code=401, detail=reason)
