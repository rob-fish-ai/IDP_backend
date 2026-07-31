import io
import logging
from pathlib import Path

import httpx
from PIL import Image

from app.core.config import Settings
from app.core.exceptions import ProcessingError

logger = logging.getLogger(__name__)


def _call_deepseek_ocr(buf: io.BytesIO, image_name: str, settings: Settings) -> dict:
    """Tier 1: DeepSeek-OCR."""
    buf.seek(0)
    with httpx.Client(timeout=settings.ocr_timeout) as client:
        response = client.post(
            settings.ocr_service_url,
            files={"file": (image_name, buf, "image/png")},
            data={
                "prompt": settings.ocr_prompt,
                "dpi": str(settings.ocr_dpi),
                "raw": bool(settings.ocr_raw),
                "retry": bool(settings.ocr_retry),
            },
        )
        response.raise_for_status()
    return response.json()


def _call_glm_ocr(buf: io.BytesIO, image_name: str, settings: Settings) -> dict:
    """Tier 2: GLM-OCR fallback."""
    buf.seek(0)
    with httpx.Client(timeout=settings.ocr_timeout) as client:
        response = client.post(
            settings.ocr_fallback_url,
            files={"files": (image_name, buf, "image/png")},
        )
        response.raise_for_status()

    result = response.json()

    text = result.get("text", "")
    if not text and isinstance(result.get("results"), list):
        text = "\n".join(r.get("text", "") for r in result["results"])

    return {
        "text": text,
        "flag": "yellow",
        "flag_message": "Extracted via GLM-OCR fallback",
        "flag_details": ["glm_ocr_fallback"],
        "score": {"composite": 0.6},
        "needs_external_ocr": False,
    }


def flag_codes(flags) -> set[str]:
    """Normalize a flag_details list to its string codes.

    The OCR service emits rich dict flags ({'code': ..., 'severity': ...});
    pipeline stages append plain strings. Membership checks must accept both.
    """
    codes: set[str] = set()
    for f in flags or []:
        if isinstance(f, str):
            codes.add(f)
        elif isinstance(f, dict) and f.get("code"):
            codes.add(f["code"])
    return codes


def composite_of(result: dict) -> float:
    """Extract the composite quality score from an OCR result (dict or scalar)."""
    score = result.get("score")
    if isinstance(score, dict):
        score = score.get("composite")
    try:
        return float(score or 0.0)
    except (TypeError, ValueError):
        return 0.0


def ocr_single_image(
    image_path: Path, settings: Settings, *, allow_fallback: bool = True,
) -> dict:
    """Send a single processed image to OCR with tiered fallback.

    Tier 1: DeepSeek-OCR (primary)
    Tier 2: GLM-OCR (if DeepSeek fails or needs_external_ocr)
    Tier 3: Vision LLM (handled by pdf_service Phase B2)

    allow_fallback=False skips the GLM tier — used by the rotation probe,
    where a low score usually means "wrong angle" and burning the fallback
    service on it is waste.
    """
    img = Image.open(image_path)

    buf = io.BytesIO()
    img.save(buf, format="PNG")

    # Tier 1: DeepSeek-OCR
    try:
        result = _call_deepseek_ocr(buf, image_path.name, settings)
    except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning(
            "DeepSeek-OCR failed for %s (%s) — trying GLM-OCR fallback",
            image_path.name, type(exc).__name__,
        )
        result = None

    needs_fallback = (
        result is None
        or result.get("needs_external_ocr")
    )

    # Tier 2: GLM-OCR fallback
    if needs_fallback and allow_fallback and settings.ocr_fallback_url:
        try:
            glm_result = _call_glm_ocr(buf, image_path.name, settings)
            if glm_result.get("text", "").strip():
                logger.info(
                    "GLM-OCR fallback succeeded for %s — %d chars",
                    image_path.name, len(glm_result["text"]),
                )
                return glm_result
            logger.warning(
                "GLM-OCR returned empty text for %s — marking for vision fallback",
                image_path.name,
            )
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.warning(
                "GLM-OCR fallback also failed for %s (%s) — marking for vision fallback",
                image_path.name, type(exc).__name__,
            )

    if result is None:
        raise ProcessingError(f"All OCR services failed for {image_path.name}")

    if result.get("needs_external_ocr"):
        flags = result.setdefault("flag_details", [])
        if isinstance(flags, list) and "ocr_failed" not in flags:
            flags.append("ocr_failed")

    return result


def ocr_job(job_id: str, settings: Settings) -> dict:
    """Run OCR on all processed page images for a given job.

    Returns a dict with job_id, total_pages, and per-page extracted text.
    """
    processed_dir = settings.output_dir / job_id / "processed"
    if not processed_dir.exists():
        raise ProcessingError(f"No processed images found for job '{job_id}'.")

    image_files = sorted(
        processed_dir.glob("page_*.png"),
        key=lambda p: int(p.stem.split("_")[1]),
    )
    if not image_files:
        raise ProcessingError(f"No page images found in job '{job_id}'.")

    logger.info("Starting OCR job=%s pages=%d", job_id, len(image_files))
    pages = []

    for image_path in image_files:
        page_num = int(image_path.stem.split("_")[1])
        logger.info("OCR job=%s page=%d", job_id, page_num)

        result = ocr_single_image(image_path, settings)

        pages.append({
            "page": page_num,
            "text": result.get("text"),
            "image": str(image_path),
            "flag": result.get("flag"),
            "flag_message": result.get("flag_message"),
            "flag_details": result.get("flag_details", []),
            "score": result.get("score"),
        })

    logger.info("OCR complete job=%s pages=%d", job_id, len(pages))

    summary = {"green": 0, "yellow": 0, "red": 0}
    flagged_pages =[]
    for p in pages:
        color = p.get("flag") or "yellow"
        summary[color] = summary.get(color, 0) + 1
        if color in ("yellow", "red"):
            flagged_pages.append({
                "page": p["page"],
                "flag": color,
                "flag_message": p.get("flag_message"),
                "score": p["score"]["composite"] if p.get("score") else None,
            })

    return {
        "job_id": job_id,
        "total_pages": len(pages),
        "pages": pages,
        "summary": summary,
        "flagged_pages": flagged_pages
    }
