"""Bearer-token authentication shared by every route that needs it.

This lived in the webhook router while webhooks were the only authenticated
surface. The PDF routes need the same check, and a router importing another
router to get at it would be the wrong shape, so it sits here instead.
"""

import hmac
import logging

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings
from app.core.dependencies import get_settings

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


def verify_cartograph_upload_token(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> None:
    """Authenticate a case upload from Cartograph.

    A credential of its own rather than the shared webhook token, so
    Cartograph's access can be revoked or rotated without breaking the
    monitoring frontend or the Salesforce webhooks. Same reasoning as the two
    separate HMAC secrets.

    No dev-mode bypass: this endpoint spends money on every call and accepts
    resident documents, so an unset token is a 503 in every environment.
    """
    expected = settings.cartograph_upload_token
    if not expected:
        logger.error(
            "IDP_CARTOGRAPH_UPLOAD_TOKEN is not set. Refusing the upload "
            "rather than accepting an unauthenticated document."
        )
        raise HTTPException(
            status_code=503,
            detail="Upload auth not configured",
        )

    _check(authorization, expected)
