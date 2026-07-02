# IDP — Intelligent Document Processing for Affordable-Housing Certifications

IDP is a FastAPI service that audits affordable-housing tenant certifications
(LIHTC / HUD 50059 / TIC / RD 3560-8). It performs a second, independent
extraction of a certification packet and **reconciles it against the MuleSoft
data already in Salesforce**, then writes a per-case audit report back to the
Case record for a human analyst to review.

---

## 1. Why this exists

When a tenant certification packet (often 50–60+ scanned pages) is uploaded,
**MuleSoft** extracts structured data from it into Salesforce. MuleSoft misses
and mislabels things. IDP is a **peer extractor**: it independently extracts the
same packet with OCR + LLMs, runs document-level compliance checks, and then
diffs its result against MuleSoft's.

Neither side is treated as ground truth — they **supplement each other**:

| | MuleSoft | IDP |
|---|---|---|
| Strength | Often complete coverage | Correct member identity, document compliance |
| Weakness | Mislabels (e.g. funding-program names in the member field), duplicates | Can drop a source, OCR errors |

The audit's value is the **asymmetric findings** (one side has something the
other doesn't) plus the **compliance gates** (unsigned forms, missing Race/
Ethnic data form, etc.) — surfaced to an analyst with a confidence score.

---

## 2. High-level flow

```
                Salesforce (Case + MuleSoft Certification Review)
                                  │  webhook: case ready for audit
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  IDP service (FastAPI)                                         │
   │                                                                │
   │  1. Download PDF from Salesforce (ContentDocument)             │
   │  2. IDP extraction pipeline   (OCR → classify → extract)       │
   │  3. Pull MuleSoft data        (Salesforce SOQL)                │
   │  4. Reconcile IDP vs MuleSoft (comparator)                     │
   │  5. Format findings + confidence                               │
   │  6. Write back to Case.IDP_Testing_Results__c                  │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                  Analyst reviews findings in Salesforce / dashboard
```

Each webhook returns **202 Accepted** immediately and runs the heavy work in a
background task. State is persisted in a local SQLite **JobStore** so repeated
signals for the same case are de-duplicated and crashed jobs can be recovered.

---

## 3. The IDP extraction pipeline

Entry point: `app/services/pdf_service.py::process_pdf_full` →
`app/services/pipeline.py::run_extraction_pipeline`.

```
PDF
 └─ render pages (PyMuPDF @ 200 DPI)
     └─ preprocess images (OpenCV → 1280×1920)
         └─ OCR (external OCR service; Claude Vision fallback for low-quality pages)
             └─ page_texts ──► run_extraction_pipeline:
                 1. Classify + group pages           (single Haiku call, no keyword rules)
                 2. Route document groups by category (demographics / cert / income / asset)
                 3. LLM extraction (one call per schema):
                      • household demographics
                      • certification info (effective date, rent, totals, signatures…)
                      • income sources
                      • assets
                 4. Income calculations (annualize each source, Section 9)
                 5. Build document inventories (HUD forms, financial docs)
                 6. Questionnaire disclosures + name reconciliation
                 7. Compile compliance findings (signatures, required forms, special scenarios)
                 8. Multi-stage field-level scoring → extraction confidence
```

LLM calls go through `app/services/llm_service.py` (Anthropic Claude). Extraction
runs on Sonnet 4.6 (`claude-sonnet-4-6`); classification runs on Haiku 4.5
(`claude-haiku-4-5-20251001`) — a separate capacity pool, cheaper for a
constrained labeling task.

### How documents are filtered

Filtering is layered. Only the outermost layer looks at page count and title;
everything after that is content-driven.

| Layer | Where | Rule | What it protects against |
|---|---|---|---|
| **1. PDF selection** | `salesforce/client.py::get_pdf_for_case` | `FileExtension='pdf'` + title denylist (`certification review`, `review report`, `audit`, `findings`, `ai file audit`, `review notes`, `review comments`) + `min_pdf_pages` (default 4) | Picking a Certification Review PDF or a cover-sheet fragment as if it were the source packet |
| **2. Page pre-filter** | `two_pass_classifier.py::_prefilter_and_snippet` | Drop pages with **no text at all AND no OCR-failure flag**. Watermark / low-quality pages are kept and forwarded with their OCR flags. | Silently deleting a real form the OCR struggled on |
| **3. Per-page LLM classify** | `two_pass_classifier.py::_llm_classify_and_group` | Haiku 4.5 reads a ~450-char snippet per live page and assigns each page a canonical `document_type` **and** a category (`include`, `compliance`, `ignore`). Multi-page forms are grouped in the same call. | Everything downstream — this is where a paystub gets called a paystub |
| **4. Deterministic split** | same file, post-group pass | Cert forms with two different effective dates on different pages are force-split; older = `<Type> (Previous)`, category `ignore`. | Auditing against the previous cert instead of the current one |
| **5. Extractor routing** | `extractor.py`, `questionnaire_extractor.py` | Each extractor iterates the groups and picks by substring on `document_type` (e.g. `"application" in dt or "questionnaire" in dt`), skipping `category == "ignore"`. | Feeding a bank statement to the demographics extractor |

So the answer to "does it filter by page count and name" is: **only Layer 1
does** — and that's just a coarse gate to keep obvious junk out. Layers 2–5
are content-based. Places where classification accuracy could be improved
without redoing the pipeline:

- **The title denylist is substring-only.** A file called `Q2 Review Notes.pdf`
  is caught (`review notes`), but `Reviewed 2026-06-15.pdf` isn't.
- **Extractor routing uses substring matching on `document_type`.** If the
  classifier labels something `"Sworn Statement of Anticipated Income"` and
  the prompt example nudges it toward `"Application / Housing Questionnaire"`,
  a rename in the label list silently orphans the record.
- **Salesforce case metadata is not fed into the classifier.** `CertType__c`
  and `Funding_Program2__c` are known before classification runs, but the
  Haiku prompt doesn't see them, so it can't use them as priors to disambiguate
  a HUD 50059 from a TIC on a borderline page.
- **`min_pdf_pages` is coarse.** The floor has to stay low (packet fragments
  exist), so a short-but-real garbage packet still enters the pipeline.
- **No title↔content cross-check.** If a document is *titled*
  `"HUD 50059.pdf"` but its content is a paystub, nothing flags the mismatch.

### Self-healing retries

LLM extraction is non-deterministic, so each extractor re-asks for what the first
pass dropped. All retries share one driver, `_retry_if_incomplete` (gate → fill,
max 1, failures swallowed so first-pass data is never lost):

- **Certification info** — re-ask for critical fields that came back null.
- **Income amounts** — a source with a name but no dollar figure triggers a
  targeted re-ask for just those amounts.
- **Income source coverage** — a classified benefit document (SSA / SSI / SSDI /
  pension / child support) that produced no record of its type is re-extracted on
  its own (grounded in a real document; never invents income to fill a gap).
- **Assets** — when the record count is below the number of asset documents, the
  missed documents are re-extracted.

---

## 4. The reconciliation (comparator)

`app/services/audit/comparator.py::compare(extraction, sf_data)` diffs the IDP
extraction against MuleSoft and returns findings + a confidence score. It compares
four record families plus the IDP-internal findings:

- **Members** — matched on (DOB, SSN last-4) with a fuzzy name fallback.
- **Income** — matched on normalized source + member; a member-agnostic pass also
  matches by **source + annualized amount** so MuleSoft's unreliable member field
  (e.g. a funding-program label) doesn't break matching. Income-source synonyms
  (SSA/SSI/SSDI/SSP/TANF) are normalized with word-boundary matching.
- **Assets** — global best-first one-to-one assignment; balance-matching pairs
  rank above name-only pairs so multiple same-bank accounts don't mis-pair.
- **Certification scalars** — effective date, cert type, unit number
  (prefix-normalized), household income total, household size, and rent
  (reconciled the LIHTC way: gross = tenant + utility allowance).

### Confidence score

```
case_confidence = extraction_score × 0.6 + agreement_rate × 0.4
flag            = green (≥0.85) | yellow (≥0.65) | red
```

A **high-severity comparison disagreement** (e.g. a year-off effective date) caps
the flag at **yellow** — a single critical mismatch can't be averaged into a
false green. Zero-value/immaterial items are de-escalated to NOTES.

---

## 5. Output report format

Findings are rendered by `app/services/audit/formatter.py` into the
`Case.IDP_Testing_Results__c` text field, split by the *source* of the finding:

```
--- AI FILE AUDIT ---
Case: CAS574128
Processed: 2026-05-28
Confidence: 81% (YELLOW)
Findings: 5 from comparison, 1 from IDP analysis

=== MULESOFT COMPARISON ===          ← IDP vs MuleSoft diffs
[CRITICAL]
  - ...
[REVIEW]
  - ...
[NOTES]
  - ...

=== IDP ANALYSIS ===                  ← compliance / field-quality / notes
[CRITICAL]
  - Certification form (TIC/HUD 50059) is NOT signed — resubmission required ...

---
Stats: 4 agreements, 2 disagreements, 3 AI-only, 0 MuleSoft-only
Extraction score: 0.90, Agreement rate: 0.67
```

Severity → bucket: `high → [CRITICAL]`, `medium → [REVIEW]`, `low/info → [NOTES]`.

---

## 6. HTTP API

| Method & path | Purpose |
|---|---|
| `POST /webhook/audit-ready` | **Recommended.** MuleSoft is done → run the full chain (download → extract → compare → writeback). |
| `POST /webhook/pdf-attached` | Legacy 2-webhook flow: start extraction only. |
| `POST /webhook/mulesoft-done` | Legacy 2-webhook flow: run comparison once MuleSoft is done. |
| `GET  /audit/cases` | List audit jobs from the JobStore (filter by `state`, paginated). |
| `GET  /audit/cases/{case_id}` | Inspect one case: findings text + IDP extraction + MuleSoft data (3-panel dashboard view). |
| `POST /admin/audit-jobs/reset` | Clear JobStore rows so cases can be re-audited (`mode`: `done` / `by_ids` / `all`). |
| `GET  /health` | Liveness/readiness check. |

**Auth:** webhooks require a bearer token matching `IDP_WEBHOOK_AUTH_TOKEN`
(fail-closed in production; bypass only with `IDP_DEV_MODE=true` for local dev).

**Webhook payload** (audit-ready / pdf-attached):

```json
{
  "case_id": "500XXXXXXXXXXXX",
  "case_number": "CAS574128",
  "cert_type": "AR",                       // MI | AR | AR-SC | IR (others → 422)
  "funding_program": "LIHTC",              // LIHTC | HUD | USDA | ...
  "content_document_id": "069XXXXXXXXXXXX" // optional; else IDP scans the case
}
```

---

## 7. Integration modes (`IDP_AUDIT_MODE`)

- `webhook` (default) — Salesforce pushes signals. Lowest latency; needs
  Salesforce-side Apex triggers + named credentials.
- `poll` — IDP polls Salesforce on an interval for ready cases. No SF-side setup;
  higher latency / API load. Webhooks return 503 in this mode.
- `both` — useful during transition.

A **maintenance thread** runs in every mode: it prunes terminal JobStore rows
older than `IDP_AUDIT_RETENTION_DAYS` and a **watchdog** re-queues cases wedged
mid-flight after a crash/deploy (up to `IDP_AUDIT_WATCHDOG_MAX_RETRIES`).

### Job lifecycle states

`pending → extracting → extracted → comparing → done`
plus terminal failure states: `extraction_failed`, `comparison_failed`,
`mulesoft_timeout`. State is stored in SQLite at `output/audit_jobs.db`.

---

## 8. Project layout

```
app/
  main.py                     FastAPI app factory + lifespan (starts poller/maintenance)
  core/                       config (env settings), logging, exceptions, DI
  routers/                    health, pdf (manual upload), webhook (Salesforce signals)
  schemas/                    Pydantic models: extraction, scoring, pdf, context
  services/
    pdf_service.py            render → preprocess → OCR orchestration
    ocr_service.py            external OCR client + Vision fallback
    image_processing.py       OpenCV preprocessing
    two_pass_classifier.py    page classification + grouping
    extractor.py              LLM extraction per schema + self-healing retries
    llm_service.py            Anthropic Claude wrapper (retry/backoff)
    pipeline.py               full extraction pipeline orchestration
    income_calculator.py      annualize income by method (Section 9)
    field_scorer.py           multi-stage field confidence scoring
    cross_doc_validator.py    TIC-total / consistency checks
    name_reconciler.py        collapse name variants across records
    validation.py             normalize money/dates/SSN; clean extraction output
    signature_validator.py, cert_type_rules.py, special_scenarios.py,
    bug_detector.py, questionnaire_extractor.py, inventory_builder.py, ...
    audit/
      comparator.py           IDP-vs-MuleSoft diff + confidence
      formatter.py            render findings → IDP_Testing_Results__c text
      jobs.py                 run_audit / run_extraction / run_comparison
      job_store.py            SQLite job state (idempotency, retention)
      poller.py               poll mode + maintenance/watchdog threads
    salesforce/client.py      SOQL queries (cert review/members/income/assets), PDF download, writeback
    parsers/                  questionnaire + source-name normalization
output/                       rendered images, audit_jobs.db
requirements.txt
.env                          configuration (not committed)
```

---

## 9. Configuration

Settings load from environment variables / `.env` with the **`IDP_` prefix**
(`app/core/config.py`). Key ones:

```bash
# LLM (Anthropic Claude)
IDP_ANTHROPIC_API_KEY=sk-ant-...
IDP_LLM_MODEL=claude-sonnet-4-6                 # Sonnet 4 (claude-sonnet-4-20250514) was retired
IDP_LLM_CLASSIFY_MODEL=claude-haiku-4-5-20251001

# OCR service (external)
IDP_OCR_SERVICE_URL=https://...
IDP_OCR_FALLBACK_URL=https://...
IDP_OCR_VISION_THRESHOLD=0.5        # below this OCR score → Claude Vision

# Salesforce
IDP_SF_USERNAME=...
IDP_SF_PASSWORD=...
IDP_SF_TOKEN=...
IDP_SF_DOMAIN=us-hc.my

# Webhooks / mode
IDP_WEBHOOK_AUTH_TOKEN=...          # required in production
IDP_DEV_MODE=false                  # true bypasses webhook auth (local only)
IDP_AUDIT_MODE=webhook              # webhook | poll | both

# Job store / housekeeping
IDP_AUDIT_JOB_DB=output/audit_jobs.db
IDP_AUDIT_RETENTION_DAYS=7
IDP_MIN_PDF_PAGES=4                 # smaller PDFs rejected as non-source docs
```

See `app/core/config.py` for the full list (rendering DPI, OCR concurrency,
watchdog/poll intervals, etc.).

---

## 10. Running locally

```bash
# 1. Install dependencies (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure — create a .env file with the IDP_* values above
#   (for local dev without webhook auth: set IDP_DEV_MODE=true)

# 3. Run the API
uvicorn app.main:app --reload --port 8000

# 4. Smoke-test a webhook (dev mode)
curl -X POST http://localhost:8000/webhook/audit-ready \
  -H "Content-Type: application/json" \
  -d '{"case_id":"500...","case_number":"CAS000001","cert_type":"AR","funding_program":"LIHTC"}'

# 5. Inspect the result
curl http://localhost:8000/audit/cases/500...
```

Requires a reachable OCR service, valid Anthropic + Salesforce credentials. PDFs
under `IDP_MIN_PDF_PAGES` (default 4) are rejected as non-source documents.

---

## 11. Current issues and known limitations

Live-service state as of 2026-07. Sorted by blast radius, not urgency.

### Deployment / operations

- **`--reload` does not actually reload on the RunPod pod.** The uvicorn
  worker keeps the same PID across file edits, so pushed code changes require
  a manual `kill && nohup uvicorn ...` restart to take effect. Do not assume
  a commit is running just because it's on master.
- **No `.gitignore`.** `.env` (Anthropic + Salesforce + webhook credentials)
  and `.venv/` and `uvicorn.log` are protected only by discipline. Never
  `git add -A` / `git add .` — always name files. Adding a `.gitignore` is
  outstanding.
- **RunPod proxy URL is pod-lifetime scoped.** When the pod is recycled the
  proxy hostname changes, and the frontend `VITE_API_URL` / `NEXT_PUBLIC_API_URL`
  in Vercel must be updated by hand. A "CORS error" on the frontend is almost
  always this, not a real CORS misconfiguration.

### Extraction / classification

- **Filtering is coarse at the outer layer** (see §3 "How documents are
  filtered" for the details and improvement ideas).
- **Classifier prompt has no case-metadata priors.** Salesforce already knows
  the case's `CertType__c` and `Funding_Program2__c`; wiring them into the
  Haiku prompt would resolve most HUD-50059-vs-TIC / RD-3560-vs-worksheet
  borderline calls.
- **Extractor↔classifier coupling is by substring.** A rename of a
  `document_type` label in the classifier prompt silently orphans downstream
  extractors. There's no registry / enum enforcement.
- **LLM shape drift on multi-applicant forms.** The questionnaire extractor
  now tolerates a list-shaped response by merging (True wins over False wins
  over None), but the same drift can happen on any list-typed schema; the
  tolerance is not generalized.
- **Field-level HTML/markup leakage.** OCR of HTML-rendered PDFs can leave
  tag fragments (`</strong></td>`) inside string fields. `text_sanitizer`
  scrubs at extraction boundaries and drops records with no usable identity;
  the sanitizer is defense-in-depth, not a fix for the underlying OCR output.

### Reconciliation

- **MuleSoft field mislabeling is worked around, not eliminated.** The
  comparator has member-agnostic passes (source + amount) to survive
  MuleSoft putting a funding-program name in the member field. Genuine
  mismatches on those fields are noise-suppressed, not resolved.
- **Confidence is heuristic.** `0.6 × extraction_score + 0.4 × agreement_rate`
  with a high-severity cap. Thresholds (`0.85`, `0.65`) are hand-tuned to
  the shadow-mode sample, not learned.

### Salesforce integration

- **Session reuse can go stale on the 4th+ sequential SOQL call.** Surfaces as
  `requests.ConnectionError: RemoteDisconnected`. `_is_retryable_error` now
  classifies typed `requests` exceptions as transient so the watchdog
  re-queues them; the root-cause session refresh is not implemented.
- **Findings field has a 32k-char cap.** `IDP_Testing_Results__c` is a Long
  Text Area; anything longer is truncated with a `[TRUNCATED — see IDP logs
  for full findings]` marker. Not observed in practice, but the ceiling is
  fixed by the schema, not by us.
- **Writeback is single-field.** Every finding lands in one text blob; the
  UI does its own parsing of `[CRITICAL] / [REVIEW] / [NOTES]` prefixes.

### Data model

- **JobStore is SQLite, single-file, single-node.** ~2.2k rows, ~464 MB
  post-VACUUM. Fine for shadow mode, not intended for HA. If the pod dies
  mid-write the row can be left in an intermediate state — the watchdog
  handles this, but it's the reason the watchdog exists.
- **`extraction_result` blobs are large** (~212 KB/row avg). Retention
  sweep (`IDP_AUDIT_RETENTION_DAYS=7`) is what keeps the DB bounded.

---

## 12. Glossary

- **TIC** — Tenant Income Certification.
- **HUD 50059 / RD 3560-8** — agency certification forms.
- **Cert types** — `MI` (move-in), `AR` (annual recert), `AR-SC` (annual
  self-certification), `IR` (interim recert).
- **Gross rent (LIHTC)** — tenant-paid rent + utility allowance.
- **MuleSoft** — the upstream integration that extracts packet data into
  Salesforce; IDP audits its output.
