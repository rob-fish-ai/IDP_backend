"""Request authentication for the Cartograph endpoints."""

import logging

from fastapi import Depends, HTTPException, Request

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.services.cartograph.environments import identify
from app.services.cartograph.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER

logger = logging.getLogger(__name__)


async def verify_cartograph_signature(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Verify an HMAC-signed request from Cartograph and name its environment.

    Every inbound Cartograph endpoint uses this. Each environment the host
    serves has its own callback secret; the one that verifies the signature
    says which environment sent the request, and the handler reads it from
    `request.state.environment`. The secret is per direction, not per
    endpoint: everything an environment sends is signed with its callback
    secret, everything sent back to it with its ingest secret.

    Reading the body here is free for the handler: Starlette caches it, so a
    later `await request.body()` returns the same bytes rather than a second
    read of the stream.
    """
    body = await request.body()
    environment, reason = identify(
        settings, body,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
    )
    if environment is None:
        logger.warning("Cartograph request rejected: %s", reason)
        raise HTTPException(status_code=401, detail=reason)
    request.state.environment = environment
