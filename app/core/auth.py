"""Bearer-token authentication shared by every route that needs it.

This lived in the webhook router while webhooks were the only authenticated
surface. The PDF routes need the same check, and a router importing another
router to get at it would be the wrong shape, so it sits here instead.
"""

import hmac
import logging

from fastapi import Depends, Header, HTTPException, Request

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.services.cartograph.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify,
)

logger = logging.getLogger(__name__)


def _check(authorization: str | None, expected: str) -> None:
    """Compare a bearer header against the expected token, in constant time.

    compare_digest rather than `==` so the comparison does not leak the
    length of the matching prefix through timing. Impractical to exploit
    against a 64-char random token over a network, but it costs nothing and
    the integration document asks Cartograph to do the same.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if not hmac.compare_digest(authorization.split(" ", 1)[1], expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def verify_bearer_token(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> None:
    """Reject requests lacking a matching bearer token.

    Auth is REQUIRED unless IDP_DEV_MODE=true is explicitly set. A missing
    token in production fails closed with 503 — a configuration error is
    never silently downgraded into an open endpoint.
    """
    expected = settings.webhook_auth_token
    if not expected:
        if settings.dev_mode:
            logger.warning(
                "Bearer auth bypassed (IDP_DEV_MODE=true) — DEV ONLY",
            )
            return
        # Fail closed: configuration error, not a request error
        logger.error(
            "IDP_WEBHOOK_AUTH_TOKEN is not set in production. "
            "Refusing the request to prevent unauthenticated access."
        )
        raise HTTPException(
            status_code=503,
            detail="Auth not configured",
        )

    _check(authorization, expected)


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
