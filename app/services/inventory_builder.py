"""Document inventory builder — derives inventories from classification without LLM."""

import logging
import re

from app.schemas.extraction import (
    DocumentGroup,
    DocumentInventory,
    DocumentInventoryEntry,
)
from app.services.validation import normalize_date, to_title_case

logger = logging.getLogger(__name__)

# HUD compliance form types (handled by HUD inventory)
from app.services.doc_taxonomy import FAMILY_COMPLIANCE, labels_in_family

# Every compliance-family label in the taxonomy; a literal copy here had
# drifted ("Acknowledgement of Receipt of HUD Forms", "EIV Income Report")
# and the VAWA addendum landed in the financial inventory.
_HUD_TYPES: frozenset[str] = labels_in_family(FAMILY_COMPLIANCE)

# Types to exclude entirely from financial inventory
_EXCLUDE_FROM_FINANCIAL: set[str] = {
    "Blank Form", "Blank Page", "File Order Form",
}

# System-generated forms (not signed)
_SYSTEM_GENERATED: set[str] = {
    "EIV Summary Report", "EIV Income Report",
}


def build_financial_inventory(
    groups: list[DocumentGroup],
) -> DocumentInventory:
    """Build financial document inventory from classified groups.

    No LLM call needed — uses classification + text analysis.
    """
    documents: list[DocumentInventoryEntry] = []

    for g in groups:
        # Skip HUD forms (separate inventory) and excluded types
        if g.document_type in _HUD_TYPES:
            continue
        if g.document_type in _EXCLUDE_FROM_FINANCIAL:
            continue

        entry = _build_entry(g)

        # Add notes for calculation worksheets
        if "calculation" in g.document_type.lower() or "calc" in g.document_type.lower():
            entry.notes = "Internal calculation document — excluded from IDP data extraction per Document Exclusion rules"

        documents.append(entry)

    logger.info("Financial inventory: %d documents cataloged (no LLM)", len(documents))
    return DocumentInventory(documents=documents)


def build_hud_inventory(
    groups: list[DocumentGroup],
) -> DocumentInventory:
    """Build HUD compliance form inventory from classified groups.

    No LLM call needed.
    """
    documents: list[DocumentInventoryEntry] = []

    for g in groups:
        # Only include HUD types or compliance category docs that might be HUD forms
        is_hud = g.document_type in _HUD_TYPES
        is_compliance_hud = g.category == "compliance" and any(
            kw in g.document_type.lower()
            for kw in ("hud", "9887", "92006", "eiv", "race", "ethnic", "acknowledgement",
                        "authorization", "consent", "release", "citizenship")
        )

        if not is_hud and not is_compliance_hud:
            continue

        entry = _build_entry(g)

        # Mark system-generated forms
        if g.document_type in _SYSTEM_GENERATED:
            entry.isSigned = "N/A"

        documents.append(entry)

    logger.info("HUD inventory: %d documents cataloged (no LLM)", len(documents))
    return DocumentInventory(documents=documents)


def _build_entry(g: DocumentGroup) -> DocumentInventoryEntry:
    """Build a single inventory entry from a document group."""
    signed = _detect_signature(g.combined_text)
    signer = _extract_signer(g.combined_text)
    sig_date = _extract_signature_date(g.combined_text)
    sig_state = "read" if sig_date else _signature_date_state(g.combined_text)
    doc_date = _extract_document_date(g.combined_text)

    return DocumentInventoryEntry(
        documentType=g.document_type,
        documentTitle=g.document_type,
        sourceOrganization=_extract_organization(g.combined_text),
        personName=to_title_case(g.person_name),
        pageRange=g.page_range,
        pageCount=len(g.pages),
        isSigned=signed,
        signedBy=to_title_case(signer),
        signatureDate=sig_date,
        signatureDateState=sig_state,
        documentDate=doc_date,
        notes=g.notes,
    )


def _detect_signature(text: str) -> str:
    """Detect if a document appears to be signed.

    Returns 'Yes', 'No', or 'N/A'.
    """
    text_lower = text.lower()

    # Look for signature with actual content after it (not just the label)
    sig_patterns = [
        r"(?:signature|signed)\s*(?:of|by)?\s*(?:household|head|owner|agent|employer|tenant|resident)\s*[:\s]+([A-Za-z])",
        r"(?:signature|signed)\s*[:\s]+([A-Za-z]{2,})",
        r"(?:Accepted|Acknowledged)\s*[:\s]+([A-Za-z]{2,})",
    ]
    for pattern in sig_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return "Yes"

    # Check for signature labels (suggests form expects signature but may not have one)
    if re.search(r"signature|sign here|signed", text_lower):
        return "No"

    return "N/A"


def _extract_signer(text: str) -> str | None:
    """Extract the name of the person who signed."""
    patterns = [
        r"Signature of Household Head:\s*(.+?)\s+Date",
        r"Signed by:\s*(.+?)(?:\n|Date)",
        r"Owner/Agent[:\s]+(.+?)\s+Date",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if name and len(name) > 1 and not name.startswith("_"):
                return name
    return None


def _extract_signature_date(text: str) -> str | None:
    """Extract the signature date."""
    # Look for "Date:" followed by a date, near signature context
    patterns = [
        r"(?:signature|signed).*?Date[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
        r"Date[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\s*$",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return normalize_date(m.group(1))
    return None


# The date slot beside a signature: the "Date" label that follows the
# signature label on the same line or the next, and whatever sits after it
# up to the end of the line.
_SIGNATURE_DATE_SLOT_RE = re.compile(
    r"(?:signature|signed)[^\n]{0,80}?\bdate[^\n]{0,10}?[:\s_]*([^\n]*)"
    r"|(?:signature|signed)[^\n]*\n[^\n]{0,40}?\bdate[^\n]{0,10}?[:\s_]*([^\n]*)",
    re.IGNORECASE,
)
# Marks a form prints in an empty slot, and the OCR's own placeholder for
# a region it could not read.
_SLOT_FILLER_RE = re.compile(r"[_\-.\s|:]+|<\|ref\|>.*?<\|/ref\|>|<\|det\|>.*?<\|/det\|>")


def _signature_date_state(text: str) -> str | None:
    """Whether the date slot beside the signature was written in.

    "unclear" when the slot holds characters that did not read as a date
    (handwriting the OCR could not resolve), "blank" when it holds nothing
    but the form's own underline, None when no such slot was found. Only
    consulted when no signature date was read, so the two reasons a date is
    missing — unreadable and absent — are reported as what they are.
    """
    for m in _SIGNATURE_DATE_SLOT_RE.finditer(text):
        content = m.group(1) if m.group(1) is not None else m.group(2)
        if content is None:
            continue
        # The slot ends at the next label on the line ("Date: ___ Unit: 4B").
        content = re.split(r"\s{2,}|\t|\b[A-Z][a-z]+\s*:", content, maxsplit=1)[0]
        if re.search(r"<\|ref\|>", content):
            return "unclear"
        remaining = _SLOT_FILLER_RE.sub("", content)
        return "unclear" if remaining else "blank"
    return None


def _extract_document_date(text: str) -> str | None:
    """Extract the primary date of the document."""
    patterns = [
        r"Effective Date[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{4})",
        r"Date[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{4})",
        r"Statement Date[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{4})",
        r"Date (?:Mailed|Received|Completed)[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return normalize_date(m.group(1))
    return None


def _extract_organization(text: str) -> str | None:
    """Extract the source organization from document text."""
    from app.services.text_sanitizer import clean_extracted_value, strip_html

    # Work on plain text (no HTML tags)
    clean = strip_html(text)

    patterns = [
        r"(?:Property|Project) Name[:\s]+(.+?)(?:\n|$)",
        r"(?:Company|Employer|Agency|From)[:\s]+(.+?)(?:\n|$)",
        r"(EQUIFAX|Social Security Administration)",
    ]
    for pattern in patterns:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            name = clean_extracted_value(m.group(1))
            if name and len(name) > 1:
                return to_title_case(name)
    return None
