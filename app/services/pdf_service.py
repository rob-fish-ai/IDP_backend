import logging
import re
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import fitz  # PyMuPDF
from PIL import Image

from app.core.config import Settings
from app.core.exceptions import ProcessingError
from app.services.image_processing import preprocess_for_ocr, suspected_content_loss
from app.services.ocr_service import composite_of, flag_codes, ocr_single_image
from app.services.text_sanitizer import strip_html
from app.services.pipeline import run_extraction_pipeline

logger = logging.getLogger(__name__)


_IMAGE_REGION_RE = None


def _skipped_region_fraction(ocr_text: str) -> float:
    """Fraction of the page area DeepSeek-OCR skipped as image regions.

    DeepSeek emits `<|ref|>image<|/ref|><|det|>[[x1, y1, x2, y2]]<|/det|>`
    for regions it did not transcribe, with coordinates on a 0-1000 grid.
    """
    global _IMAGE_REGION_RE
    import re as _re
    if _IMAGE_REGION_RE is None:
        _IMAGE_REGION_RE = _re.compile(
            r"<\|ref\|>image<\|/ref\|><\|det\|>\[\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]\]<\|/det\|>"
        )
    area = 0
    for m in _IMAGE_REGION_RE.finditer(ocr_text):
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        area += max(0, x2 - x1) * max(0, y2 - y1)
    return min(1.0, area / 1_000_000)


# A 1-3 character unit repeated four or more times, where the unit carries
# at least one letter or digit. Dotted leaders and rules ("........",
# "------") are excluded on purpose: they are formatting, and a form page
# legitimately has many of them.
_REPEAT_RUN_RE = re.compile(r"((?=[^\s]*\w)[^\s]{1,3})\1{3,}")
# Above this fraction of a page's text, the OCR has transcribed a barcode,
# a security strip or a scanner artefact as characters. The score does not
# notice — the run is "confident" output — and the flags the OCR service
# raises for hallucination do not fire on it either.
_REPETITIVE_FRACTION = 0.20


def _repetitive_fraction(text: str) -> float:
    """Fraction of a page's characters inside repeated short-unit runs.

    Observed: a Social Security benefit letter came back as 7,995 characters
    of which 343 runs of "I1I1I1I1..." were garbage — over a third of the
    page — with the composite at 0.81 and no quality flag. The benefit
    amount did not survive. The extractor then read that page and reported
    the head of household's income as $0.00, which the audit delivered.
    """
    total = len(text or "")
    if not total:
        return 0.0
    run_chars = sum(len(m.group(0)) for m in _REPEAT_RUN_RE.finditer(text))
    return run_chars / total


# Quality flags the OCR service raises on its own output. They used to feed
# only the rotation probe and the replacement rule; a page carrying one but
# scoring above the vision threshold shipped as read. Observed: an EIV
# income report whose five pages were "Mouth Line Thickness: 1/4" and
# "Data of Birth:" repeated hundreds of times, flagged repetitive_content by
# the service, scored 0.79-0.86, never re-read.
_VISION_QUEUE_FLAGS = frozenset({
    "possible_hallucination",
    "repetitive_content",
    "incomplete_extraction",
})

# Degenerate text: a decoder loop compresses to almost nothing, and one
# token dominates. Calibrated on 147 stored pages: the five hallucinated
# pages compress to 0.012-0.031 of their length and every clean page above
# 300 characters to at least 0.30; the bank verification page whose account
# cell became 2,896 nines compresses to 0.13 but is 82% one token, and no
# clean page exceeds 0.19.
_DEGENERATE_ZLIB_RATIO = 0.10
_DOMINANT_TOKEN_FRACTION = 0.50
_DEGENERATE_MIN_CHARS = 300


def _plain_text(text: str) -> str:
    """The text as the extractor will see it: no grounding tags, no HTML."""
    return strip_html(text or "")


def _degenerate_signals(text: str) -> tuple[float, float]:
    """(compression ratio, dominant-token fraction) of the page's plain text.

    Both are whitespace-tolerant, which the short-run regex is not: the
    loops it missed were "I/we, I/we," (six characters with a space) and
    "Data of birth:" (three words).
    """
    plain = _plain_text(text)
    if len(plain) < _DEGENERATE_MIN_CHARS:
        return 1.0, 0.0
    ratio = len(zlib.compress(plain.encode("utf-8"))) / len(plain)
    tokens = plain.split()
    counts: dict[str, int] = {}
    for tok in tokens:
        counts[tok] = counts.get(tok, 0) + 1
    dominant = max(counts.values()) / len(tokens) if tokens else 0.0
    return ratio, dominant


def _is_degenerate(text: str) -> tuple[bool, str]:
    ratio, dominant = _degenerate_signals(text)
    if ratio < _DEGENERATE_ZLIB_RATIO:
        return True, f"compresses to {ratio:.3f} of its length"
    if dominant >= _DOMINANT_TOKEN_FRACTION:
        return True, f"one token is {dominant:.0%} of the page"
    return False, ""


_DET_BOX_RE = re.compile(r"<\|det\|>\[\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]\]<\|/det\|>")
# A sideways scan on a portrait canvas puts every recognised line in a
# narrow vertical band where the rotated header lands. On the stored
# hallucinated pages the band is 4-21% of the page wide; the narrowest
# clean page spans 66%.
_VERTICAL_BAND_MAX_WIDTH = 0.30
_VERTICAL_BAND_MIN_HEIGHT = 0.40


def _boxes_in_vertical_band(text: str) -> bool:
    """True when the OCR's own line boxes sit in a tall, narrow band."""
    boxes = [tuple(int(g) for g in m.groups()) for m in _DET_BOX_RE.finditer(text or "")]
    if not boxes:
        return False
    width = (max(b[2] for b in boxes) - min(b[0] for b in boxes)) / 1000
    height = (max(b[3] for b in boxes) - min(b[1] for b in boxes)) / 1000
    return width < _VERTICAL_BAND_MAX_WIDTH and height > _VERTICAL_BAND_MIN_HEIGHT


_TEXT_LAYER_MIN_CHARS = 200
_TEXT_LAYER_DIGIT_RECALL = 0.70


def _digit_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,./-]*\d|\d", text or ""))


def _text_layer_disagrees(layer: str, ocr_text: str) -> bool:
    """True when the PDF's own text layer holds numbers the OCR lost.

    A born-digital page carries its text exactly; OCR re-samples it and,
    run to run, drops value cells while the labels survive. The layer is
    the second read that detects it for free.
    """
    want = _digit_tokens(layer)
    if len(want) < 5:
        return False
    have = _digit_tokens(_plain_text(ocr_text))
    return len(want & have) / len(want) < _TEXT_LAYER_DIGIT_RECALL


def _add_flag(result: dict, code: str) -> None:
    flags = result.setdefault("flag_details", [])
    if isinstance(flags, list) and code not in flag_codes(flags):
        flags.append(code)


_ROTATION_PROBE_FLAGS = frozenset({
    "possible_hallucination",
    "repetitive_content",
    "incomplete_extraction",
    "no_content",
    "ocr_failed",
    "low_quality_scan",
})
_ROTATION_PROBE_MAX_COMPOSITE = 0.7
_ROTATION_WIN_MARGIN = 0.1
# Content wider than tall by this factor = a landscape scan.
_LANDSCAPE_ASPECT = 1.15


def _content_is_landscape(image_path) -> bool:
    """True when the non-white content of a processed page is landscape.

    The preprocessor pads every page into a portrait canvas, so the frame
    dimensions say nothing — only the content bounding box does."""
    try:
        img = Image.open(image_path).convert("L")
    except OSError:
        return False
    bbox = img.point(lambda p: 255 if p < 245 else 0).getbbox()
    if not bbox:
        return False
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return w > h * _LANDSCAPE_ASPECT


def _rotation_probe(
    pdf_bytes: bytes,
    settings: Settings,
    processed_dir,
    processed_map: dict,
    ocr_results: dict[int, dict],
    beat=lambda: None,
) -> None:
    """Re-OCR suspect landscape pages at 90°/270°; keep decisive winners.

    Mutates ocr_results and the processed page images in place.
    """
    from app.services.ocr_service import composite_of, flag_codes

    suspects = []
    for page_num, result in ocr_results.items():
        unreliable = (
            result.get("needs_external_ocr")
            or composite_of(result) < _ROTATION_PROBE_MAX_COMPOSITE
            or bool(_ROTATION_PROBE_FLAGS & flag_codes(result.get("flag_details")))
        )
        if not unreliable:
            continue
        if _content_is_landscape(processed_map[page_num]):
            suspects.append(page_num)
        elif _boxes_in_vertical_band(result.get("text") or ""):
            logger.info(
                "Rotation probe: page %d has its OCR lines in a narrow vertical "
                "band — treating as a sideways scan", page_num,
            )
            suspects.append(page_num)
    if not suspects:
        return

    logger.info(
        "Phase B1.5: rotation probe for %d suspect landscape page(s): %s",
        len(suspects), suspects,
    )

    # Render candidates sequentially (PyMuPDF is not thread-safe), then OCR
    # them in parallel.
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        logger.exception("Rotation probe: could not reopen PDF — skipping")
        return
    candidates: list[tuple[int, int, object]] = []  # (page_num, angle, path)
    try:
        zoom = settings.render_dpi / 72
        for page_num in suspects:
            page = doc[page_num - 1]
            base_rotation = page.rotation
            for angle in (90, 270):
                page.set_rotation((base_rotation + angle) % 360)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                pil_image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples,
                )
                processed = preprocess_for_ocr(
                    pil_image,
                    max_width=settings.image_max_width,
                    max_height=settings.image_max_height,
                )
                path = processed_dir / f"page_{page_num}_rot{angle}.png"
                processed.save(str(path), "PNG")
                candidates.append((page_num, angle, path))
            page.set_rotation(base_rotation)
    finally:
        doc.close()

    def _probe_one(item: tuple[int, int, object]) -> tuple[int, int, dict | None]:
        page_num, angle, path = item
        try:
            return page_num, angle, ocr_single_image(path, settings, allow_fallback=False)
        except ProcessingError:
            return page_num, angle, None

    best: dict[int, tuple[int, dict, float]] = {}  # page -> (angle, result, composite)
    with ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as pool:
        for page_num, angle, result in pool.map(_probe_one, candidates):
            beat()
            if result is None:
                continue
            composite = composite_of(result)
            logger.info(
                "Rotation probe page=%d angle=%d°: composite %.2f (as-scanned %.2f)",
                page_num, angle, composite, composite_of(ocr_results[page_num]),
            )
            if page_num not in best or composite > best[page_num][2]:
                best[page_num] = (angle, result, composite)

    for page_num, (angle, result, composite) in best.items():
        base_result = ocr_results[page_num]
        base_composite = composite_of(base_result)
        # A rotation wins by decisively out-scoring the original — or by
        # matching it flag-clean when the original is flagged unreliable.
        # DeepSeek scores its own hallucinations high (observed: 0.84 for
        # invented content on a sideways page vs 0.85 for the true rotated
        # read), so score alone cannot break that tie; the reliability
        # flags can.
        base_bad = (
            base_result.get("needs_external_ocr")
            or bool(_ROTATION_PROBE_FLAGS & flag_codes(base_result.get("flag_details")))
        )
        cand_clean = not (_ROTATION_PROBE_FLAGS & flag_codes(result.get("flag_details")))
        decisive = composite >= base_composite + _ROTATION_WIN_MARGIN
        flag_win = base_bad and cand_clean and composite >= base_composite - 0.05
        if not (decisive or flag_win):
            continue
        winner_path = processed_dir / f"page_{page_num}_rot{angle}.png"
        try:
            winner_path.replace(processed_map[page_num])
        except OSError:
            logger.exception(
                "Rotation probe: could not replace processed image page=%d", page_num,
            )
            continue
        details = result.setdefault("flag_details", [])
        if isinstance(details, list) and "auto_rotated" not in details:
            details.append("auto_rotated")
        ocr_results[page_num] = result
        logger.info(
            "Rotation probe: page %d was scanned sideways — corrected at %d° "
            "(composite %.2f vs %.2f)",
            page_num, angle, composite, base_composite,
        )


_HEARTBEAT_MIN_INTERVAL = 30.0


def _throttled(heartbeat):
    """Wrap a progress callback so it fires at most twice a minute.

    Called once per page across four OCR phases, which on a large packet is
    several hundred calls. The consumer is a row update whose only purpose
    is to prove liveness, and nothing downstream reads it more finely than
    the watchdog's half-hour window.

    Failures are swallowed. A heartbeat exists to report that work is
    happening; letting it abort the work it reports on would invert that.
    """
    if heartbeat is None:
        return lambda: None

    state = {"last": 0.0}

    def beat() -> None:
        now = time.perf_counter()
        if now - state["last"] < _HEARTBEAT_MIN_INTERVAL:
            return
        state["last"] = now
        try:
            heartbeat()
        except Exception:
            logger.warning("Progress heartbeat failed", exc_info=True)

    return beat


def process_pdf(
    pdf_bytes: bytes, settings: Settings, work_dir=None, heartbeat=None,
) -> dict:
    """Split PDF into pages, pre-process images, run OCR, and save text per page.

    Output structure (under work_dir, default settings.output_dir):
        <work_dir>/processed/page_1.png, page_2.png, ...
        <work_dir>/texts/page_1.txt, page_2.txt, ...

    Concurrent jobs MUST pass distinct work_dirs — page files are named
    by page number only and would collide in a shared directory.
    """
    beat = _throttled(heartbeat)
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
    # The colour render, kept for the vision reads: the OCR preprocessor's
    # grayscale/CLAHE/sharpen canvas is tuned for the OCR engine and
    # flattens handwriting, strike-throughs and ink colour that a
    # transcription needs to see.
    original_map: dict[int, Path] = {}
    # The PDF's own text layer, when it has one. Born-digital pages carry
    # their text exactly; it is the cheapest second read there is.
    text_layer_map: dict[int, str] = {}
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
            try:
                orig = pil_image.copy()
                orig.thumbnail((settings.image_max_width, settings.image_max_height))
                orig_path = processed_dir / f"page_{page_num}.orig.jpg"
                orig.save(str(orig_path), "JPEG", quality=85)
                original_map[page_num] = orig_path
            except Exception:
                logger.exception("Could not keep the colour render for page %d", page_num)
            try:
                layer = page.get_text("text") or ""
                if len(layer.strip()) >= _TEXT_LAYER_MIN_CHARS:
                    text_layer_map[page_num] = layer
            except Exception:
                logger.exception("Could not read the text layer of page %d", page_num)
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
        "(preprocess_concurrency=%d, text layer on %d page(s)) — starting "
        "parallel OCR (concurrency=%d)",
        total_pages, time.perf_counter() - start,
        settings.preprocess_concurrency, len(text_layer_map),
        settings.ocr_concurrency,
    )

    # Phase B: OCR in parallel. The OCR service handles ocr_concurrency
    # requests concurrently; ThreadPoolExecutor is safe here because
    # ocr_single_image only does I/O (HTTP POST).
    def _ocr_one(
        item: tuple[int, Path], *, allow_fallback: bool = True,
    ) -> tuple[int, dict]:
        page_num, path = item
        try:
            result = ocr_single_image(path, settings, allow_fallback=allow_fallback)
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

    # The first pass uses the primary tier only. A page scanned sideways
    # fails DeepSeek, falls through to the GLM tier over the network, reads
    # badly there too because it is still sideways, and only then reaches
    # the rotation probe that would have fixed it. Rotation is decided
    # first, so the expensive tier is spent only on pages whose problem
    # survives being turned the right way up.
    #
    # Measured on a 119-page packet where 75% of pages were scanned
    # sideways: 21 minutes for the first pass with the fallback enabled,
    # against 3 minutes for 202 probe reads without it.
    ocr_results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as pool:
        for page_num, result in pool.map(
            lambda item: _ocr_one(item, allow_fallback=False), processed_paths,
        ):
            ocr_results[page_num] = result
            beat()

    # Phase B1.5: rotation probe for sideways scans.
    # Portrait forms scanned into landscape pages (rotation flag 0) reach
    # OCR rotated 90°. DeepSeek-OCR cannot read sideways text — it
    # hallucinates plausible-looking tables instead of failing, and some
    # hallucinated pages even score above the vision thresholds (observed:
    # a sideways HUD 50059 rendered as 35KB of invented content at 0.62).
    # Suspect pages whose CONTENT is landscape get re-rendered from the PDF
    # at 90°/270° at full DPI — rotating the processed canvas instead loses
    # too much resolution to recover dense forms — and the orientation that
    # scores decisively best wins. The corrected image replaces the
    # processed page so classification, extraction, and vision fallback all
    # see the upright page. (180° upside-down scans are out of scope: their
    # content stays portrait, and DeepSeek copes with them far better.)
    _rotation_probe(
        pdf_bytes, settings, processed_dir, processed_map, ocr_results, beat,
    )

    # Phase B1.6: the secondary OCR tier, now that orientation is settled.
    # Only pages the primary tier could not read upright reach it, which on
    # a sideways-scanned packet is a small fraction of what would otherwise
    # have gone through. Pages the probe corrected are re-read here from the
    # replaced image if they are still unreliable.
    if settings.ocr_fallback_url:
        from app.services.ocr_service import composite_of

        needs_second_tier = [
            (page_num, processed_map[page_num])
            for page_num, result in ocr_results.items()
            if result.get("needs_external_ocr")
        ]
        if needs_second_tier:
            logger.info(
                "Phase B1.6: secondary OCR tier for %d page(s) still "
                "unreadable after rotation: %s",
                len(needs_second_tier), [p for p, _ in needs_second_tier],
            )
            with ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as pool:
                for page_num, result in pool.map(_ocr_one, needs_second_tier):
                    beat()
                    # Keep the better read rather than assuming the second
                    # tier improves on the first — it is a different engine,
                    # not a strictly better one.
                    if composite_of(result) >= composite_of(ocr_results[page_num]):
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
        # Composite score below the vision threshold: the OCR service itself
        # judged its output unreliable (hallucination, sparse content, bad
        # scan) even if it didn't ask for external OCR outright.
        score = ocr_result.get("score")
        composite = score.get("composite") if isinstance(score, dict) else score
        try:
            composite = float(composite) if composite is not None else None
        except (TypeError, ValueError):
            composite = None
        if composite is not None and composite < settings.ocr_vision_threshold:
            logger.warning(
                "Page %d: OCR composite %.2f below vision threshold %.2f — "
                "queueing vision fallback",
                page_num, composite, settings.ocr_vision_threshold,
            )
            low_quality_pages.append(page_num)
            continue
        service_flags = _VISION_QUEUE_FLAGS & flag_codes(ocr_result.get("flag_details"))
        if service_flags:
            logger.warning(
                "Page %d: OCR service flagged its own output (%s) at composite "
                "%s — queueing vision fallback",
                page_num, ", ".join(sorted(service_flags)),
                f"{composite:.2f}" if composite is not None else "?",
            )
            low_quality_pages.append(page_num)
            continue
        degenerate, why = _is_degenerate(ocr_result.get("text") or "")
        if degenerate:
            logger.warning(
                "Page %d: OCR text is a decoder loop (%s) — queueing vision "
                "fallback", page_num, why,
            )
            _add_flag(ocr_result, "degenerate_text")
            low_quality_pages.append(page_num)
            continue
        repetitive = _repetitive_fraction(ocr_result.get("text") or "")
        if repetitive >= _REPETITIVE_FRACTION:
            logger.warning(
                "Page %d: ~%.0f%% of OCR text is a repeated short pattern — "
                "barcode or scan artefact read as characters; queueing "
                "vision fallback", page_num, repetitive * 100,
            )
            _add_flag(ocr_result, "repetitive_content")
            low_quality_pages.append(page_num)
            continue
        # Ink density against the text the extractor will actually see, not
        # the raw string: grounding markup is 13-66% of the raw length on
        # sparse pages and hid real content loss behind it.
        text_len = len(_plain_text(ocr_result.get("text") or ""))
        img_path = path_by_page.get(page_num)
        if img_path and suspected_content_loss(img_path, text_len):
            logger.warning(
                "Page %d: OCR returned %d chars but the page's ink density "
                "implies far more — suspected content loss, queueing vision "
                "fallback", page_num, text_len,
            )
            _add_flag(ocr_result, "suspected_content_loss")
            low_quality_pages.append(page_num)
            continue
        layer = text_layer_map.get(page_num)
        if layer and _text_layer_disagrees(layer, ocr_result.get("text") or ""):
            logger.warning(
                "Page %d: the PDF text layer holds numbers the OCR read "
                "lost — queueing a second read", page_num,
            )
            _add_flag(ocr_result, "text_layer_disagrees")
            low_quality_pages.append(page_num)
            continue
        # DeepSeek-OCR marks regions it could not read as
        # <|ref|>image<|/ref|> with a bounding box. A large skipped
        # region on a form page is unread content (typically the
        # handwritten fill-in block on a self-cert) even when the
        # printed boilerplate keeps char counts and quality scores
        # high. Coordinates are on a 0-1000 grid: area is normalized
        # by 1000x1000.
        skipped = _skipped_region_fraction(ocr_result.get("text") or "")
        if skipped >= 0.12:
            logger.warning(
                "Page %d: OCR skipped ~%.0f%% of the page as unread "
                "image region(s) — queueing vision fallback",
                page_num, skipped * 100,
            )
            _add_flag(ocr_result, "unread_region")
            low_quality_pages.append(page_num)

    # Phase B1.9: pages queued for a second read that carry a PDF text
    # layer take it verbatim — exact, deterministic, free — and leave the
    # vision queue. Scanned packets have no layer and fall through.
    if low_quality_pages:
        layered = [pn for pn in low_quality_pages if pn in text_layer_map]
        for page_num in layered:
            layer = text_layer_map[page_num]
            logger.info(
                "Page %d: replaced %d chars of OCR text with the PDF text layer "
                "(%d chars)", page_num, len(ocr_results[page_num].get("text") or ""),
                len(layer),
            )
            ocr_results[page_num]["text"] = layer
            ocr_results[page_num]["flag"] = "green"
            ocr_results[page_num]["flag_message"] = "Text taken from the PDF text layer"
            ocr_results[page_num]["needs_external_ocr"] = False
            _add_flag(ocr_results[page_num], "text_layer")
        low_quality_pages = [pn for pn in low_quality_pages if pn not in text_layer_map]

    if low_quality_pages:
        from app.services.llm_service import call_llm_vision
        low_quality_pages.sort()
        logger.info(
            "Phase B2: Vision fallback for %d low-quality OCR page(s): %s",
            len(low_quality_pages), low_quality_pages,
        )

        _VISION_PROMPT = (
            "Transcribe ALL text on this document page so that a text-only reader "
            "can recover every field on it. Preserve the structure:\n"
            "- Reproduce tables using HTML <table> tags, one row per row, keeping "
            "every column and its header\n"
            "- Keep each field label together with the value in its cell, on one "
            "line: 'Tenant Rent: $953.00'\n"
            "- When a value cell is EMPTY, write the label followed by [blank]\n"
            "- Checkboxes: '[X] Label' or '[ ] Label'\n"
            "- Transcribe handwriting exactly as written; wrap text that has been "
            "struck through in ~~double tildes~~ and keep any initials beside it\n"
            "- Include all dollar amounts, dates, names, account numbers and form "
            "field numbers (e.g. '12. Effective Date', '86. Total Annual Income') "
            "exactly as shown; never derive a value from another field\n"
            "- Note a signature as [signature present] or [signature line blank]\n"
            "Return ONLY the transcription, no commentary."
        )

        def _vision_one(page_num: int) -> tuple[int, str | None]:
            img_path = original_map.get(page_num) or path_by_page.get(page_num)
            if not img_path:
                return page_num, None
            try:
                return page_num, call_llm_vision(
                    _VISION_PROMPT,
                    f"Transcribe page {page_num} of this document.",
                    [str(img_path)],
                    settings,
                    thinking={"type": "disabled"},
                    reject_truncated=True,
                )
            except Exception:
                logger.exception(
                    "Vision fallback page=%d failed — keeping original OCR text",
                    page_num,
                )
                return page_num, None

        # Flags that mean the OCR text is fabricated or broken, not merely
        # incomplete. Vision output must replace such text even when it is
        # SHORTER: hallucinated tables run to tens of thousands of chars, so
        # a longer-is-better rule would keep the garbage every time
        # (observed: a rotated HUD 50059 whose 35KB hallucination beat the
        # real ~4KB of vision-read content).
        _UNRELIABLE_OCR_FLAGS = {
            "possible_hallucination", "no_content", "ocr_failed",
            "low_quality_scan", "max_tokens_hit", "repetitive_content",
            "degenerate_text", "incomplete_extraction",
        }
        # Queued because part of the page was unread, not because the read
        # text is wrong: a shorter vision read is still worth keeping, as an
        # addition rather than a replacement.
        _PARTIAL_READ_FLAGS = {"unread_region", "suspected_content_loss", "text_layer_disagrees"}
        _MIN_VISION_CHARS = 200

        with ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as pool:
            for page_num, vision_text in pool.map(_vision_one, low_quality_pages):
                beat()
                # Compare what the extractor will see: grounding markup is
                # 13-66% of the raw OCR length on sparse pages, and a
                # longer-raw-string rule kept the unread text every time.
                ocr_plain_len = len(_plain_text(ocr_results[page_num].get("text", "")))
                vision_plain_len = len(_plain_text(vision_text or ""))
                page_flags = flag_codes(ocr_results[page_num].get("flag_details"))
                ocr_unreliable = bool(_UNRELIABLE_OCR_FLAGS & page_flags)
                if vision_text and (
                    vision_plain_len > ocr_plain_len
                    or (ocr_unreliable and vision_plain_len >= _MIN_VISION_CHARS)
                ):
                    logger.info(
                        "Vision fallback page=%d: replaced %d chars with %d chars",
                        page_num, ocr_plain_len, vision_plain_len,
                    )
                    ocr_results[page_num]["text"] = vision_text
                    # Provenance, not a quality verdict: the text in use is
                    # a transcription of the page. Scoring "not found" as
                    # "poor OCR" on the best-read pages was backwards.
                    ocr_results[page_num]["flag"] = "green"
                    ocr_results[page_num]["flag_message"] = "Text re-extracted via Vision fallback"
                    ocr_results[page_num]["needs_external_ocr"] = False
                    _add_flag(ocr_results[page_num], "vision_fallback")
                elif vision_text and page_flags & _PARTIAL_READ_FLAGS and vision_plain_len >= 40:
                    logger.info(
                        "Vision fallback page=%d: appended a %d-char vision read to "
                        "the %d-char OCR text (page was queued for unread content)",
                        page_num, vision_plain_len, ocr_plain_len,
                    )
                    ocr_results[page_num]["text"] = (
                        (ocr_results[page_num].get("text") or "").rstrip()
                        + "\n\n[Vision read of this page]\n" + vision_text.strip()
                    )
                    _add_flag(ocr_results[page_num], "vision_appended")
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
            "original_image": str(original_map[page_num]) if page_num in original_map else None,
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
    heartbeat=None,
) -> dict:
    """Full pipeline: OCR all pages, then classify, extract, and validate.

    Returns both the OCR results and the structured MuleSoft extraction.
    """
    # Stage 1: OCR
    ocr_result = process_pdf(
        pdf_bytes, settings, work_dir=work_dir, heartbeat=heartbeat,
    )

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
            "image_path": p.get("original_image") or p.get("processed_image"),
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
