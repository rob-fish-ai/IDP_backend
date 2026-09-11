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
from app.services.doc_taxonomy import (
    canonical_label, category_of, family_of, known_labels, prompt_type_list,
)
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
    repeated = _repeated_lines([pt["text"] or "" for pt in page_texts])

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

        snippet = _make_snippet(text, repeated)
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


_SNIPPET_HEAD = 600
_SNIPPET_MAX = 900


def _repeated_lines(texts: list[str], min_pages: int = 3) -> set[str]:
    """Lines that recur on several pages of the packet — letterhead, footers,
    confidentiality notices. They spent ~100 of the old 350-character head
    on every page of one packet and pushed the discriminating text out."""
    counts: dict[str, int] = {}
    for t in texts:
        for line in {ln.strip() for ln in t.splitlines() if len(ln.strip()) >= 12}:
            counts[line] = counts.get(line, 0) + 1
    return {ln for ln, n in counts.items() if n >= min_pages}


def _make_snippet(raw: str, repeated: set[str] | None = None) -> str:
    """What the classifier sees of a page.

    Headings first (the page's own title is the strongest signal), then the
    first characters of the page with letterhead lines removed, then each
    dollar amount with the five words before it so "$0.00" carries its
    label, then dates. Names are not extracted separately: on a packet with
    letterhead the regex returned the management company on every page.
    """
    repeated = repeated or set()
    lines = [ln for ln in raw.splitlines() if ln.strip() and ln.strip() not in repeated]
    body = "\n".join(lines)
    clean = strip_html(body)
    headings = [re.sub(r"^#+\s*", "", ln).strip() for ln in lines if ln.lstrip().startswith("#")]
    head = re.sub(r"\s+", " ", clean[:_SNIPPET_HEAD]).strip()
    parts = []
    if headings:
        parts.append("title: " + " / ".join(dict.fromkeys(h for h in headings[:3] if h)))
    parts.append(head)
    extras = []
    amounts = []
    for m in re.finditer(r"\$\s?[\d,]+\.?\d*", clean[:1200]):
        before = clean[max(0, m.start() - 60):m.start()].split()[-5:]
        amounts.append(" ".join(before + [m.group(0)]).strip())
        if len(amounts) >= 3:
            break
    if amounts:
        extras.append("amounts: " + " | ".join(amounts))
    dates = re.findall(r"\d{1,2}/\d{1,2}/\d{2,4}", clean[:1200])
    if dates:
        extras.append(f"dates: {','.join(dates[:2])}")
    if extras:
        parts.append("; ".join(extras))
    return " || ".join(parts)[:_SNIPPET_MAX]


# ---------------------------------------------------------------------------
# LLM classification + grouping (single call)
# ---------------------------------------------------------------------------

_GROUP_PROMPT_TEMPLATE = """\
You are an expert document reviewer for HUD/Affordable Housing certification files.

You will receive a list of pages from a PDF, each with:
  - A short text snippet (head of the page + amounts/dates/names found)
  - Optional OCR quality flags (e.g. low_quality_scan, watermark)

Your job:
1. CLASSIFY each page into a canonical document type from the list below
2. GROUP consecutive pages that belong to the same logical document
3. Set the correct category (include / compliance / ignore)

{TYPE_LIST}

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

- THE 9887 CONSENT PACKAGE: the pages that introduce or explain the package
  are not the forms. A cover sheet titled "Document Package for
  Applicant's/Tenant's Consent to the Release Of Information" is
  "HUD 9887 Consent Package Cover"; a page headed "HUD-9887/A Fact Sheet" is
  "HUD 9887/A Fact Sheet". Only "Notice and Consent for the Release of
  Information" and its continuation are HUD 9887, and only "Applicant's/
  Tenant's Consent to the Release of Information" and its continuation are
  HUD 9887-A. Labelling the cover or the fact sheet as a form makes the
  package's page counts wrong in both directions, and the audit checks those
  counts — a 9887 must be 4 pages and a 9887-A 2 pages per adult, so a
  miscount reports missing pages on a package that is complete.

- A "WAGE MATCH AGREEMENT" is its own form, not part of the 9887 family.
  If no canonical type fits it, follow the last-resort rule below rather than
  attaching it to a 9887.

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

- SELF-CERTIFICATION vs QUESTIONNAIRE — decide on structure, not on title.
  Both have a resident declaring income and signing under penalty of
  perjury, so the declaration alone does not tell them apart. What does is
  whether the OWNER also determines eligibility ON THE SAME FORM.

  A form is "Tenant Income Certification (TIC)" only when it carries BOTH:
    (a) the resident certifying household members and gross annual income, AND
    (b) an owner/management determination — income limit, maximum rent,
        effective date, owner or agent signature line.

  Examples with both: "Self-Certification of Household Annual Income"
  (OHCS), "NY AR Self Certification Form", "Owner's Eligibility
  Determination", "Annual Self Certification" (and its checklist cover
  page). On AR-SC files this form IS the certification and the source of
  truth — which is exactly why (b) is the test.

  A form with (a) but NOT (b) is the resident's own declaration. Return it
  with document_type EXACTLY "Application / Housing Questionnaire" — not the
  form's printed title. A form headed "Tenant Income Certification
  Questionnaire" (CA TCAC) is this case: the resident answers income and
  asset questions across several pages and signs, but nobody determines
  eligibility on it, and it feeds a separate certification. Its title
  containing the certification's name is not evidence; the absence of an
  owner determination section is.

  Use only document_type values from the list above, exactly as written.
  A label copied from a form's letterhead matches nothing downstream, so
  the document is read by no extractor and silently contributes nothing.

  Getting this wrong is costly in one direction specifically: a
  questionnaire filed as a certification puts the resident's signature and
  date where the certification's belong, and an unsigned certification then
  reads as signed.

- PHOTO IDs AND SOCIAL SECURITY CARDS: pages showing driver licenses,
  state IDs, passports, or Social Security cards = "Identity Document",
  INCLUDE. These carry the authoritative DOB and SSN for household members.

- Criminal history / sex offender affidavits, registry search results,
  credit reports, background screening authorizations = "Background
  Screening Report", ignore (screening paperwork, not certification data). Maintenance and inspection paperwork (apartment
  inspection checklists, work orders, unit condition statements) =
  "Maintenance / Inspection Form", ignore.

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

- One group is ONE form. Documents that sit next to each other in the file
  are not the same document, even when they share a type and a person. A
  submission cover sheet, the certification it introduces, and a
  questionnaire filed behind it are three forms and three groups. Each form
  has its own beginning — a title block, a form number, a "Page 1 of N" —
  and a new one starts a new group even when the previous form's type would
  also fit. Merging them makes one form's signatures and dates appear to
  belong to another.

- CONTINUATION PAGES: a page carrying only a header, a footer, a timestamp,
  a confidentiality notice or a page number — with no title block, no form
  number and no content of its own — is the continuation of the document
  that precedes it. Put it in that group. Do NOT classify it independently:
  judged alone it looks like nothing, so it becomes "Unknown", the group
  lands in "ignore", and the pages of a real document are dropped from
  extraction on the strength of a page that was never meant to stand alone.

- "Unknown" is a last resort. A page with identifying markers — an agency
  or employer name, a form number, benefit or wage amounts, a signature
  block — gets the closest canonical type with fit "nearest" and its own
  title in observed_title, so it is routed for review rather than lost.

PREVIOUS CERTIFICATION DETECTION:
  Files often contain BOTH the current cert AND a previous one for comparison.
  Split them into separate groups:
  - Different effective dates on two TIC/50059/3560-8 pages → split them.
  - More recent date = current; older = "<Type> (Previous)", category "ignore".
  - If dates are unclear, FIRST occurrence in page order = current.

PERSON NAME: person_name is the household member the document is ABOUT —
  the beneficiary of a benefit letter, the employee on a paystub or VOI, the
  account holder on a statement, the applicant who signed an affidavit — as
  the document prints it. null when the document concerns the household as
  a whole (certification form, lease, notices) or names nobody.

FIT: for every group say how well the canonical type fits:
  - "exact": the page is that type.
  - "nearest": no canonical type is this document; you chose the closest.
    Put the document's own title in observed_title. Use this instead of
    forcing a wrong-but-specific label — a nearest match is routed for
    review, not silently extracted as something it is not.
  - "none": nothing fits at all; document_type must be "Unknown", and
    observed_title carries what the page calls itself.
Never invent a type name: document_type is always one of the canonical
names above.

Return JSON in exactly this shape:
{"groups": [
  {"pages": [1,2,3], "document_type": "HUD Model Lease", "person_name": null, "observed_title": "Model Lease for Subsidized Programs", "fit": "exact", "notes": null},
  {"pages": [4], "document_type": "Tenant Income Certification (TIC)", "person_name": null, "observed_title": "Tenant Income Certification", "fit": "exact", "notes": null},
  {"pages": [5,6], "document_type": "HUD 50059 (Previous)", "person_name": null, "observed_title": "HUD 50059", "fit": "exact", "notes": "Older effective date"},
  {"pages": [7], "document_type": "Compliance — Other Signed HUD Form", "person_name": "Steven Moore", "observed_title": "Wage Match Agreement", "fit": "nearest", "notes": null}
]}
Return ONLY valid JSON."""

GROUP_PROMPT = _GROUP_PROMPT_TEMPLATE.replace("{TYPE_LIST}", prompt_type_list())


def _known_document_types() -> frozenset[str]:
    """The canonical type list, owned by doc_taxonomy and generated into the prompt."""
    return known_labels()


_reported_unknown_types: set[str] = set()


def _validated_type(document_type: str) -> tuple[str, str]:
    """(canonical label, fit) for a label the model returned.

    A recorded alias resolves to its canonical label ("SSI Benefit Letter"
    is an SSA Benefit Letter, "Screening Affidavit" is a Background
    Screening Report). A label outside the taxonomy becomes "Unknown" and
    is reported once: the model invented "Tenant Income Certification
    Questionnaire" on a real packet, and an invented label routes nowhere
    while looking like a classification.
    """
    label, fit = canonical_label(document_type)
    if fit == "none" and document_type and document_type not in _reported_unknown_types:
        _reported_unknown_types.add(document_type)
        logger.warning(
            "Classifier returned %r, which is not in the canonical type list; "
            "the pages are classified Unknown with the label kept in notes.",
            document_type,
        )
    return label, fit


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

    # Output budget: classification output is small — each group is ~50-80
    # tokens of JSON, and even a 100-page file with 20 groups produces only
    # ~1500 output tokens. The per-page factor carries headroom for Sonnet
    # 5's tokenizer, which produces roughly 30% more tokens for the same
    # text than earlier families. Cap at 8000 to stay under the Anthropic
    # SDK's non-streaming timeout guardrail (which fires around
    # max_tokens > 8192 because it assumes 100 tok/s worst-case and refuses
    # requests estimated to take longer than 10 minutes).
    out_budget = max(6000, min(8000, 200 * len(lines)))

    logger.info(
        "LLM classify+group: sending %d page snippets (model=%s, max_tokens=%d)",
        len(lines), settings.llm_classify_model, out_budget,
    )

    try:
        result = call_llm_json(
            GROUP_PROMPT, user_prompt, settings,
            max_tokens=out_budget,
            model=settings.llm_classify_model,
            # max_tokens covers thinking AND output. On a model that thinks
            # by default this budget is a JSON allowance, not a reasoning
            # one: moving classification to Sonnet 5 — where omitting the
            # parameter runs adaptive thinking, unlike the 4.6 family —
            # produced a response that spent all 4800 tokens thinking and
            # emitted no text block, failing every case with an
            # "Expecting value: line 1 column 1" from an empty string.
            #
            # Page labelling is the task type Anthropic's own migration
            # guidance puts in the thinking-disabled column, and the reason
            # for moving off Haiku was the model's judgement on ambiguous
            # pages rather than its capacity to deliberate about them.
            thinking={"type": "disabled"},
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

    disagreements = _title_vote_disagreements(classification_pages, text_map)
    if disagreements:
        logger.warning(
            "Classification: title vote disagrees on %d/%d page(s): %s",
            len(disagreements), len(classification_pages),
            "; ".join(f"p{pn} '{title[:40]}' looks {fam} but labelled {label}" for pn, title, fam, label in disagreements[:8]),
        )
        for pn, title, fam, label in disagreements:
            for pc in classification_pages:
                if pc.page == pn:
                    pc.notes = (pc.notes + "; " if pc.notes else "") + f"title '{title[:50]}' suggests {fam}"
    classification = ClassificationResult(pages=classification_pages)

    logger.info(
        "Classification: %d include, %d compliance, %d ignore | %d groups",
        sum(1 for p in classification_pages if p.category == "include"),
        sum(1 for p in classification_pages if p.category == "compliance"),
        sum(1 for p in classification_pages if p.category == "ignore"),
        len(document_groups),
    )

    return classification, document_groups


# Keyword votes from a page's own title, by evidence family. Deliberately
# coarse: this flags disagreement with the model's label for the log and
# the page notes; it never overrides the label.
_TITLE_VOTES = (
    (("tenant income certification", "50059", "3560-8", "owner's certification"), "cert"),
    (("wage match", "release of information", "authorization", "consent"), "compliance"),
    (("worksheet", "calculation"), "ignore"),
    (("registry", "screening", "credit report", "background"), "ignore"),
    (("verification of deposit", "bank statement", "account statement", "statement of account"), "asset"),
    (("benefit verification", "social security", "supplemental security", "pay stub", "paystub",
      "earnings statement", "verification of employment", "verification of income", "child support"), "income"),
    (("questionnaire", "application"), "declaration"),
)


def _title_family(text: str) -> tuple[str, str] | None:
    """(title, family) the page's own heading suggests, or None."""
    plain = strip_html(text or "")
    title = ""
    for ln in (text or "").splitlines():
        if ln.lstrip().startswith("#") and len(ln.strip()) > 3:
            title = re.sub(r"^#+\s*", "", ln).strip()
            break
    head = (title or plain[:120]).lower()
    for words, fam in _TITLE_VOTES:
        if any(w in head for w in words):
            return (title or plain[:80]), fam
    return None


def _title_vote_disagreements(pages: list, text_map: dict[int, str]) -> list[tuple[int, str, str, str]]:
    out = []
    for pc in pages:
        vote = _title_family(text_map.get(pc.page, ""))
        if not vote:
            continue
        title, fam = vote
        label_fam = family_of(pc.document_type)
        if fam == "ignore" and label_fam in ("ignore", "compliance"):
            continue
        if fam == "declaration" and label_fam in ("declaration", "household"):
            continue
        if fam == "income" and label_fam == "declaration":
            continue   # an affidavit about income is a declaration, not a disagreement
        if fam != label_fam:
            out.append((pc.page, title, fam, pc.document_type))
    return out


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
        raw_type = g.get("document_type", "Unknown") or "Unknown"
        doc_type, fit = _validated_type(raw_type)
        model_fit = (g.get("fit") or "").strip().lower()
        if model_fit in ("nearest", "none") and fit == "exact":
            fit = model_fit
        # The category is the taxonomy's, never the model's: a label routes
        # the same way on every run.
        category = category_of(doc_type)
        person_name = g.get("person_name")
        observed_title = (g.get("observed_title") or "").strip() or None
        notes = g.get("notes")
        if fit != "exact":
            what = observed_title or raw_type
            notes = f"{fit} match — page titled '{what}'" + (f"; {notes}" if notes else "")
        if not page_nums:
            continue
        for pn in page_nums:
            pages_classified[pn] = PageClassification(
                page=pn,
                document_type=doc_type,
                category=category,
                person_name=person_name,
                confidence=0.90 if fit == "exact" else 0.60,
                notes=notes,
                fit=fit,
                observed_title=observed_title,
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
