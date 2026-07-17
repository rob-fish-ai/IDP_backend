"""LLM-only classifier — classify + group pages in a single LLM call.

Flow:
  1. Minimal pre-filter: drop only genuinely empty pages (no text at all).
     Watermark-flagged and low-quality pages are KEPT and passed to the LLM
     with their OCR quality flags as metadata so the LLM can decide.
  2. Build a short text snippet per live page (~400 chars + key identifiers).
  3. One LLM call: classify every page AND group multi-page documents.
  4. Deterministic post-group split: force-split cert forms that contain
     both current and previous certs (different effective dates / incomes).

No keyword patterns. Adding a new form type requires no code change —
update the canonical document-type list in the LLM prompt only.
"""

import logging
import re

from app.core.config import Settings
from app.core.exceptions import ClassificationUnavailableError
from app.schemas.extraction import ClassificationResult, DocumentGroup, PageClassification
from app.services.llm_service import call_llm_json
from app.services.text_sanitizer import sanitize_for_extraction, strip_html

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pass 1: minimal pre-filter + snippet build
# ---------------------------------------------------------------------------

def _prefilter_and_snippet(page_texts: list[dict]) -> list[dict]:
    """Build a snippet for each page. Only drop pages with NO text at all.

    Pages with watermarks, low OCR quality, or unusual content are kept —
    their OCR quality flags are attached and forwarded to the LLM, which
    decides whether the page is blank, a form, or something else.

    Returns list of:
      {"page": int, "snippet": str, "text": str, "skip": bool, "flags": list[str]}
    """
    results = []

    for pt in page_texts:
        page_num = pt["page"]
        text = pt["text"] or ""
        flag_details = pt.get("ocr_flag_details") or []
        if not isinstance(flag_details, list):
            flag_details = []

        flag_names = _flag_names(flag_details)
        stripped = text.strip()

        # OCR-failure flags mean the page HAS content but OCR couldn't read it.
        # Send these to the LLM with an explicit marker so they appear in the
        # classification output as Unknown/human-review instead of vanishing.
        ocr_failed = any(
            f in flag_names for f in ("ocr_failed", "low_quality_scan")
        )

        if not stripped and not ocr_failed:
            # Truly empty page with no OCR failure hint → safe to drop.
            results.append({
                "page": page_num,
                "snippet": "[no text extracted]",
                "text": text,
                "skip": True,
                "flags": flag_details,
            })
            continue

        if not stripped and ocr_failed:
            # OCR failed on a real page — send to LLM anyway with a marker.
            results.append({
                "page": page_num,
                "snippet": "[OCR failed — page has content but text could not be extracted]",
                "text": text,
                "skip": False,
                "flags": flag_details,
            })
            continue

        snippet = _make_snippet(strip_html(text))
        results.append({
            "page": page_num,
            "snippet": snippet,
            "text": text,
            "skip": False,
            "flags": flag_details,
        })

    return results


def _flag_names(flags: list) -> list[str]:
    """Normalize OCR flag entries to a list of short string labels.

    Flag entries may be plain strings OR dicts with a 'type'/'flag'/'name' key.
    """
    names: list[str] = []
    for f in flags or []:
        if isinstance(f, str):
            names.append(f)
        elif isinstance(f, dict):
            name = f.get("type") or f.get("flag") or f.get("name") or f.get("code")
            if name:
                names.append(str(name))
    return names


def _make_snippet(clean: str) -> str:
    """Build a ~450 char snippet preserving key identifiers for the LLM.

    Head of the page plus any dollar amounts, dates, and proper names found
    in the first 800 chars — enough for the LLM to recognize form type.
    """
    head = re.sub(r"\s+", " ", clean[:350]).strip()

    extras = []
    amounts = re.findall(r"\$[\d,]+\.?\d*", clean[:800])
    if amounts:
        extras.append(f"amounts:{','.join(amounts[:3])}")

    dates = re.findall(r"\d{1,2}/\d{1,2}/\d{2,4}", clean[:800])
    if dates:
        extras.append(f"dates:{','.join(dates[:2])}")

    names = re.findall(r"[A-Z][a-z]+(?:[-\s][A-Z][a-z]+)+", clean[:400])
    if names:
        extras.append(f"names:{names[0]}")

    extra_str = f" | {'; '.join(extras)}" if extras else ""
    return f"{head}{extra_str}"[:500]


# ---------------------------------------------------------------------------
# LLM classification + grouping (single call)
# ---------------------------------------------------------------------------

GROUP_PROMPT = """\
You are an expert document reviewer for HUD/Affordable Housing certification files.

You will receive a list of pages from a PDF, each with:
  - A short text snippet (head of the page + amounts/dates/names found)
  - Optional OCR quality flags (e.g. low_quality_scan, watermark)

Your job:
1. CLASSIFY each page into a canonical document type from the list below
2. GROUP consecutive pages that belong to the same logical document
3. Set the correct category (include / compliance / ignore)

CANONICAL DOCUMENT TYPES (use these exact names):

  INCLUDE — data-extracted forms:
    - HUD 50059                              (HUD Owner's Certification of Compliance)
    - Tenant Income Certification (TIC)      (LIHTC TIC, state HFA TIC forms)
    - HUD 3560 Form                          (USDA RD 3560-8 Tenant Certification)
    - HUD Model Lease                        (HUD Section 8/202/236 lease — contains rent/effective date)
    - Application / Housing Questionnaire
    - Verification of Income (VOI)
    - Verification of Assets (VOA)
    - Work Number / Equifax Report
    - Paystub
    - SSA Benefit Letter
    - SSI Benefit Letter
    - SSDI Benefit Letter
    - Verification of Disability Benefits    (private LTD/STD insurer benefit letters)
    - Pension Statement
    - TANF Verification
    - Child Support Statement
    - Bank Statement
    - Life Insurance Policy
    - Asset Self-Certification
    - Student Status Certification
    - Zero Income Certification
    - Self-Employment Affidavit
    - Debit Card Asset Self-Certification
    - HomeBASE Verification
    - Unemployment Affidavit
    - Notice of Rent Change

  COMPLIANCE — required forms, not data-extracted:
    - HUD 9887
    - HUD 9887-A
    - HUD 92006
    - HUD Race and Ethnic Data Form
    - Citizenship Declaration
    - Acknowledgement of Receipt
    - Tenant Release and Consent Form
    - VAWA Lease Addendum
    - Lead-Based Paint Certification
    - EIV Summary Report

  IGNORE — not processed:
    - Income Calculation Worksheet           (INTERNAL staff calc sheet ONLY)
    - Certification Review                   (reviewer/auditor findings & correction reports)
    - Receipt / Purchase Documentation       (retail receipts, order summaries, billing receipts)
    - File Order Form
    - Blank Page
    - Blank Form
    - Correspondence
    - Fax Cover Sheet
    - Credit Screening Report
    - Unknown

CRITICAL CLASSIFICATION RULES:

- "Form RD 3560-8" / "USDA-Rural Housing Service Tenant Certification" / any
  form listing household members with SSNs, annual income calculation lines,
  and RHS decision fields = "HUD 3560 Form", INCLUDE.
  Do NOT label RD 3560-8 as "Income Calculation Worksheet" just because it
  contains a "Part IV - Income Calculations" heading.

- RD 3560-8 BACK PAGE: The second page of an RD 3560-8 continues Parts VII-X
  with sections like "PART VII - PRELIMINARY CALCULATIONS", "PART VIII
  DETERMINING GROSS TENANT CONTRIBUTION (GTC)", "PART IX - DETERMINING NET
  TENANT CONTRIBUTION (NTC)", "PART X - CERTIFICATION BY BORROWER",
  "Gross Note Rate Rent", "Note Rate Rent", "Utility Allowance" as line
  items (30.a / 30.b / 30.c). Classify as "HUD 3560 Form", INCLUDE — do NOT
  misclassify as "Income Calculation Worksheet" or "TIC" just because the
  Part I header is absent on the back page.

- "Income Calculation Worksheet" means a SEPARATE internal spreadsheet or
  calc tape used by property staff — NOT any cert form that contains an
  income calculation section.

- HUD Model Lease (Section 8/202/236) is a LEGAL contract that contains the
  effective date, contract rent, utility allowance, tenant rent, and HAP
  amount. Classify as "HUD Model Lease", INCLUDE — do NOT dump it into
  "Correspondence". Recognize by phrases like "Model Lease", "Section 202",
  "Housing Assistance Payments (HAP) Contract", "HUD-Approved Market Rent",
  or a numbered list of tenant/landlord obligations.

- "Correspondence" means letters, emails, notices — NOT any form containing
  legal or boilerplate language.

- A document titled/headed "CERTIFICATION REVIEW" listing review findings,
  corrections needed, or a review status (Approved/Rejected) is a REVIEWER'S
  REPORT about a cert, not a cert = "Certification Review", ignore. Never
  extract data from it.

- Retail receipts, online order summaries (Amazon, etc.), credit-card
  transaction printouts, and medical/pharmacy billing receipts =
  "Receipt / Purchase Documentation", ignore. They are expense evidence,
  NOT bank statements — do NOT classify them as "Bank Statement" just
  because they show dollar amounts or a card number.

- A DISABILITY BENEFIT letter from an insurance company (Unum, MetLife,
  Aetna, The Hartford, ...) stating a monthly LTD/STD benefit amount =
  "Verification of Disability Benefits", INCLUDE. Do NOT classify it as
  "Life Insurance Policy" — a life insurance policy is an ASSET; a
  disability benefit is INCOME. The insurer's name alone does not make a
  document a life insurance policy.

- "Sworn Statement of Anticipated Income and Assets" = "Application / Housing
  Questionnaire".

- "Alternate Certification" / "AR-SC" forms = "Tenant Income Certification (TIC)".

- Blank VOI (employer section empty) = "Blank Form", compliance.

- File checklists / cover sheets = "File Order Form", ignore.

- Lease amendment / rent change notice = "Notice of Rent Change", compliance.

- OCR quality flags are HINTS, not verdicts. A page flagged "low_quality_scan"
  or "watermark" may still be a real form — classify based on the snippet
  content. Only return "Blank Page" if the snippet contains no recognizable
  form content at all.

- If a page snippet is "[OCR failed — page has content but text could not be
  extracted]" AND has an "ocr_failed" flag, classify it as "OCR Failed",
  category "ignore", notes "OCR failed — manual review required". Keep it
  as its own single-page group; never merge it with surrounding groups.

- Multi-page forms: GROUP ALL pages of the same form into ONE group.

PREVIOUS CERTIFICATION DETECTION:
  Files often contain BOTH the current cert AND a previous one for comparison.
  Split them into separate groups:
  - Different effective dates on two TIC/50059/3560-8 pages → split them.
  - More recent date = current; older = "<Type> (Previous)", category "ignore".
  - If dates are unclear, FIRST occurrence in page order = current.

Return JSON in exactly this shape:
{"groups": [
  {"pages": [1,2,3], "document_type": "HUD Model Lease", "category": "include", "person_name": "Steven Moore", "notes": null},
  {"pages": [4], "document_type": "Tenant Income Certification (TIC)", "category": "include", "person_name": "Steven Moore", "notes": null},
  {"pages": [5,6], "document_type": "HUD 50059 (Previous)", "category": "ignore", "person_name": "Steven Moore", "notes": "Older effective date"}
]}
Return ONLY valid JSON."""


def _llm_classify_and_group(
    page_results: list[dict],
    settings: Settings,
) -> list[dict] | None:
    """Send snippets to LLM for classification + grouping.

    Returns list of group dicts, or None if LLM call fails.
    """
    lines = []
    for pr in page_results:
        if pr["skip"]:
            continue
        flag_names = _flag_names(pr["flags"])
        flag_str = f" [ocr: {','.join(flag_names)}]" if flag_names else ""
        lines.append(f"PAGE {pr['page']}{flag_str}: {pr['snippet']}")

    if not lines:
        return []

    user_prompt = (
        f"Classify and group these {len(lines)} pages from a HUD/affordable "
        f"housing PDF.\n\n"
        + "\n".join(lines)
    )

    # Output budget: classification output is actually small — each group
    # is ~50-80 tokens of JSON, and even a 100-page file with 20 groups
    # produces only ~1500 output tokens. Cap at 8000 to stay under the
    # Anthropic SDK's non-streaming timeout guardrail (which fires around
    # max_tokens > 8192 because it assumes 100 tok/s worst-case and refuses
    # requests estimated to take longer than 10 minutes).
    out_budget = max(4096, min(8000, 150 * len(lines)))

    logger.info(
        "LLM classify+group: sending %d page snippets (model=%s, max_tokens=%d)",
        len(lines), settings.llm_classify_model, out_budget,
    )

    try:
        result = call_llm_json(
            GROUP_PROMPT, user_prompt, settings,
            max_tokens=out_budget,
            model=settings.llm_classify_model,
        )
        return result.get("groups", [])
    except Exception as exc:
        logger.exception("LLM classify+group call failed")
        # Do NOT fall back to Unknown singletons: with zero classified
        # pages the pipeline produces a confident-looking garbage audit
        # and marks the case complete in Salesforce. Surface the failure
        # as retryable so the case is re-processed on a later cycle.
        raise ClassificationUnavailableError(
            f"Classification LLM call failed — {type(exc).__name__}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def classify_and_group(
    page_texts: list[dict],
    settings: Settings,
) -> tuple[ClassificationResult, list[DocumentGroup]]:
    """LLM-only classification and grouping.

    Returns:
        (ClassificationResult, list[DocumentGroup])
    """
    logger.info("Pre-filter + snippet build (%d pages)", len(page_texts))
    page_results = _prefilter_and_snippet(page_texts)

    text_map: dict[int, str] = {}
    for pr in page_results:
        text_map[pr["page"]] = sanitize_for_extraction(pr["text"])

    llm_groups = _llm_classify_and_group(page_results, settings)

    if llm_groups is not None:
        classification_pages, document_groups = _build_from_llm_groups(
            llm_groups, page_results, text_map,
        )
    else:
        # LLM failed — emit Unknown singletons so pipeline can still run.
        # No keyword fallback. Human review flagged explicitly.
        classification_pages, document_groups = _build_unknown_fallback(
            page_results, text_map,
        )

    # Post-group deterministic split: force-split cert groups that contain
    # both current and previous certs (different dates/income)
    document_groups, split_updates = _post_group_split(document_groups, text_map)
    for page_num, new_type, new_category in split_updates:
        for pc in classification_pages:
            if pc.page == page_num:
                pc.document_type = new_type
                pc.category = new_category
                pc.notes = "Split by post-group date/income check"

    classification = ClassificationResult(pages=classification_pages)

    logger.info(
        "Classification: %d include, %d compliance, %d ignore | %d groups",
        sum(1 for p in classification_pages if p.category == "include"),
        sum(1 for p in classification_pages if p.category == "compliance"),
        sum(1 for p in classification_pages if p.category == "ignore"),
        len(document_groups),
    )

    return classification, document_groups


def _build_from_llm_groups(
    llm_groups: list[dict],
    page_results: list[dict],
    text_map: dict[int, str],
) -> tuple[list[PageClassification], list[DocumentGroup]]:
    """Build classification + groups from LLM response."""
    pages_classified: dict[int, PageClassification] = {}
    groups: list[DocumentGroup] = []

    for g in llm_groups:
        page_nums = g.get("pages", [])
        doc_type = g.get("document_type", "Unknown")
        category = g.get("category", "ignore")
        person_name = g.get("person_name")
        notes = g.get("notes")

        if not page_nums:
            continue

        for pn in page_nums:
            pages_classified[pn] = PageClassification(
                page=pn,
                document_type=doc_type,
                category=category,
                person_name=person_name,
                confidence=0.90,
                notes=notes,
            )

        page_nums_sorted = sorted(page_nums)
        page_range = (
            str(page_nums_sorted[0]) if len(page_nums_sorted) == 1
            else f"{page_nums_sorted[0]}-{page_nums_sorted[-1]}"
        )

        combined_text = "\n\n".join(
            f"--- Page {p} ---\n{text_map.get(p, '')}" for p in page_nums_sorted
        )

        groups.append(DocumentGroup(
            document_type=doc_type,
            category=category,
            person_name=person_name,
            pages=page_nums_sorted,
            page_range=page_range,
            combined_text=combined_text,
            notes=notes,
        ))

    # Any page not covered by LLM groups (empty pages or gaps)
    covered = set(pages_classified.keys())
    for pr in page_results:
        pn = pr["page"]
        if pn in covered:
            continue
        pages_classified[pn] = PageClassification(
            page=pn,
            document_type="Blank Page" if pr["skip"] else "Unknown",
            category="ignore",
            confidence=0.95 if pr["skip"] else 0.30,
            notes="No text extracted" if pr["skip"] else "Not in LLM groups",
        )
        if not pr["skip"]:
            groups.append(DocumentGroup(
                document_type="Unknown",
                category="ignore",
                person_name=None,
                pages=[pn],
                page_range=str(pn),
                combined_text=f"--- Page {pn} ---\n{text_map.get(pn, '')}",
                notes="Not in LLM groups",
            ))

    return sorted(pages_classified.values(), key=lambda p: p.page), groups


def _build_unknown_fallback(
    page_results: list[dict],
    text_map: dict[int, str],
) -> tuple[list[PageClassification], list[DocumentGroup]]:
    """Fallback when LLM classification fails entirely.

    Emits one 'Unknown' singleton per non-empty page so the pipeline still
    produces output. Explicit human-review finding required.
    """
    pages: list[PageClassification] = []
    groups: list[DocumentGroup] = []

    for pr in sorted(page_results, key=lambda x: x["page"]):
        if pr["skip"]:
            pages.append(PageClassification(
                page=pr["page"],
                document_type="Blank Page",
                category="ignore",
                confidence=0.95,
                notes="No text extracted",
            ))
            continue

        pages.append(PageClassification(
            page=pr["page"],
            document_type="Unknown",
            category="ignore",
            confidence=0.0,
            notes="LLM classification unavailable — human review required",
        ))
        groups.append(DocumentGroup(
            document_type="Unknown",
            category="ignore",
            person_name=None,
            pages=[pr["page"]],
            page_range=str(pr["page"]),
            combined_text=f"--- Page {pr['page']} ---\n{text_map.get(pr['page'], '')}",
            notes="LLM classification unavailable",
        ))

    return pages, groups


# ---------------------------------------------------------------------------
# Post-group split (deterministic current vs previous cert detection)
# ---------------------------------------------------------------------------

def _post_group_split(
    groups: list[DocumentGroup],
    text_map: dict[int, str],
) -> tuple[list[DocumentGroup], list[tuple[int, str, str]]]:
    """Deterministic post-group split for previous-cert contamination.

    After LLM grouping, check each cert group (HUD 50059 / TIC / HUD 3560 Form):
    - Extract effective date and income total per page
    - If pages have different dates or incomes → split into current + previous
    """
    from app.services.validation import normalize_date, normalize_money

    _SPLITTABLE_TYPES = {
        "HUD 50059",
        "Tenant Income Certification (TIC)",
        "HUD 3560 Form",
    }
    updated: list[DocumentGroup] = []
    classification_updates: list[tuple[int, str, str]] = []

    for g in groups:
        if g.document_type not in _SPLITTABLE_TYPES or len(g.pages) < 2:
            updated.append(g)
            continue

        page_data: list[dict] = []
        for pn in g.pages:
            raw = text_map.get(pn, "")
            clean = re.sub(r"<[^>]+>", " ", raw)
            clean = re.sub(r"\\+[()]", "", clean)

            eff_date = None
            for pat in [
                r"[Ee]ffective\s*(?:[Dd]ate)?[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
                r"[Cc]ertification\s*[Dd]ate[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
                r"[Ee]ffective\s*[Dd]ate[:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})",
                # Table-linearized fallback: OCR flattens form tables
                # row-major, so labels and values separate ("12. Effective
                # Date 13. Anticipated Voucher Date 14. Next Recert Date
                # 08/01/2025 10/01/2025 ..."). The FIRST full date within
                # a short window after the label is the effective date,
                # because values appear in the same order as their labels.
                # The window must be able to cross other field NUMBERS
                # ("13.", "14.") — only a complete date pattern stops it.
                r"[Ee]ffective\s*[Dd]ate[\s\S]{0,80}?(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
            ]:
                m = re.search(pat, clean)
                if m:
                    eff_date = normalize_date(m.group(1))
                    break

            income_total = None
            # Amounts must look like real dollar figures (4+ digit chars or
            # cents) — a bare short number is more likely the NEXT field's
            # label number ("Total Annual Income 87. Low Income Limit")
            # than a household income, and a wrongly-captured value here
            # causes false previous-cert splits.
            for pat in [
                r"(?:86\.?\s*)?Total\s*(?:Annual\s*)?Income[:\s]*\$?\s*([\d,]{4,}(?:\.\d{2})?|\d+\.\d{2})",
                r"TOTAL\s*INCOME\s*\(E\)[:\s]*\$?\s*([\d,]{4,}(?:\.\d{2})?|\d+\.\d{2})",
                r"Total\s*Income[:\s]*\$\s*([\d,]+\.\d{2})",
                # Table-linearized fallback (see effective-date comment).
                # The amount must look like a real dollar figure (4+ digit
                # chars or cents) so an intervening field number ("87.")
                # can never be captured; fails safe to None otherwise.
                r"Total\s*Annual\s*Income[^0-9]{0,40}?([\d,]{4,}(?:\.\d{2})?|\d+\.\d{2})",
            ]:
                m = re.search(pat, clean, re.IGNORECASE)
                if m:
                    income_total = normalize_money(m.group(1))
                    break

            tenant_rent = None
            m = re.search(r"Tenant\s*Rent[:\s]*\$?\s*([\d,]+\.?\d*)", clean, re.IGNORECASE)
            if m:
                tenant_rent = normalize_money(m.group(1))

            page_data.append({
                "page": pn,
                "eff_date": eff_date,
                "income": income_total,
                "tenant_rent": tenant_rent,
            })

        dates = {d["eff_date"] for d in page_data if d["eff_date"]}
        incomes = {d["income"] for d in page_data if d["income"]}

        needs_split = len(dates) >= 2 or len(incomes) >= 2
        if not needs_split:
            updated.append(g)
            continue

        logger.info(
            "Post-group split: %s pages %s have different dates=%s or incomes=%s",
            g.document_type, g.pages, dates, incomes,
        )

        def _page_sort_key(pd: dict) -> tuple:
            return (pd["eff_date"] or "", -pd["page"])

        sorted_pages = sorted(page_data, key=_page_sort_key, reverse=True)

        current_date = sorted_pages[0]["eff_date"]
        current_income = sorted_pages[0]["income"]

        current_pages = []
        previous_pages = []
        for pd in sorted_pages:
            is_same = True
            if current_date and pd["eff_date"] and pd["eff_date"] != current_date:
                is_same = False
            if current_income and pd["income"] and pd["income"] != current_income:
                is_same = False
            if is_same or (not pd["eff_date"] and not pd["income"]):
                current_pages.append(pd["page"])
            else:
                previous_pages.append(pd["page"])

        current_pages.sort()
        previous_pages.sort()

        if not previous_pages:
            updated.append(g)
            continue

        current_text = "\n\n".join(
            f"--- Page {p} ---\n{text_map.get(p, '')}" for p in current_pages
        )
        current_range = (
            str(current_pages[0]) if len(current_pages) == 1
            else f"{current_pages[0]}-{current_pages[-1]}"
        )
        updated.append(DocumentGroup(
            document_type=g.document_type,
            category=g.category,
            person_name=g.person_name,
            pages=current_pages,
            page_range=current_range,
            combined_text=current_text,
            notes=f"Current cert (split from pages {g.page_range})",
        ))

        prev_type = f"{g.document_type} (Previous)"
        prev_text = "\n\n".join(
            f"--- Page {p} ---\n{text_map.get(p, '')}" for p in previous_pages
        )
        prev_range = (
            str(previous_pages[0]) if len(previous_pages) == 1
            else f"{previous_pages[0]}-{previous_pages[-1]}"
        )
        updated.append(DocumentGroup(
            document_type=prev_type,
            category="ignore",
            person_name=g.person_name,
            pages=previous_pages,
            page_range=prev_range,
            combined_text=prev_text,
            notes="Previous cert detected by date/income mismatch",
        ))

        for pn in previous_pages:
            classification_updates.append((pn, prev_type, "ignore"))

        logger.info(
            "Split %s: current pages %s, previous pages %s",
            g.document_type, current_pages, previous_pages,
        )

    return updated, classification_updates
