"""Fetching a case packet from the URLs Cartograph sends.

Cartograph stores uploads in S3 via Active Storage, which assigns randomized
blob keys — the engine cannot locate a packet by listing a bucket, only by
using a link it was handed. Those links are presigned and expire, so the
download happens as the first act of the background task rather than
whenever the extraction happens to reach the front of the queue.

A case may arrive as several documents. They are merged into one PDF before
extraction, because the audit is inherently cross-document: paystubs are
compared against the verification of income, the certification total against
the sum of its sources. Extracting them separately would produce confident
findings about missing documentation that is sitting in the next file.
"""

import logging

import fitz  # PyMuPDF
import httpx

logger = logging.getLogger(__name__)

# Bounds a single document. A certification packet is tens of megabytes at
# most; anything past this is a misconfiguration or a wrong link, and it
# should fail fast rather than fill the pod's disk.
MAX_DOCUMENT_BYTES = 200 * 1024 * 1024


class DocumentUnavailable(RuntimeError):
    """A document could not be fetched.

    Distinct from a processing failure: the packet never arrived, so there is
    nothing to retry locally. Recovery needs a fresh URL from Cartograph.
    """


def fetch_document(url: str, timeout: float = 120.0) -> bytes:
    """Download one document. Raises DocumentUnavailable on any failure."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    # 403 on a presigned URL almost always means expired
                    # rather than forbidden, and the distinction matters when
                    # reading logs later.
                    hint = (
                        " (presigned URL likely expired)"
                        if response.status_code == 403 else ""
                    )
                    raise DocumentUnavailable(
                        f"HTTP {response.status_code} fetching document{hint}"
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_DOCUMENT_BYTES:
                        raise DocumentUnavailable(
                            f"document exceeds {MAX_DOCUMENT_BYTES} bytes"
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise DocumentUnavailable(f"network error fetching document: {exc}") from exc

    body = b"".join(chunks)
    if not body:
        raise DocumentUnavailable("document is empty")
    return body


# What an attachment is, from its first bytes — never from its filename or
# the content type the link claims. A reviewer's "VOI scan" screenshot is a
# page of evidence and is read like one; a text file or a spreadsheet is
# not a page and is left out with a warning rather than failing the case.
_MAGIC = (
    (b"%PDF", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"BM", "bmp"),
)
_IMAGE_KINDS = {"png", "jpeg", "gif", "tiff", "bmp", "webp"}


def sniff_kind(body: bytes) -> str | None:
    """'pdf', an image kind, or None for anything the engine cannot page."""
    head = body[:16]
    if head[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    # A PDF with junk before its header still opens; look a little further.
    if b"%PDF" in body[:1024]:
        return "pdf"
    return None


def _image_as_pdf(body: bytes, kind: str) -> bytes:
    with fitz.open(stream=body, filetype=kind) as image:
        return image.convert_to_pdf()


def assemble_packet(parts: list[tuple[str, bytes]]) -> tuple[bytes, list[str]]:
    """One PDF from the attachments of a case, in the order given, and a
    warning for each attachment that could not become pages.

    A PDF contributes its pages; an image becomes one page; anything else
    is skipped and named in the warnings. Raises DocumentUnavailable when
    nothing at all could be paged, which is the same situation as no
    document arriving.
    """
    merged, warnings, _ = _assemble(parts)
    return merged, warnings


def _assemble(parts: list[tuple[str, bytes]]) -> tuple[bytes, list[str], list[tuple[str, int, int]]]:
    """assemble_packet plus, for each attachment that became pages, its
    name and the 1-based first and last packet page it occupies — the
    record of which request document a packet page came from."""
    warnings: list[str] = []
    pdfs: list[tuple[str, bytes]] = []
    for name, body in parts:
        kind = sniff_kind(body)
        if kind == "pdf":
            pdfs.append((name, body))
        elif kind in _IMAGE_KINDS:
            try:
                pdfs.append((name, _image_as_pdf(body, kind)))
                logger.info("Attachment %s is a %s image; added as one page", name, kind)
            except Exception as exc:  # a corrupt image is skipped, not fatal
                warnings.append(f"attachment {name} ({kind} image) could not be rendered as a page: {exc}")
                logger.warning("Attachment %s (%s) could not be rendered: %s", name, kind, exc)
        else:
            warnings.append(f"attachment {name} is not a PDF or an image and was left out of the audit")
            logger.warning("Attachment %s is not a PDF or an image (starts %r); left out", name, body[:8])
    if not pdfs:
        raise DocumentUnavailable(
            "no attachment could be read as pages: " + "; ".join(warnings) if warnings else
            "no attachment could be read as pages"
        )
    spans: list[tuple[str, int, int]] = []
    merged = fitz.open()
    try:
        for name, body in pdfs:
            with fitz.open(stream=body, filetype="pdf") as part:
                first = merged.page_count + 1
                merged.insert_pdf(part)
                spans.append((name, first, merged.page_count))
        if len(pdfs) == 1:
            return pdfs[0][1], warnings, spans
        return merged.tobytes(), warnings, spans
    finally:
        merged.close()


def merge_pdfs(documents: list[bytes]) -> bytes:
    """Combine several PDFs into one, in the order given."""
    merged_bytes, _ = assemble_packet([(f"document {i}", b) for i, b in enumerate(documents)])
    return merged_bytes


def fetch_packet(documents: list[dict]) -> tuple[bytes, list[str], list[dict]]:
    """Download every document for a case and return one PDF, the
    warnings for attachments that could not be paged, and one span per
    attachment that became pages: its `job_document_id`, filename and
    the packet pages it occupies, so a figure can be traced back to the
    request document it was read from.

    `documents` is the array from the audit notification; each entry needs a
    `url`. Entries without one are skipped with a warning rather than failing
    the case, since a packet with three of four files is still worth auditing
    and the gap shows up as a finding.
    """
    parts: list[tuple[str, bytes]] = []
    warnings: list[str] = []
    by_name: dict[str, dict] = {}
    for index, doc in enumerate(documents):
        url = doc.get("url")
        name = doc.get("filename") or f"documents[{index}]"
        by_name.setdefault(name, doc)
        if not url:
            logger.warning("documents[%d] has no url; skipped", index)
            warnings.append(f"attachment {name} had no download link and was left out")
            continue
        body = fetch_document(url)
        logger.info(
            "Fetched documents[%d] %s (%d bytes)",
            index, doc.get("filename") or "unnamed", len(body),
        )
        parts.append((name, body))

    if not parts:
        raise DocumentUnavailable("no documents could be fetched for this case")

    merged_bytes, skipped, spans = _assemble(parts)
    document_spans = [
        {"job_document_id": (by_name.get(name) or {}).get("job_document_id"),
         "filename": name, "first_page": first, "last_page": last}
        for name, first, last in spans
    ]
    return merged_bytes, warnings + skipped, document_spans
