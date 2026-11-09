# IDP — Intelligent Document Processing for Affordable-Housing Certifications

IDP is a FastAPI service that audits affordable-housing tenant certification
packets (LIHTC TIC, HUD 50059, RD 3560-8, HUD Section 8 MI/AR/AR-SC/IR) for
one consumer, **Cartograph**. Cartograph notifies the engine when a case is
ready; the engine fetches the packet, reads every page with OCR and Claude,
extracts the household, income, assets and certification figures, compiles
compliance findings, answers the case's checklist rows, and delivers the
result as a signed JSON payload to Cartograph's ingest endpoint. Reviewers
judge the findings in Cartograph and their verdicts come back to the engine.

---

## 1. Why this exists

A certification packet is often 50–60+ scanned pages: the certification
form, verifications of income and assets, paystubs, bank statements, the
questionnaire, the HUD race/ethnicity form, signatures. Cartograph holds the
case record and a checklist built from the community's template, but not
what the packet says. The engine is an independent reader: it extracts the
figures with their page provenance, scores how reliably each was read, flags
what is missing, unsigned, inconsistent or stale, and hands the structured
result to Cartograph so staff review a populated record instead of
transcribing the packet. Nothing is inferred: an unticked box is delivered
as `null`, never a guess; SSNs leave as last four digits only; anything the
payload cannot carry is reported as a warning rather than dropped.

---

## 2. End-to-end flow

```
   Cartograph                                 IDP engine (FastAPI, one process)
   ──────────                                 ────────────────────────────────
   POST /integration/case  ────────────────►  verify HMAC, dedupe, store request,
   (case_ref, presigned document URLs,        put the case in the intake line, answer 202
    cert_type, program, checklist, manifest)        │
                                                    ▼  at most IDP_AUDIT_PIPELINE_CONCURRENCY at a time
                                              1. fetch documents (presigned URLs) → one PDF
                                              2. keep the packet under output/pdfs
                                              3. render → preprocess → OCR (DeepSeek, GLM, Claude vision)
                                              4. extraction pipeline (classify → extract → findings → score)
                                              5. payload adapter (Cartograph vocabulary, finding keys,
                                                 checklist matches, manifest links)
   POST /webhooks/runpod_ocr_results ◄──────  6. signed POST to IDP_CARTOGRAPH_INGEST_URL
   POST /integration/import_result ────────►  stored on the job row
   POST /integration/findings_feedback ────►  reviewer verdicts stored per finding key
```

The notification returns **202 Accepted** without doing anything expensive;
the result reaches Cartograph minutes later as a signed POST, and the
outcome of its import arrives separately on the callback. Every failure on
the way (document unavailable, extraction error, delivery rejected, worker
died) is recorded on the job row **and** reported to Cartograph as a
`status: failed` payload with an `error_code`, so a case never stops silently.

### Job lifecycle

State is persisted in a SQLite job store (`IDP_AUDIT_JOB_DB`, default
`output/audit_jobs.db`, table `audit_jobs`).

| State | Meaning |
|---|---|
| `queued` | Accepted and handed to the intake, waiting for a worker |
| `extracting` | The audit is running (fetch, OCR, extraction) |
| `extracted` | Extraction stored, payload being built and delivered |
| `done` | Delivered to Cartograph; `confidence` and `findings_text` on the row |
| `extraction_failed` | The audit errored, a document could not be fetched, or the worker died (watchdog) |
| `comparison_failed` | Delivery failed, was rejected by the ingest, or delivery is not configured |

A row in `queued`/`extracting`/`extracted` is *owned*; a second notification
for the same case while owned is answered `already_in_progress`. The intake
keeps a heartbeat on waiting rows and the audit touches the row as it works,
so the watchdog measures time since the last sign of life, not time since
the case started. The legacy constants `pending`, `comparing` and
`mulesoft_timeout` remain only so old rows are still recognised and pruned.

Three background threads run inside the one uvicorn process
(`app/main.py` lifespan): the **intake** (`cartograph/intake.py`, a bounded
worker pool; cases beyond the bound wait in arrival order), the **resume**
thread (`cartograph/resume.py`: at start every owned Cartograph row is
claimed and run again from the stored request, the packet kept under
`output/pdfs` standing in when the presigned URLs have expired), and
**maintenance** (`cartograph/maintenance.py`: every
`IDP_AUDIT_MAINTENANCE_INTERVAL_SECONDS` a retention sweep deletes terminal
rows older than `IDP_AUDIT_RETENTION_DAYS`, an extraction sweep drops the
extraction blob from terminal rows older than
`IDP_EXTRACTION_RETENTION_DAYS`, an output sweep deletes result
JSON, orphaned work dirs and aged packets under `output/` older than
`IDP_OUTPUT_RETENTION_DAYS`, and a watchdog fails owned rows
whose heartbeat has not moved for `IDP_AUDIT_WATCHDOG_SECONDS`, telling
Cartograph to re-notify).

---

## 3. HTTP API

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /health` | none | `{"status": "ok", "version": ...}` |
| `POST /integration/case` | HMAC | Cartograph signals a case is ready. Returns 202. |
| `POST /integration/import_result` | HMAC | Cartograph reports how its import went. Returns 200. |
| `POST /integration/findings_feedback` | HMAC | Reviewer verdicts on delivered findings, plus findings added by hand. Returns 200. |

Interactive docs (`/docs`, `/redoc`, `/openapi.json`) are served only when
`IDP_DEBUG=true`. The wire contract is in
[`deliverables/01_endpoints_and_auth.md`](deliverables/01_endpoints_and_auth.md)
and [`deliverables/02_ingest_payload_contract.md`](deliverables/02_ingest_payload_contract.md);
this README does not duplicate them.

### Authentication

Both directions use HMAC-SHA256 over the raw body
(`app/services/cartograph/signing.py`, enforced by `app/core/auth.py`):

```
signed      = "{timestamp}.{raw_body}"
X-Timestamp = unix seconds
X-Signature = HMAC_SHA256(secret, signed).hexdigest()     # hex, no prefix
```

One engine host can serve several Cartograph environments (Cartograph runs
dev, staging and prod; its staging shares the prod engine). Each environment
has its own secret pair and ingest URL under `IDP_CARTOGRAPH_ENVIRONMENTS`:

```
IDP_CARTOGRAPH_ENVIRONMENTS={"prod": {"ingest_url": "https://.../webhooks/runpod_ocr_results",
                                      "ingest_secret": "...", "callback_secret": "..."},
                             "staging": {...}}
```

An inbound request is verified against each environment's `callback_secret`;
the one that verifies names the environment, and the result is signed with
that environment's `ingest_secret` and POSTed to its `ingest_url`. Job rows
are keyed per environment (`<name>:<case_ref>`, bare `case_ref` for the
default environment — `IDP_CARTOGRAPH_DEFAULT_ENVIRONMENT`, else the first
configured), so a staging run of a case never deduplicates against the prod
run of the same reference. The older single-environment settings
(`IDP_CARTOGRAPH_INGEST_URL`, `_INGEST_SECRET`, `_CALLBACK_SECRET`) still
work and define one environment named `default`. `/health` lists the
environments served (names and hosts, never secrets).

Fail closed: no configured environment, missing headers, a timestamp more
than 300 s from now, or a signature matching no environment all return
**401**. The signature covers the exact bytes sent.

### `POST /integration/case`

Body fields the engine reads (`AuditRequest` in `app/routers/integration.py`):
`case_ref` (required), `documents[]` (required; each a presigned `url` with
optional `filename`, `job_document_id`, `document_class`), `cert_type`,
`program`, `job_id`, `community_id`, `unit_number`, `effective_date`,
`requirements` / `checklist_rows`, `existing_records_manifest`,
`rent_extraction_contract`. Unknown top-level keys are logged, never
rejected. The raw request is stored on the job row (`cartograph_request`)
with the checklist rows read out of it. Cartograph's cert types (`initial`,
`annual`, `interim`, `ar_self_cert`) are translated to the engine's (`MI`,
`AR`, `IR`, `AR-SC`); an unrecognised value is ignored and the type is read
from the documents.

Responses (all HTTP 202):

| `status` | When |
|---|---|
| `accepted` | The case joined the intake line |
| `already_in_progress` | A row for `case_ref` is already owned (`state` included) |
| `already_delivered` | The repeat guard fired (`delivered_seconds_ago` included) |

**Repeat guard** (`repeat_of_delivered`): a notification for a case already
`done`, completed within `IDP_CARTOGRAPH_REPOST_WINDOW_SECONDS` (default
7200), naming the same documents as the request that was audited — compared
on `job_document_id`, `filename` and `document_class`, since the presigned
URL is minted afresh on every send — is not audited again. A different
document list, a failed case, or a delivery older than the window is a
deliberate re-run. `0` turns the guard off.

### The two callbacks

`/import_result` is stored verbatim on the job row
(`cartograph_import_result`, `cartograph_import_at`); a body carrying
`event_type: findings_feedback` is routed to the feedback handler instead.
`/findings_feedback` accepts one case (`case_ref`, `scan_id`, `verdicts[]`,
`manual_findings[]`) or Cartograph's nightly batch envelope (`cases[]`, with
`finding_verdicts` / `missed_findings` mapped onto the documented names);
rows land in the `finding_feedback` table keyed on (case, source, finding
key, description), a later verdict on the same key replacing the earlier one.

---

## 4. The extraction pipeline

Entry point: `app/services/pdf_service.py::process_pdf_full` →
`app/services/pipeline.py::run_extraction_pipeline`.

### 4.1 Pages to text (`pdf_service.py`)

1. **Render** each page with PyMuPDF at `IDP_RENDER_DPI` (200). The colour
   render is kept for vision reads; the PDF's own text layer is kept when it
   has one (and discarded when it is a scanner's own OCR).
2. **Preprocess** with OpenCV to 1280×1920 in parallel
   (`IDP_PREPROCESS_CONCURRENCY`).
3. **OCR tier 1** — DeepSeek-OCR at `IDP_OCR_SERVICE_URL`,
   `IDP_OCR_CONCURRENCY` pages at a time.
4. **Rotation probe** — pages whose content looks landscape and read badly
   are re-rendered at 90°/270°; the orientation that scores decisively best
   replaces the page image.
5. **OCR tier 2** — GLM-OCR at `IDP_OCR_FALLBACK_URL` for pages still
   unreadable after rotation; the better of the two reads is kept.
6. **Claude vision** — a page is re-read from its image when its composite
   score is below `IDP_OCR_VISION_THRESHOLD` (0.5), when the OCR service
   flags its own output, when the text is a decoder loop or mostly a
   repeated short pattern (a barcode read as characters), or when ink
   density implies far more text than OCR returned.

Per-page quality (score, flags, fallback provenance) is carried into the
extraction result as `page_ocr`.

### 4.2 Text to structured result (`pipeline.py`)

| Step | What happens |
|---|---|
| 1–2 | Classify and group pages in one LLM call (no keyword rules): every live page gets a canonical `document_type` and a category (`include` / `compliance` / `ignore`); multi-page forms are grouped; cert forms carrying two effective dates are force-split and the older marked `(Previous)`. A packet holding only the prior year's form promotes it to form of record with a finding. |
| 3 | Routed LLM extraction, one call per schema: household demographics, certification info, income (paystubs and verification records), assets. Each extractor sees only the document types it needs. The previous certification is read separately for interim deltas. |
| — | Signature vision check: when the text says the cert is unsigned, one image call on the signature page distinguishes wet-signed, blank and draft watermark. |
| — | Race and ethnicity per member, read from the HUD-27061-H form image (winning over the 50059's columns); unticked is `null`. |
| 4 / 4b | Deterministic document inventories; questionnaire disclosures (LLM-first) and affirmative-response validation. |
| 4f | Income calculations: source-of-truth routing (paystub average / VOI rate×frequency / fixed monthly ×12 / self-declared × frequency), YTD cross-checks, stale-wage guard (`[historical]`). |
| 5 | Findings: missing compliance documents, signature requirements, cross-document consistency (TIC totals vs source sums), special scenarios, cert-type rules. Each finding has a stable `finding_key` (code + subject) so a re-audit updates in place. |
| 6 | Field-level scoring → `field_scores.overall_composite`. |

LLM calls go through `app/services/llm_service.py` (Anthropic SDK, one
retry policy). Extraction runs on `IDP_LLM_MODEL` and classification on
`IDP_LLM_CLASSIFY_MODEL`, both `claude-sonnet-5` by default. A truncated or
non-JSON response is an `ExtractionUnavailableError`, which the job layer
retries (`IDP_CARTOGRAPH_EXTRACT_ATTEMPTS`) rather than shipping as a result.

### Extraction score

The score on a delivered case (`field_scores.overall_composite`, 0–1)
measures how reliably the figures were *read*, not how complete the packet
is. Each extracted field is scored by stage — populated, found on its own
pages, consistent across documents, passing a business rule, not disputed
by a finding — and the case is the weighted mean of its fields (the figures
a reviewer acts on weigh 3×), times a coverage factor that drops for each
income amount the audit says was missed. So that the score tracks accuracy:

- A record's own figures must agree: a self-declared amount may be the
  per-period share of the certified annual figure, never a decimal away from
  it; a rate × unit a period or a decimal off the declared figure is red on
  whichever side the certified total says is misread.
- A wage history the calculator marked `[historical]` is left out of the
  score entirely (`not_counted` on its payload confidence): it is reported
  for the reviewer, and its empty amounts are not gaps in the reading.
- A null that the document could not have filled is N/A, not red: a student
  or disability flag on a print with no such column, the balance fields of
  a verification request that was never returned, a source named after its
  income type because the household gave no payer.

The score is extraction quality alone; there is no agreement term, because
there is no second extractor to agree with on this path.

### How documents are filtered

Only the first layer looks at the file; everything after it is content-driven.

| Layer | Where | Rule |
|---|---|---|
| **1. Attachment paging** | `cartograph/documents.py::assemble_packet` | Each attachment is typed by its first bytes, never by filename. A PDF contributes its pages, an image (PNG/JPEG/GIF/TIFF/BMP/WebP) becomes one page, anything else is left out with a warning that reaches the payload. |
| **2. Page pre-filter** | `two_pass_classifier.py::_prefilter_and_snippet` | Drop only pages with no text at all. Watermark / low-quality pages are kept and forwarded with their OCR flags. |
| **3. Per-page LLM classify** | `two_pass_classifier.py` | One call reads a short snippet per live page and assigns a canonical `document_type` and category; multi-page forms grouped in the same call. |
| **4. Deterministic split** | same file, post-group pass | Cert forms with two different effective dates are force-split; older = `<Type> (Previous)`, category `ignore`. |
| **5. Extractor routing** | `extractor.py`, `questionnaire_extractor.py`, `doc_taxonomy.py` | Each extractor picks groups by document type via the taxonomy's routes, skipping `category == "ignore"`. |

### Second reads

LLM extraction is non-deterministic, so the demographics and certification
extractors re-ask for the critical fields the first pass left null
(`extractor.py::_retry_if_incomplete`: gate → fill, one retry, failures
swallowed so first-pass data is never lost). The income extractor can
re-read a page from its image when OCR dropped a value, and an asset
document that prints figures but produced no record gets a second read; a
record returned without any figure is not kept.

---

## 5. Delivery and the feedback loop

`app/services/cartograph/adapter.py::build_payload` turns the
`ExtractionResult` into the ingest payload (schema version `1.3`): envelope
and `target`, `cert_review`, `household_members` (addressed by
engine-assigned `ref`s), `income_records` with paystubs nested,
`asset_records` with bank statements and the VOA nested, `findings` with
stable keys, and `checklist_matches` (one entry per checklist row the packet
can answer; rows left out stay untouched on Cartograph's side).
`expense_records` is not sent because the engine does not extract expenses.

- **Vocabulary** — `income_type` / `asset_type` are mapped onto Cartograph's
  picklists (`IDP_CARTOGRAPH_INCOME_TYPES`, `IDP_CARTOGRAPH_ASSET_TYPES`,
  `IDP_CARTOGRAPH_TYPE_ALIASES`). An empty picklist collapses every value to
  `other` and reports it.
- **Manifest linking** (`manifest.py`) — rows matching records Cartograph
  already holds (`existing_records_manifest`) carry those ids back so the
  import updates in place, filling blanks only. A link needs identity
  evidence (DOB plus a recognisable name, or a close name alone).
- **Checklist** (`checklist.py`) — reads the rows from either request shape
  (`requirements[].community_defaults.checklist_items[]` or flat
  `checklist_rows[]`), matches each to the classified documents, and
  returns found/not-found with pages and a note.

The delivery (`client.py::post_extraction`) is a signed POST bounded by
`IDP_CARTOGRAPH_TIMEOUT_SECONDS`; the response (`scan_id`, status) is kept
on the row as `cartograph_delivery`. `scripts/score_calibration.py` joins
the reviewers' verdicts with the delivered scores.

---

## 6. Project layout

```
app/
  main.py                    app factory + lifespan (intake, resume thread, maintenance thread)
  core/                      config (IDP_* settings), auth (HMAC), logging, exceptions, DI
  routers/                   health.py (GET /health), integration.py (the three /integration endpoints)
  schemas/                   Pydantic models: extraction, scoring, context, health
  services/
    pdf_service.py           render → preprocess → OCR tiers → rotation probe → vision fallback
    ocr_service.py           DeepSeek / GLM OCR clients, Claude vision read
    image_processing.py      OpenCV preprocessing, ink-density content-loss check
    two_pass_classifier.py   page classification + grouping (one LLM call)
    doc_taxonomy.py          canonical document types, categories, extractor routes
    extractor.py             LLM extraction per schema + second reads
    llm_service.py           Anthropic client wrapper (retry, typed failures)
    pipeline.py              extraction pipeline orchestration
    race_ethnicity.py        HUD-27061-H / 50059 race and ethnicity read
    income_calculator.py     annualisation, paystub matching, stale-wage guard
    field_scorer.py          multi-stage field confidence scoring
    findings.py              finding construction, categories, stable keys
    cross_doc_validator.py, signature_validator.py, cert_type_rules.py, special_scenarios.py,
    completeness.py, identity.py, members.py, name_reconciler.py, validation.py, text_sanitizer.py,
    inventory_builder.py, questionnaire_extractor.py, hours_resolver.py, bug_detector.py, parsers/
    audit/job_store.py       SQLite job state (audit_jobs, finding_feedback)
    cartograph/
      intake.py              bounded worker pool with waiting-row heartbeat
      tasks.py               audit_case: fetch → keep packet → extract → adapt → deliver
      documents.py           presigned-URL fetch, attachment sniffing, packet assembly
      adapter.py             ExtractionResult → ingest payload
      checklist.py           checklist rows → matches
      manifest.py            link payload rows to Cartograph's existing records
      client.py              signed POST to the ingest; failure reports
      signing.py             HMAC sign / verify
      resume.py              re-run cases interrupted by a stop
      maintenance.py         retention + watchdog sweeps; is_retryable_error
scripts/                     engine.sh (supervisor), replay_corpus.py, score_calibration.py
eval/                        extraction-quality harness (gold/, run_eval.py, README.md)
tests/                       pytest rule tests + corpus_baseline.json
deliverables/                the Cartograph contract documents
mock_cartograph.py           local stand-in for Cartograph
output/                      audit_jobs.db, pdfs/ (kept packets), results/ (extraction JSON)
.env                         configuration (not committed)
```

---

## 7. Configuration

Settings load from environment variables / `.env` with the **`IDP_` prefix**
(`app/core/config.py`). Unknown `IDP_*` names are ignored, so a `.env` still
carrying retired settings does not stop the engine.

**Storage, logging, rendering, OCR**

| Setting | Default | Meaning |
|---|---|---|
| `IDP_OUTPUT_DIR` | `output` | Job DB, kept packets and results, per-job work dirs |
| `IDP_LOG_LEVEL` / `IDP_LOG_JSON` | `INFO` / `false` | Logging |
| `IDP_DEBUG` | `false` | Also enables `/docs` |
| `IDP_RENDER_DPI` | `200` | Page render resolution |
| `IDP_IMAGE_MAX_WIDTH` / `IDP_IMAGE_MAX_HEIGHT` | `1280` / `1920` | OCR input size |
| `IDP_PREPROCESS_CONCURRENCY` | `4` | Preprocess worker threads |
| `IDP_OCR_SERVICE_URL` | — | DeepSeek-OCR (tier 1) |
| `IDP_OCR_FALLBACK_URL` | — | GLM-OCR (tier 2); empty disables the tier |
| `IDP_OCR_PROMPT` / `IDP_OCR_DPI` / `IDP_OCR_RAW` / `IDP_OCR_RETRY` | `document` / `200` / `false` / `true` | Passed to the OCR service |
| `IDP_OCR_TIMEOUT` | `600` | Seconds per OCR request |
| `IDP_OCR_CONCURRENCY` | `4` | Pages OCR'd at once |
| `IDP_OCR_VISION_THRESHOLD` | `0.5` | Composite score below which a page is re-read by Claude vision |

**LLM**

| Setting | Default | Meaning |
|---|---|---|
| `IDP_ANTHROPIC_API_KEY` | — | Required |
| `IDP_LLM_MODEL` / `IDP_LLM_CLASSIFY_MODEL` | `claude-sonnet-5` | Extraction / page classification |
| `IDP_LLM_MAX_TOKENS` | `16384` | Response budget (thinking tokens share it) |
| `IDP_LLM_TEMPERATURE` | `0.0` | |
| `IDP_LLM_THINKING` | `disabled` | `disabled` or `adaptive`, extraction calls only |
| `IDP_FUNDING_PROGRAM` / `IDP_CERTIFICATION_TYPE_OVERRIDE` | empty | Pipeline-context defaults when the request carries none |

**Jobs, housekeeping, retries**

| Setting | Default | Meaning |
|---|---|---|
| `IDP_AUDIT_PIPELINE_CONCURRENCY` | `3` | Cases audited at once (also the memory guard on a 16 GB pod) |
| `IDP_AUDIT_JOB_DB` | `output/audit_jobs.db` | SQLite job store |
| `IDP_AUDIT_WATCHDOG_SECONDS` | `1800` | Heartbeat silence after which an owned row is failed |
| `IDP_AUDIT_RESUME_ON_START` / `IDP_AUDIT_RESUME_DELAY_SECONDS` | `true` / `10` | Re-run interrupted cases at start |
| `IDP_AUDIT_RETENTION_DAYS` | `7` | Terminal rows older than this are deleted; `0` disables |
| `IDP_AUDIT_MAINTENANCE_INTERVAL_SECONDS` | `3600` | Sweep interval |
| `IDP_PDF_RETENTION_DAYS` / `IDP_PDF_RETENTION_MAX_MB` | `14` / `1500` | Kept packets under `output/pdfs`; `0` days disables keeping |
| `IDP_OUTPUT_RETENTION_DAYS` | `30` | Extraction JSON under `output/results` and orphaned work dirs older than this are deleted hourly; `0` disables |
| `IDP_EXTRACTION_RETENTION_DAYS` | `0` | The extraction blob (about 0.5 MB a case) is cleared from terminal rows older than this, the row itself staying; `0` keeps it for the row's life, which the corpus regression needs |
| `IDP_CARTOGRAPH_EXTRACT_ATTEMPTS` / `IDP_CARTOGRAPH_RETRY_DELAY_SECONDS` | `2` / `30` | Transient extraction failures retried this many times in all |

**Cartograph**

| Setting | Default | Meaning |
|---|---|---|
| `IDP_CARTOGRAPH_ENVIRONMENTS` | `{}` | JSON object: per environment `ingest_url`, `ingest_secret`, `callback_secret` |
| `IDP_CARTOGRAPH_DEFAULT_ENVIRONMENT` | first configured | Environment whose rows are keyed by bare `case_ref` |
| `IDP_CARTOGRAPH_INGEST_URL` / `_INGEST_SECRET` / `_CALLBACK_SECRET` | — | Single-environment form; defines one environment named `default` when `IDP_CARTOGRAPH_ENVIRONMENTS` is unset |
| `IDP_CARTOGRAPH_INCOME_TYPES` / `IDP_CARTOGRAPH_ASSET_TYPES` | `[]` | Cartograph's picklists (JSON lists) |
| `IDP_CARTOGRAPH_TYPE_ALIASES` | `{}` | Normalised term → picklist value overrides |
| `IDP_CARTOGRAPH_TIMEOUT_SECONDS` | `30` | Outbound POST timeout |
| `IDP_CARTOGRAPH_REPOST_WINDOW_SECONDS` | `7200` | Repeat-notification guard; `0` disables |
| `IDP_REPORT_PREVIOUS_CERT_MISSING` | `false` | Whether a missing prior-year cert is a finding / checklist gap |

`scripts/engine.sh` reads two more from the environment: `IDP_PORT` (8001)
and `IDP_STOP_GRACE` (30 s given to uvicorn before it is killed).

---

## 8. Running

### On the pod

```bash
scripts/engine.sh start      # start the keep-alive loop and the engine
scripts/engine.sh status     # pids, /health, the checkout it started from
scripts/engine.sh restart    # pushed code takes effect only after this
scripts/engine.sh stop
```

The supervisor runs `.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port
$IDP_PORT` and starts it again whenever it exits (crash, OOM kill) until
`stop` is asked. Output appends to `engine.log`; pidfiles and the stop flag
live under `run/`. The pod's `/post_start.sh` calls `engine.sh start` at
boot, so a pod restart brings the engine back by itself; a case in flight
at a stop is audited again at the next start, so a restart never waits.

### Production profile

The defaults suit a development host that sees a few cases a day and keeps
every extraction for the corpus regression. At the production rate
Cartograph plans (200 cases on a normal day, 300 on a heavy one) a case
costs about 4 MB of packet, 0.5 MB of extraction blob, 0.5 MB of result
JSON and 15 KB of row bookkeeping, so the production host sets:

| Setting | Production value | Holds |
|---|---|---|
| `IDP_AUDIT_RETENTION_DAYS` | `30` | a month of rows, 150 MB |
| `IDP_EXTRACTION_RETENTION_DAYS` | `3` | three days of blobs for a case under question, 450 MB |
| `IDP_OUTPUT_RETENTION_DAYS` | `14` | two weeks of result JSON, 2 GB |
| `IDP_PDF_RETENTION_DAYS` / `IDP_PDF_RETENTION_MAX_MB` | `14` / `20000` | two weeks of packets, 17 GB; the cap wins when a run of heavy days exceeds it |

About 20 GB in all; a 100 GB volume leaves room for heavy weeks and for
raising the windows later. `/health` reports `storage.output_free_mb`,
`storage.job_db_free_mb` and `storage.job_db_mb`, so the host filling up
is visible from the same call that says the engine is up. The job store
and the output folder must both sit on the persistent volume, not the
container disk: a container disk is lost when the pod is re-created, and
with it every in-flight row and kept packet.

### Setting up a new host

Everything the engine needs that is not in this repository: the `.env`
file, the job store, the output folder, and the pod's boot hook. To stand
up a Dev or Prod engine on a fresh pod:

1. **Pod.** CPU only (rendering and extraction run on CPU and the Claude
   API; OCR is a separate service), 8 vCPU / 32 GB, a persistent volume
   of 100 GB mounted at `/workspace`. Python 3.10 or newer.
2. **Code.** `git clone` into `/workspace/IDP_backend`, then
   `python3.10 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
3. **Data folders on the volume.** `mkdir -p /workspace/data/output` and
   point `IDP_AUDIT_JOB_DB=/workspace/data/audit_jobs.db` and
   `IDP_OUTPUT_DIR=/workspace/data/output` at them in `.env`. A move from
   an existing host copies its `audit_jobs.db` there after `engine.sh
   stop` on the old host, so the rows and the repeat guard come along;
   the kept packets and result JSON can be copied or left to age out.
4. **`.env`.** Start from the existing host's file; never commit it.
   Change the Anthropic key if this host has its own, set
   `IDP_CARTOGRAPH_ENVIRONMENTS` to the environments this host serves and
   `IDP_CARTOGRAPH_DEFAULT_ENVIRONMENT` to the one whose rows are keyed by
   bare case_ref (the Prod host serves `prod` and `staging` with `prod`
   as default; the Dev host serves `dev`), drop the legacy
   `IDP_CARTOGRAPH_INGEST_URL` / `_INGEST_SECRET` / `_CALLBACK_SECRET`
   trio, and on the Prod host apply the production profile above.
5. **OCR access.** The OCR service is run by another party; ask them to
   allow the new host's address before the first case.
6. **Boot hook.** Add to the pod's `/post_start.sh`, before anything that
   may exit the script:
   ```bash
   if [[ -x /workspace/IDP_backend/scripts/engine.sh ]]; then
       bash /workspace/IDP_backend/scripts/engine.sh start >> /workspace/IDP_backend/engine.log 2>&1 || true
   fi
   ```
7. **Start and check.** `scripts/engine.sh start`, then `scripts/engine.sh
   status`: the health line must list every environment this host serves
   with `"verifies": true` and `"deliverable": true`, and a sensible
   `output_free_mb`. The boot log line `Cartograph environments: ...`
   says the same. Then have Cartograph send one case from each environment
   and confirm it reaches `done` in `/audit/cases` and arrives on their side.
8. **Old host.** Once the new one has taken a case end to end, `engine.sh
   stop` on the old host and remove its Cartograph secrets, so one
   environment is never served from two places.

### Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# create .env with the IDP_* values above (at least the API key, OCR URL
# and the three Cartograph values)
.venv/bin/uvicorn app.main:app --port 8001
```

Requires a reachable OCR service and a valid Anthropic key. To exercise the
round trip without Cartograph, run the stand-in in a second terminal with
`IDP_CARTOGRAPH_INGEST_URL=http://localhost:9000/webhooks/runpod_ocr_results`
set for the engine:

```bash
python3 mock_cartograph.py --callback   # verifies the signature, prints the payload,
                                        # answers 202, posts the import_result back
```

Then send a notification signed with the inbound secret over the exact
bytes sent, e.g. from a Python shell:

```python
import json, httpx
from app.core.dependencies import get_settings
from app.services.cartograph.signing import sign, TIMESTAMP_HEADER, SIGNATURE_HEADER
body = json.dumps({"case_ref": "TEST-0001", "documents": [{"url": "https://.../packet.pdf"}]}).encode()
ts, sig = sign(body, get_settings().cartograph_callback_secret)
httpx.post("http://localhost:8001/integration/case", content=body,
           headers={"Content-Type": "application/json", TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sig})
```

---

## 9. Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests            # rule tests, no model calls, seconds
```

Every reconciliation, calculation, normaliser, identity, scoring and payload
rule is pinned in `tests/` with one case where it must fire and one where it
must not; `tests/test_corpus_regression.py` replays the corpus against the
baseline when the job store is present.

- `scripts/replay_corpus.py update | check [-v]` — replays the deterministic
  post-model stages (merges, identity, income calculations, findings,
  scoring, payload) over every stored extraction and diffs against
  `tests/corpus_baseline.json`. OCR, classification and the model reads need
  a live run.
- `eval/run_eval.py` — scores a packet against gold values in `eval/gold/`;
  see [`eval/README.md`](eval/README.md).

---

## 10. Known limitations

### Extraction / classification

- **Attachment paging is the only file-level gate.** Anything that is a PDF
  or an image enters the pipeline; junk is sorted out by classification.
- **Extractor↔classifier coupling is by document-type label.** A rename in
  the classifier's label list must be mirrored in `doc_taxonomy.py` or the
  records are silently orphaned.
- **LLM shape drift on multi-applicant forms.** The questionnaire extractor
  tolerates a list-shaped response by merging (True wins over False wins
  over None); the tolerance is not generalised to other list-typed schemas.
- **Field-level HTML/markup leakage.** OCR of HTML-rendered PDFs can leave
  tag fragments inside string fields; `text_sanitizer` scrubs at extraction
  boundaries as defence in depth.
- **Rotation probe covers 90°/270° only.** Upside-down scans are out of scope.

### Integration

- **The engine fetches only the URLs in the notification.** It does not call
  Cartograph for fresh document links and does not poll for missed triggers;
  a notification lost in transit relies on Cartograph's retry.
- **A case resumed from the kept packet loses attachment boundaries**, so its
  rent figures go out without a source `job_document_id`.
- **A watchdog-failed case is not re-run by the engine**; Cartograph is told
  to re-notify. Resume covers process stops, not worker deaths.
- **One inbound secret per environment.** A caller is identified by which
  environment's callback secret verifies; the dual-secret rotation the
  contract asks for (old and new valid at once) is not implemented.
- **`expense_records` is never sent.**

- **JobStore is SQLite, single-file, single-process.** Fine for one pod, not
  HA. `extraction_result` blobs are large; the extraction and retention
  sweeps keep the file bounded, and SQLite does not return freed pages to
  the filesystem until a `VACUUM`, so the file's size is a high-water mark.

---

## 11. Glossary

- **Cartograph** — the consumer: holds the case, the checklist and the
  reviewers; sends notifications and receives the payload.
- **case_ref** — Cartograph's `Job#ref_number`; the engine's job id.
- **scan_id** — Cartograph's handle for one delivered extraction; echoed in
  the import result and in findings feedback.
- **finding_key** — `CODE:subject`, stable across re-audits of one packet.
- **TIC** — Tenant Income Certification. **HUD 50059 / RD 3560-8** — agency
  certification forms. **Gross rent (LIHTC)** — tenant rent + utility allowance.
- **Cert types** — `MI` (move-in), `AR` (annual recert), `AR-SC` (annual
  self-certification), `IR` (interim recert); Cartograph's `initial`,
  `annual`, `ar_self_cert`, `interim`.
