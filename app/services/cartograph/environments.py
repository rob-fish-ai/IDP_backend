"""The Cartograph environments this engine serves.

Cartograph runs dev, staging and prod; the engine runs as two hosts, and
staging shares the prod host (2026-11-05). So one host may serve several
environments, and every request must be known by the environment it came
from: its result goes back to that environment's ingest URL, and its row
in the job store is keyed so a staging run of a case never collides with
the prod run of the same case_ref — the two share a database, and so the
same references.

The caller is identified by its signature: each environment has its own
callback secret, and the one that verifies names the environment. No
field in the request is needed for it, which keeps the request format
Cartograph's.
"""
from __future__ import annotations

import logging

from app.core.config import CartographEnvironment, Settings
from app.services.cartograph.signing import verify

logger = logging.getLogger(__name__)

LEGACY_NAME = "default"


class CartographNotConfigured(RuntimeError):
    """Raised when delivery is attempted for an environment without a
    URL or secret."""


def environments(settings: Settings) -> dict[str, CartographEnvironment]:
    """The configured environments by name. With none configured, the
    single-environment settings from before environments existed define
    one, named "default", so an unchanged env file keeps working."""
    if settings.cartograph_environments:
        return dict(settings.cartograph_environments)
    legacy = CartographEnvironment(
        ingest_url=settings.cartograph_ingest_url,
        ingest_secret=settings.cartograph_ingest_secret,
        callback_secret=settings.cartograph_callback_secret,
    )
    if legacy.ingest_url or legacy.ingest_secret or legacy.callback_secret:
        return {LEGACY_NAME: legacy}
    return {}


def default_environment(settings: Settings) -> str | None:
    """The environment whose rows are keyed by bare case_ref."""
    envs = environments(settings)
    if settings.cartograph_default_environment:
        return settings.cartograph_default_environment
    return next(iter(envs), None)


def job_key(settings: Settings, environment: str | None, case_ref: str) -> str:
    """The job store key for a case in an environment: the case_ref itself
    in the default environment (rows written before environments existed
    are keyed that way), "<environment>:<case_ref>" in any other."""
    if not environment or environment == default_environment(settings):
        return case_ref
    return f"{environment}:{case_ref}"


def identify(settings: Settings, body: bytes, timestamp: str | None,
             signature: str | None) -> tuple[str | None, str]:
    """(environment name, reason): the environment whose callback secret
    verifies the signature, or None and why. Each secret is tried in
    turn; a mismatch against every one is one rejection, not many."""
    envs = environments(settings)
    if not envs:
        return None, "no Cartograph environment is configured"
    reason = "signature does not match any configured environment"
    for name, env in envs.items():
        if not env.callback_secret:
            continue
        ok, why = verify(body, env.callback_secret, timestamp, signature)
        if ok:
            return name, "ok"
        # A missing header or a stale timestamp fails the same way for
        # every secret; say that rather than "no match".
        if "signature" not in why.lower() or "missing" in why.lower():
            reason = why
    return None, reason


def is_configured(settings: Settings, environment: str | None = None) -> bool:
    """Whether delivery can be attempted for the environment.

    The placeholder check is not pedantry: a shipped default of
    'https://<cartograph-host>/...' is a non-empty string, and a bare
    truthiness test calls it configured, so the failure surfaces minutes
    later as a DNS error inside a background task."""
    env = environments(settings).get(environment or default_environment(settings) or "")
    if env is None or not env.ingest_url or not env.ingest_secret:
        return False
    if "<" in env.ingest_url or ">" in env.ingest_url:
        return False
    return True


def for_delivery(settings: Settings, environment: str | None = None) -> tuple[str, CartographEnvironment]:
    """The named (or default) environment to deliver to, or
    CartographNotConfigured."""
    name = environment or default_environment(settings)
    if not name or not is_configured(settings, name):
        raise CartographNotConfigured(
            f"Cartograph environment {name!r} has no ingest URL or secret "
            "(IDP_CARTOGRAPH_ENVIRONMENTS, or IDP_CARTOGRAPH_INGEST_URL / _SECRET)"
        )
    return name, environments(settings)[name]


def describe(settings: Settings) -> dict:
    """What the health response and the boot log say: names and hosts,
    never secrets."""
    out = {}
    for name, env in environments(settings).items():
        host = env.ingest_url.split("//", 1)[-1].split("/", 1)[0] if env.ingest_url else None
        out[name] = {"ingest_host": host, "deliverable": is_configured(settings, name),
                     "verifies": bool(env.callback_secret)}
    return {"environments": out, "default": default_environment(settings)}
