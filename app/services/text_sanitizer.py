"""Central text sanitizer — cleans OCR output for all downstream extractors.

Applied once during page grouping so every extractor (parsers, LLM, Vision)
works on consistent clean text. The raw HTML is preserved in a separate field
for parsers that need table structure.
"""

import re


def sanitize_for_extraction(text: str) -> str:
    """Clean OCR text for LLM and regex extraction.

    Removes:
    - OCR escape artifacts: \\( \\) \\[ \\]
    - HTML entities: &amp; &lt; &gt; &#x27; etc.
    - Grounding/detection tags: <|ref|>, <|det|>
    - Excessive whitespace

    Preserves:
    - HTML table structure (for table_utils parsers)
    - Line breaks (important for section detection)
    """
    if not text:
        return ""

    # 1. Strip grounding/detection tags (OCR model artifacts)
    clean = re.sub(r"<\|ref\|>.*?<\|/ref\|>", "", text, flags=re.DOTALL)
    clean = re.sub(r"<\|det\|>.*?<\|/det\|>", "", clean, flags=re.DOTALL)

    # 2. Decode HTML entities
    clean = clean.replace("&amp;amp;", "&")
    clean = clean.replace("&amp;", "&")
    clean = clean.replace("&lt;", "<")
    clean = clean.replace("&gt;", ">")
    clean = clean.replace("&#x27;", "'")
    clean = clean.replace("&quot;", '"')
    clean = clean.replace("&#39;", "'")

    # 3. Fix OCR escape artifacts: \\( \\) → remove
    clean = re.sub(r"\\+[()]", "", clean)

    # 4. Collapse runs of whitespace (but preserve newlines)
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)

    return clean.strip()


def strip_html(text: str) -> str:
    """Remove all HTML tags, returning plain text.

    Use this when you need pure text (e.g., for keyword matching, LLM prompts).
    For table parsing, use the raw text with sanitize_for_extraction() instead.
    """
    clean = sanitize_for_extraction(text)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def clean_extracted_value(value: str | None) -> str | None:
    """Clean a single extracted field value — remove HTML fragments and artifacts."""
    if value is None:
        return None

    val = str(value).strip()
    if not val:
        return None

    # Remove HTML tags
    val = re.sub(r"<[^>]+>", "", val)
    # Remove HTML entities
    val = val.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    val = val.replace("&#x27;", "'").replace("&quot;", '"')
    # Remove OCR escapes
    val = re.sub(r"\\+[()]", "", val)
    # Collapse whitespace
    val = re.sub(r"\s+", " ", val).strip()

    # If result is empty or just punctuation, return None
    if not val or len(val) < 2 or all(c in ".,;:!?- " for c in val):
        return None

    return val


def _strip_markup_only(value: str | None) -> str | None:
    """Sibling of clean_extracted_value that preserves short legitimate values.

    Strips HTML tags / entities / OCR escapes and collapses whitespace, but
    does NOT impose a minimum length. Single-character flag values ('Y'/'N')
    and small numeric strings ('1', '2') must survive — they're legitimate
    values for fields like head/disabled/student/numberOfBedrooms.

    Returns None only when the result is empty or purely punctuation/markup
    leftover. Use this for bulk recursive scrubbing; use
    clean_extracted_value() for fields where a sub-2-char value would
    always be noise (regex-matched document names, source labels, etc.).
    """
    if value is None:
        return None
    val = str(value).strip()
    if not val:
        return None
    val = re.sub(r"<[^>]+>", "", val)
    val = val.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    val = val.replace("&#x27;", "'").replace("&quot;", '"').replace("&#39;", "'")
    val = val.replace("&nbsp;", " ")
    val = re.sub(r"\\+[()]", "", val)
    val = re.sub(r"\s+", " ", val).strip()
    if not val or all(c in ".,;:!?- " for c in val):
        return None
    return val


def scrub_extracted_dict(data):
    """Recursively clean every string leaf in an LLM-extracted structure.

    Walks dicts and lists, stripping HTML tags / entities / OCR escapes
    from each string. Values that reduce to empty or pure-punctuation are
    replaced with None so downstream validators see absence rather than
    markup. Returns a new structure of the same shape; the input is not
    mutated.

    Defense in depth against OCR'd HTML-rendered PDFs whose tag fragments
    would otherwise propagate into structured fields. Uses the
    short-value-tolerant cleaner so legitimate single-character values
    ('Y'/'N' flags, '1'/'2' counts) survive — record-level identity
    gating is the right place to drop garbage records, not field-level
    length minimums.
    """
    if isinstance(data, dict):
        return {k: scrub_extracted_dict(v) for k, v in data.items()}
    if isinstance(data, list):
        return [scrub_extracted_dict(item) for item in data]
    if isinstance(data, str):
        return _strip_markup_only(data)
    return data


def drop_records_without_identity(
    records: list,
    identity_fields: tuple[str, ...],
) -> tuple[list, int]:
    """Drop list entries whose identity fields are all empty/None.

    A record with no usable identity field can't be linked to a member or
    source downstream, so every per-field finding generated against it is
    derivative noise. Returns (kept, dropped_count). Non-dict entries are
    passed through untouched.
    """
    if not records:
        return records, 0
    kept: list = []
    dropped = 0
    for r in records:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        if any(r.get(f) for f in identity_fields):
            kept.append(r)
        else:
            dropped += 1
    return kept, dropped
