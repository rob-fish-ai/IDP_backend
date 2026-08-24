"""HMAC request signing for the Cartograph integration.

Both directions use the same scheme, matching what Cartograph's
`Webhooks::RunpodOcrController` already verifies in production:

    signed  = "{timestamp}.{raw_body}"
    header  = HMAC_SHA256(secret, signed).hexdigest()

Two details are easy to get wrong and fail as an opaque 401:

  - the digest is **hex**, with no version prefix. An earlier draft of the
    contract specified "v1,<base64>"; the deployed implementation does not.
  - the signature covers the **raw bytes actually sent**. Re-serializing a
    parsed body to verify it will produce a different string, so the caller
    must sign and send the same bytes.

Separate secrets are used per direction so a compromise in one does not
expose the other.
"""

import hashlib
import hmac
import time

# Requests older (or newer) than this are rejected, which bounds replay of a
# captured request. Matches the 300s window Cartograph enforces inbound.
MAX_SKEW_SECONDS = 300

TIMESTAMP_HEADER = "X-Timestamp"
SIGNATURE_HEADER = "X-Signature"


def sign(body: bytes, secret: str, timestamp: int | None = None) -> tuple[str, str]:
    """Return (timestamp, signature) headers for a request body.

    `body` must be the exact bytes that will be transmitted.
    """
    ts = str(timestamp if timestamp is not None else int(time.time()))
    signed = ts.encode() + b"." + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return ts, digest


def verify(
    body: bytes,
    secret: str | None,
    timestamp: str | None,
    signature: str | None,
    *,
    now: int | None = None,
) -> tuple[bool, str]:
    """Check an inbound signature. Returns (ok, reason).

    Fails closed: an unset secret is rejected rather than waved through.
    Cartograph's older webhooks default-allow when unconfigured; this one
    must not, or an attacker's unsigned request is indistinguishable from a
    misconfigured deploy.
    """
    if not secret:
        return False, "signing secret not configured"
    if not timestamp or not signature:
        return False, "missing signature headers"

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "invalid timestamp"

    current = now if now is not None else int(time.time())
    if abs(current - ts) > MAX_SKEW_SECONDS:
        return False, "timestamp out of range"

    signed = str(ts).encode() + b"." + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"
    return True, "ok"
