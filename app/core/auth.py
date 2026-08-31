"""Bearer-token authentication shared by every route that needs it.

This lived in the webhook router while webhooks were the only authenticated
surface. The PDF routes need the same check, and a router importing another
router to get at it would be the wrong shape, so it sits here instead.
"""

import logging

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings
from app.core.dependencies import get_settings

logger = logging.getLogger(__name__)


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

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if authorization.split(" ", 1)[1] != expected:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
