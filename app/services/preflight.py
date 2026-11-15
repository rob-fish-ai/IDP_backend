"""What a host needs before its first case, checked in one pass.

`scripts/preflight.py` runs these and prints one line per check. Each
check returns (status, detail) with status "ok", "warn" or "fail": a fail
is something the first case would die on (no key, an OCR service that does
not answer, an environment without secrets); a warn is something to know
(little free space, a placeholder left in .env). The network checks make
one real, tiny request each, so what passes here is what the engine does.
"""
from __future__ import annotations

import io
import shutil
import sqlite3
from pathlib import Path

from app.core.config import Settings

Check = tuple[str, str, str]      # name, status, detail

_PLACEHOLDER = "REPLACE_WITH"


def _tiny_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def check_anthropic(settings: Settings) -> Check:
    """The key is accepted and the configured model exists."""
    key = settings.anthropic_api_key
    if not key or _PLACEHOLDER in key:
        return ("anthropic", "fail", "IDP_ANTHROPIC_API_KEY is not set")
    try:
        import anthropic
        anthropic.Anthropic(api_key=key, timeout=30).models.retrieve(settings.llm_model)
    except Exception as exc:
        return ("anthropic", "fail", f"{settings.llm_model}: {type(exc).__name__}: {exc}")
    return ("anthropic", "ok", f"key accepted, model {settings.llm_model} found")


def _ocr(name: str, url: str, files_field: str, data: dict, required: bool) -> Check:
    # The primary service down is a fail: no page gets read. The fallback
    # down is a warn: the pages it would have taken go to vision instead.
    bad = "fail" if required else "warn"
    if not url:
        return (name, bad if required else "ok", "not set" if required else "not set (optional)")
    import httpx
    try:
        with httpx.Client(timeout=60) as client:
            r = client.post(url, files={files_field: ("preflight.png", _tiny_png(), "image/png")}, data=data)
        if r.status_code >= 400:
            return (name, bad, f"{url} answered HTTP {r.status_code}")
    except Exception as exc:
        return (name, bad, f"{url}: {type(exc).__name__}: {exc}")
    return (name, "ok", f"{url} answered HTTP {r.status_code}")


def check_ocr(settings: Settings) -> list[Check]:
    """Both OCR services take a page image, with the request shape the engine
    sends. A refused connection here is the OCR operator not yet allowing
    this host."""
    return [
        _ocr("ocr primary", settings.ocr_service_url, "file",
             {"prompt": settings.ocr_prompt, "dpi": str(settings.ocr_dpi), "raw": settings.ocr_raw, "retry": settings.ocr_retry},
             required=True),
        _ocr("ocr fallback", settings.ocr_fallback_url, "files", {"do_sample": "false"}, required=False),
    ]


def check_environments(settings: Settings) -> list[Check]:
    """Every configured Cartograph environment has a full ingest URL and
    both secrets, none of them a placeholder. No environment at all is a
    host that rejects every call: fine until credentials exist, so a warn."""
    from app.services.cartograph.environments import environments
    envs = environments(settings)
    if not envs:
        return [("cartograph", "warn", "no environment configured — the engine will reject every call")]
    out: list[Check] = []
    for name, env in envs.items():
        problems = []
        url = env.ingest_url or ""
        if not url.startswith("https://") or url.count("/") < 3:
            problems.append("ingest_url is not a full https URL with a path")
        if _PLACEHOLDER in url:
            problems.append("ingest_url is a placeholder")
        for field in ("ingest_secret", "callback_secret"):
            val = getattr(env, field) or ""
            if not val:
                problems.append(f"{field} is empty")
            elif _PLACEHOLDER in val:
                problems.append(f"{field} is a placeholder")
        if env.ingest_secret and env.ingest_secret == env.callback_secret:
            problems.append("ingest_secret and callback_secret are the same value")
        out.append((f"cartograph {name}", "fail" if problems else "ok",
                    "; ".join(problems) or f"delivers to {url.split('/')[2]}"))
    return out


def check_storage(settings: Settings, warn_below_mb: int = 5000) -> list[Check]:
    """The output folder and the job store's folder exist (made if not),
    have room, and the job store opens."""
    out: list[Check] = []
    for name, path in (("output dir", Path(settings.output_dir)), ("job store dir", Path(settings.audit_job_db).parent)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(path).free // (1024 * 1024)
        except OSError as exc:
            out.append((name, "fail", f"{path}: {exc}"))
            continue
        out.append((name, "warn" if free < warn_below_mb else "ok", f"{path}: {free} MB free"))
    try:
        conn = sqlite3.connect(str(settings.audit_job_db))
        n = conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'audit_jobs'").fetchone()[0]
        rows = conn.execute("SELECT count(*) FROM audit_jobs").fetchone()[0] if n else 0
        conn.close()
        out.append(("job store", "ok", f"{settings.audit_job_db}: {rows} row(s)" if n else f"{settings.audit_job_db}: new"))
    except sqlite3.Error as exc:
        out.append(("job store", "fail", f"{settings.audit_job_db}: {exc}"))
    return out


def run_all(settings: Settings, network: bool = True) -> list[Check]:
    checks: list[Check] = []
    if network:
        checks.append(check_anthropic(settings))
        checks.extend(check_ocr(settings))
    checks.extend(check_environments(settings))
    checks.extend(check_storage(settings))
    return checks
