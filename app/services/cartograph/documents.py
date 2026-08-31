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


def merge_pdfs(documents: list[bytes]) -> bytes:
    """Combine several PDFs into one, in the order given."""
    if len(documents) == 1:
        return documents[0]

    merged = fitz.open()
    try:
        for body in documents:
            with fitz.open(stream=body, filetype="pdf") as part:
                merged.insert_pdf(part)
        return merged.tobytes()
    finally:
        merged.close()


def fetch_packet(documents: list[dict]) -> bytes:
    """Download every document for a case and return one PDF.

    `documents` is the array from the audit notification; each entry needs a
    `url`. Entries without one are skipped with a warning rather than failing
    the case, since a packet with three of four files is still worth auditing
    and the gap shows up as a finding.
    """
    bodies: list[bytes] = []
    for index, doc in enumerate(documents):
        url = doc.get("url")
        if not url:
            logger.warning("documents[%d] has no url; skipped", index)
            continue
        body = fetch_document(url)
        logger.info(
            "Fetched documents[%d] %s (%d bytes)",
            index, doc.get("filename") or "unnamed", len(body),
        )
        bodies.append(body)

    if not bodies:
        raise DocumentUnavailable("no documents could be fetched for this case")

    return merge_pdfs(bodies)
