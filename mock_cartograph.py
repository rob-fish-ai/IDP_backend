#!/usr/bin/env python3
"""A stand-in for Cartograph, for testing the integration without them.

Plays the far side of the wire: verifies the signature on the payload the
engine delivers, prints it, and answers 202 the way their real endpoint
does. Optionally posts the import-result callback back to the engine, which
closes the full round trip.

Point the engine at it:

    IDP_CARTOGRAPH_INGEST_URL=http://localhost:9000/webhooks/runpod_ocr_results

Then run it and fire a notification at /integration/case. Everything the
real integration does happens, end to end, with nobody else involved.

    python3 mock_cartograph.py                # verify and print
    python3 mock_cartograph.py --callback     # also send the result back

This is a development tool, not part of the service. It is deliberately
strict — it verifies exactly as Cartograph should, so a signature bug shows
up here rather than in production.
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from app.core.dependencies import get_settings
from app.services.cartograph.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
    verify,
)

settings = get_settings()
ARGS = None


def _send_callback(payload: dict, import_id: str) -> None:
    """Post the import result back to the engine, as Cartograph would.

    Runs on a delay and off the request thread, because the real callback
    arrives after their background job finishes — not inside the response to
    the delivery. Testing it any other way would prove the wrong thing.
    """
    time.sleep(1.0)

    members = payload.get("household_members") or []
    body = json.dumps({
        "import_id": import_id,
        "extraction_id": payload.get("extraction_id"),
        "case_ref": payload.get("case_ref"),
        "job_id": payload.get("target", {}).get("job_id"),
        "cert_review_id": 8891,
        "status": "ok",
        "created": {"members": len(members)},
        "warnings": [],
        "errors": [],
    }).encode()

    ts, sig = sign(body, settings.cartograph_callback_secret)
    url = f"{ARGS.engine}/integration/import_result"
    try:
        response = httpx.post(
            url, content=body, timeout=30.0,
            headers={
                "Content-Type": "application/json",
                TIMESTAMP_HEADER: ts,
                SIGNATURE_HEADER: sig,
            },
        )
        print(f"\n  callback -> {url}  HTTP {response.status_code} {response.text}")
    except Exception as exc:
        print(f"\n  callback FAILED: {exc}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # the prints below are the useful output

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        print("\n" + "=" * 72)
        print(f"POST {self.path}   {len(raw)} bytes")

        ok, reason = verify(
            raw,
            settings.cartograph_ingest_secret,
            self.headers.get(TIMESTAMP_HEADER),
            self.headers.get(SIGNATURE_HEADER),
        )
        print(f"signature: {'VALID' if ok else 'REJECTED — ' + reason}")
        if not ok:
            self._reply(401, {"error": reason})
            return

        try:
            payload = json.loads(raw)
        except ValueError:
            print("body is not JSON")
            self._reply(400, {"error": "invalid JSON"})
            return

        # A failure report rather than an extraction. The real Cartograph
        # needs to handle this too — it is how the engine reports an expired
        # document URL.
        if payload.get("status") == "failed":
            print(f"FAILURE REPORT  case_ref={payload.get('case_ref')}")
            print(f"  {payload.get('error_code')}: {payload.get('error_message')}")
            self._reply(202, {"ok": True, "import_id": "imp_failure_noted"})
            return

        members = payload.get("household_members") or []
        print(f"case_ref  : {payload.get('case_ref')}")
        print(f"schema    : {payload.get('schema_version')}   "
              f"engine: {payload.get('engine_version')}")
        print(f"target    : {payload.get('target')}")
        print(f"members   : {len(members)}")
        for m in members:
            flags = []
            if m.get("is_hoh"):
                flags.append("HoH")
            if m.get("full_time_student"):
                flags.append("student")
            if m.get("is_disabled"):
                flags.append("disabled")
            name = f"{m.get('first_name') or ''} {m.get('last_name') or ''}".strip()
            print(f"  - {name or '(unnamed)':28} dob={m.get('date_of_birth') or '?':10} "
                  f"ssn4={m.get('ssn_last4') or '?':4} {' '.join(flags)}")

        print("\nfull payload:")
        print(json.dumps(payload, indent=2))

        import_id = f"imp_mock_{int(time.time())}"
        self._reply(202, {"ok": True, "import_id": import_id})

        if ARGS.callback:
            threading.Thread(
                target=_send_callback, args=(payload, import_id), daemon=True,
            ).start()


def main() -> int:
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--engine", default="http://localhost:8001",
        help="Engine base URL, for the --callback round trip",
    )
    parser.add_argument(
        "--callback", action="store_true",
        help="Post the import result back to the engine after each delivery",
    )
    ARGS = parser.parse_args()

    # Line-buffer stdout so the output appears as requests arrive even when
    # redirected to a file. Block buffering makes a live tail look dead.
    sys.stdout.reconfigure(line_buffering=True)

    if not settings.cartograph_ingest_secret:
        print("IDP_CARTOGRAPH_INGEST_SECRET is not set — nothing to verify against.")
        return 1

    print(f"Mock Cartograph listening on http://localhost:{ARGS.port}")
    print(f"  ingest path : /webhooks/runpod_ocr_results")
    print(f"  callback    : {'on -> ' + ARGS.engine if ARGS.callback else 'off'}")
    print(f"\nSet IDP_CARTOGRAPH_INGEST_URL="
          f"http://localhost:{ARGS.port}/webhooks/runpod_ocr_results\n")

    HTTPServer(("127.0.0.1", ARGS.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
