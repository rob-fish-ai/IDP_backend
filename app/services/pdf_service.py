import logging
import time
from concurrent.futures import ThreadPoolExecutor

import fitz  # PyMuPDF
from PIL import Image

from app.core.config import Settings
from app.core.exceptions import ProcessingError
from app.services.image_processing import preprocess_for_ocr, suspected_content_loss
from app.services.ocr_service import ocr_single_image
from app.services.pipeline import run_extraction_pipeline

logger = logging.getLogger(__name__)


def process_pdf(
    pdf_bytes: bytes, settings: Settings, work_dir=None,
) -> dict:
    """Split PDF into pages, pre-process images, run OCR, and save text per page.

    Output structure (under work_dir, default settings.output_dir):
        <work_dir>/processed/page_1.png, page_2.png, ...
        <work_dir>/texts/page_1.txt, page_2.txt, ...

    Concurrent jobs MUST pass distinct work_dirs — page files are named
    by page number only and would collide in a shared directory.
    """
    work_dir = work_dir or settings.output_dir
    processed_dir = work_dir / "processed"
    texts_dir = work_dir / "texts"
    processed_dir.mkdir(parents=True, exist_ok=True)
    texts_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ProcessingError(f"Failed to open PDF: {exc}") from exc

    total_pages = len(doc)
    logger.info("Processing PDF total_pages=%d", total_pages)
    start = time.perf_counter()

    # Phase A: pipelined render + parallel preprocess.
    #
    # Rendering stays sequential on the main thread because PyMuPDF page
    # access is not thread-safe. Preprocessing (OpenCV / NumPy) releases
    # the Python GIL, so it runs in a ThreadPoolExecutor with true
    # parallelism up to the worker count. This overlaps render(n+1) with
    # preprocess(n) and gives ~4× speedup on multi-core hardware.
    from pathlib import Path

    def _preprocess_and_save(
        page_num: int, pil_image: Image.Image,
    ) -> tuple[int, Path]:
        processed = preprocess_for_ocr(
            pil_image,
            max_width=settings.image_max_width,
            max_height=settings.image_max_height,
        )
        path = processed_dir / f"page_{page_num}.png"
        processed.save(str(path), "PNG")
        logger.info("Preprocessed page=%d", page_num)
        return page_num, path

    processed_map: dict[int, Path] = {}
    with ThreadPoolExecutor(max_workers=settings.preprocess_concurrency) as pool:
        futures = []
        for page_num in range(1, total_pages + 1):
            page = doc[page_num - 1]
            zoom = settings.render_dpi / 72
            matrix = fitz.Matrix(zoom, zoom)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pil_image = Image.frombytes(
                "RGB", (pixmap.width, pixmap.height), pixmap.samples,
            )
            # Submit preprocessing to worker; main thread moves on to render
            # the next page while the worker does CLAHE/denoise/deskew.
            futures.append(pool.submit(_preprocess_and_save, page_num, pil_image))

        # Wait for all preprocess tasks to finish. Results are collected by
        # page number regardless of completion order.
        for fut in futures:
            page_num, path = fut.result()
            processed_map[page_num] = path

    doc.close()
    processed_paths: list[tuple[int, Path]] = [
        (pn, processed_map[pn]) for pn in sorted(processed_map)
    ]
    logger.info(
        "Rendered + preprocessed %d pages in %.2fs "
        "(preprocess_concurrency=%d) — starting parallel OCR (concurrency=%d)",
        total_pages, time.perf_counter() - start,
        settings.preprocess_concurrency, settings.ocr_concurrency,
    )

    # Phase B: OCR in parallel. The OCR service handles ocr_concurrency
    # requests concurrently; ThreadPoolExecutor is safe here because
    # ocr_single_image only does I/O (HTTP POST).
    def _ocr_one(item: tuple[int, Path]) -> tuple[int, dict]:
        page_num, path = item
        try:
            result = ocr_single_image(path, settings)
        except ProcessingError:
            logger.warning(
                "OCR failed page=%d — will attempt vision fallback", page_num,
            )
            result = {
                "text": "",
                "flag": "red",
                "flag_message": "OCR service failed (timeout or error)",
                "flag_details": ["ocr_failed"],
                "score": {"composite": 0.0},
                "needs_external_ocr": True,
            }
        score = result.get("score", {})
        composite = score.get("composite") if isinstance(score, dict) else score
        logger.info(
            "OCR done page=%d flag=%s score=%s chars=%d needs_external=%s",
            page_num,
            result.get("flag", "?"),
            f"{composite:.2f}" if composite is not None else "?",
            len(result.get("text", "")),
            result.get("needs_external_ocr", False),
        )
        return page_num, result

    ocr_results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as pool:
        for page_num, result in pool.map(_ocr_one, processed_paths):
            ocr_results[page_num] = result

    # Phase B2: Vision fallback for low-quality OCR pages.
    # Pages whose OCR composite score falls below the threshold get their
    # text re-extracted via Claude Vision (reads directly from the page
    # image, bypassing OCR entirely). The replacement text flows into
    # classification and extraction so every downstream step benefits.
    #
    # Besides the OCR service's own quality flag, a page is also queued
    # when its ink density implies far more text than OCR returned —
    # OCR can silently drop half a dense form page while the text it
    # DID return looks clean, keeping the quality score above the
    # threshold (observed: a TIC rent/signature page reduced to its
    # boilerplate paragraphs, losing every rent field).
    path_by_page = {pn: str(p) for pn, p in processed_paths}
    low_quality_pages = []
    for page_num, ocr_result in ocr_results.items():
        if ocr_result.get("needs_external_ocr"):
            low_quality_pages.append(page_num)
            continue
        text_len = len((ocr_result.get("text") or "").strip())
        img_path = path_by_page.get(page_num)
        if img_path and suspected_content_loss(img_path, text_len):
            logger.warning(
                "Page %d: OCR returned %d chars but the page's ink density "
                "implies far more — suspected content loss, queueing vision "
                "fallback", page_num, text_len,
            )
            flags = ocr_result.setdefault("flag_details", [])
            if isinstance(flags, list) and "suspected_content_loss" not in flags:
                flags.append("suspected_content_loss")
            low_quality_pages.append(page_num)

    if low_quality_pages:
        from app.services.llm_service import call_llm_vision
        low_quality_pages.sort()
        logger.info(
            "Phase B2: Vision fallback for %d low-quality OCR page(s): %s",
            len(low_quality_pages), low_quality_pages,
        )

        _VISION_PROMPT = (
            "Extract ALL text from this document page. Preserve the structure:\n"
            "- Reproduce tables using HTML <table> tags\n"
            "- Keep field labels and their values together\n"
            "- Include all dollar amounts, dates, names, and numbers exactly as shown\n"
            "- Preserve form field numbers (e.g., '12. Effective Date', '86. Total Annual Income')\n"
            "Return ONLY the extracted text, no commentary."
        )

        def _vision_one(page_num: int) -> tuple[int, str | None]:
            img_path = path_by_page.get(page_num)
            if not img_path:
                return page_num, None
            try:
                return page_num, call_llm_vision(
                    _VISION_PROMPT,
                    f"Extract all text from page {page_num} of this document.",
                    [img_path],
                    settings,
                )
            except Exception:
                logger.exception(
                    "Vision fallback page=%d failed — keeping original OCR text",
                    page_num,
                )
                return page_num, None

        with ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as pool:
            for page_num, vision_text in pool.map(_vision_one, low_quality_pages):
                if vision_text and len(vision_text.strip()) > len(ocr_results[page_num].get("text", "").strip()):
                    logger.info(
                        "Vision fallback page=%d: replaced %d chars with %d chars",
                        page_num,
                        len(ocr_results[page_num].get("text", "")),
                        len(vision_text),
                    )
                    ocr_results[page_num]["text"] = vision_text
                    ocr_results[page_num]["flag"] = "yellow"
                    ocr_results[page_num]["flag_message"] = "Text re-extracted via Vision fallback"
                    flags = ocr_results[page_num].setdefault("flag_details", [])
                    if isinstance(flags, list) and "vision_fallback" not in flags:
                        flags.append("vision_fallback")
                elif vision_text is not None:
                    logger.info(
                        "Vision fallback page=%d: vision produced less text than OCR, keeping original",
                        page_num,
                    )

    # Phase C: write text files + build pages list in order.
    pages = []
    for page_num, processed_path in processed_paths:
        ocr_result = ocr_results[page_num]
        text = ocr_result.get("text", "")
        text_path = texts_dir / f"page_{page_num}.txt"
        text_path.write_text(text, encoding="utf-8")

        pages.append({
            "page": page_num,
            "processed_image": str(processed_path),
            "text_file": str(text_path),
            "text": text,
            "flag": ocr_result.get("flag"),
            "flag_message": ocr_result.get("flag_message"),
            "flag_details": ocr_result.get("flag_details", []),
            "score": ocr_result.get("score"),
        })

    elapsed = time.perf_counter() - start
    logger.info("Completed PDF processing pages=%d elapsed=%.2fs", total_pages, elapsed)

    # Build summary
    summary = {"green": 0, "yellow": 0, "red": 0}
    flagged_pages = []
    for p in pages:
        color = p.get("flag") or "yellow"
        summary[color] = summary.get(color, 0) + 1
        if color in ("yellow", "red"):
            flagged_pages.append({
                "page": p["page"],
                "flag": color,
                "flag_message": p.get("flag_message"),
                "score": (
                    p["score"]["composite"] if isinstance(p.get("score"), dict)
                    else float(p["score"]) if isinstance(p.get("score"), (int, float))
                    else None
                ),
            })

    return {
        "total_pages": total_pages,
        "pages": pages,
        "summary": summary,
        "flagged_pages": flagged_pages,
    }


def process_pdf_full(
    pdf_bytes: bytes,
    settings: Settings,
    *,
    funding_program: str | None = None,
    certification_type: str | None = None,
    source_files: list[dict] | None = None,
    work_dir=None,
) -> dict:
    """Full pipeline: OCR all pages, then classify, extract, and validate.

    Returns both the OCR results and the structured MuleSoft extraction.
    """
    # Stage 1: OCR
    ocr_result = process_pdf(pdf_bytes, settings, work_dir=work_dir)

    # Stage 2: Extraction pipeline — include OCR quality scores + image paths
    page_texts = []
    for p in ocr_result["pages"]:
        raw_score = p.get("score")
        # OCR may return score as float (skipped pages) or dict (processed pages)
        if isinstance(raw_score, (int, float)):
            ocr_score = float(raw_score)
        elif isinstance(raw_score, dict):
            ocr_score = raw_score.get("composite")
        else:
            ocr_score = None

        page_texts.append({
            "page": p["page"],
            "text": p["text"],
            "ocr_flag": p.get("flag"),
            "ocr_score": ocr_score,
            "ocr_flag_details": p.get("flag_details", []),
            "image_path": p.get("processed_image"),
        })

    extraction = run_extraction_pipeline(
        page_texts,
        settings,
        funding_program=funding_program,
        certification_type=certification_type,
        source_files=source_files,
    )

    # Save extraction result for local testing / debugging
    import json
    result_path = (work_dir or settings.output_dir) / "extraction_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(extraction.model_dump(), f, indent=2, default=str)
    logger.info("Saved extraction result to %s", result_path)

    return {
        "ocr": ocr_result,
        "extraction": extraction,
    }
