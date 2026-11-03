# IDP_backend — Project Overview

_Last updated: 2026-11-02_

An **audit engine** for affordable-housing certification packets (LIHTC
TIC, HUD 50059, RD 3560-8, HUD Section 8 MI/AR/AR-SC/IR) serving one
consumer, Cartograph. Cartograph notifies the engine when a case is ready;
the engine fetches the packet, reads it with OCR + Claude, extracts
household, income, asset and certification figures with page provenance,
compiles findings, answers the case's checklist, and delivers a signed
payload to Cartograph's ingest. Reviewers judge the findings there and their
verdicts come back to the engine. Nothing is inferred: unknown is `null`,
SSNs leave as last four digits, and anything the payload cannot carry is a
warning, not a silent drop.

---

## 1. How it works

### 1.1 Runtime shape

```
Cartograph (Heroku)                              IDP engine (RunPod, FastAPI :8001)
   POST /integration/case ─────────────────────►  verify HMAC → dedupe → store request → intake line
                                                  ┌─────────── uvicorn, one process ───────────────┐
                                                  │ intake pool (IDP_AUDIT_PIPELINE_CONCURRENCY=3) │
                                                  │   fetch → keep packet → OCR → extract → adapt  │
                                                  │ resume thread   re-runs cases interrupted by a │
                                                  │                 stop, from the stored request  │
                                                  │ maintenance     retention sweep + watchdog     │
                                                  └────────────────────────────────────────────────┘
   POST /webhooks/runpod_ocr_results ◄────────────  signed delivery
   POST /integration/import_result ───────────────►  stored on the job row
   POST /integration/findings_feedback ───────────►  stored per finding key
                                                  SQLite job store: IDP_AUDIT_JOB_DB (output/audit_jobs.db)
```

- **Job state machine**: `queued → extracting → extracted → done`, with
  terminal failures `extraction_failed` (audit errored, document
  unavailable, or worker died) and `comparison_failed` (delivery failed,
  rejected, or not configured). Owned rows carry a heartbeat — the intake
  touches waiting rows, the audit touches the row as it works — so the
  watchdog (`IDP_AUDIT_WATCHDOG_SECONDS`, 30 min) measures silence, not
  elapsed time.
- **Every failure is reported**, not only logged: a `status: failed` payload
  with an `error_code` (`document_unavailable`, `extraction_failed`,
  `engine_error`, `engine_restarted`) goes to Cartograph's ingest.
- **Models**: extraction and classification both default to
  `claude-sonnet-5` (`IDP_LLM_MODEL`, `IDP_LLM_CLASSIFY_MODEL`).
  Classification moved off Haiku on 2026-09-10 after identical bytes
  classified differently on two runs. Targeted image reads (signatures,
  race/ethnicity form, low-quality pages) use Claude vision.
- **OCR**: DeepSeek-OCR primary, GLM-OCR second tier, Claude vision for
  pages below the 0.5 quality threshold, flagged by the service, degenerate
  or repetitive, or whose ink density implies text OCR did not return.
- **Auth**: HMAC-SHA256 over `"{timestamp}.{raw_body}"`, hex digest, headers
  `X-Timestamp` / `X-Signature`, 300 s skew, one secret per direction, fail
  closed (401 when unset).

### 1.2 Document acquisition

1. The notification names the case's documents with **presigned URLs**,
   fetched as the first act of the audit so the links need only outlive the
   wait in the intake line.
2. Each attachment is typed by its first bytes: PDFs contribute their pages,
   images become one page each, anything else is left out with a warning
   that reaches the payload. The parts are merged into **one PDF** in the
   order sent, with per-attachment page spans so a figure can be traced to
   the request document it came from.
3. The merged packet is **kept under `output/pdfs`** (`IDP_PDF_RETENTION_DAYS`
   14, `IDP_PDF_RETENTION_MAX_MB` 1500), because every contested finding has
   needed the rendered page. The extraction JSON is kept under
   `output/results/`; the per-job work dir (page images, OCR text) is deleted.
4. **Resume on restart**: at start, every owned Cartograph row is claimed and
   run again in arrival order from the request as received
   (`cartograph_request` on the row), the kept packet standing in when the
   presigned URLs have expired. A row with no stored request is failed with
   that reason and Cartograph is told.
5. **Repeat guard**: a notification for a case delivered within
   `IDP_CARTOGRAPH_REPOST_WINDOW_SECONDS` (2 h) naming the same documents is
   answered `already_delivered` and not audited again.

### 1.3 Extraction pipeline (per job)

| Step | What happens |
|---|---|
| A | Render at 200 DPI, keep the colour render and the PDF text layer; preprocess to 1280×1920 in parallel |
| B | DeepSeek OCR (4 pages at once) → rotation probe re-renders sideways pages at 90°/270° → GLM tier for pages still unreadable → Claude vision for low-quality, flagged, degenerate or content-loss pages |
| 1–2 | One LLM call classifies every live page into a canonical document type and category and groups multi-page forms; cert forms with two effective dates are force-split; a packet holding only last year's form promotes it to form of record with a finding |
| 3 | Routed LLM extraction — demographics, certification info, income (paystubs + verification records), assets; previous cert read for interim deltas; signature vision check when the text says unsigned; race and ethnicity from the HUD-27061-H form image, unticked is `null` |
| 4 / 4b | Deterministic document inventories; questionnaire disclosures (LLM-first) and affirmative-response validation |
| 4f | Income calculations: source-of-truth routing, YTD cross-checks, stale-wage guard (`[historical]`, excluded from current income and from the score) |
| 5 | Findings with stable `finding_key`s: missing compliance documents, signature requirements, cross-document consistency, special scenarios, cert-type rules |
| 6 | Field-level scoring → `field_scores.overall_composite`; per-page OCR quality kept as `page_ocr` |

Transient failures (model unavailable, truncated response, rate limit,
network) are retried under `IDP_CARTOGRAPH_EXTRACT_ATTEMPTS` (2); a
permanent one is reported at once.

### 1.4 Delivery and the Cartograph feedback loop

The adapter (`cartograph/adapter.py`, schema `1.3`) builds the payload in
Cartograph's vocabulary: `cert_review`, `household_members` (engine-assigned
`ref`s), `income_records` with paystubs nested, `asset_records` with bank
statements and VOA nested, `findings`, and `checklist_matches` (one entry per
checklist row the packet can answer). Income and asset types map onto
Cartograph's picklists (`IDP_CARTOGRAPH_INCOME_TYPES` / `_ASSET_TYPES` /
`_TYPE_ALIASES`); rows matching the request's `existing_records_manifest`
carry Cartograph's ids back so the import updates in place. The delivery is
a signed POST; its response (`scan_id`) is stored as `cartograph_delivery`.

Two things come back: the **import result** (stored as
`cartograph_import_result`; a case delivered then rejected during import is
otherwise indistinguishable from one that imported cleanly) and the nightly
**findings feedback** — reviewer verdicts (`valid` / `invalid`) per
`finding_key` and the findings reviewers added by hand, stored in the
`finding_feedback` table. `scripts/score_calibration.py` reads it to show
precision per finding code against the delivered scores.

The contract lives in `deliverables/01_endpoints_and_auth.md` and
`deliverables/02_ingest_payload_contract.md`.

---

## 2. Core features

- **Bounded intake, resume on restart, repeat guard** — at most three cases
  audited at once (also the memory guard on the 16 GB pod); in-flight cases
  re-run from the stored request and kept packet after a stop; a re-sent
  notification for a delivered case costs nothing.
- **Multi-attachment packets** — PDFs and images merged by content type with
  per-attachment page spans; unreadable attachments reported, not fatal.
- **Rotation probe and layered OCR fallbacks** — sideways scans re-rendered
  and re-read (DeepSeek hallucinates tables on rotated pages rather than
  failing); second OCR tier, then Claude vision for low-score, flagged,
  degenerate, repetitive or content-lost pages; the better read is kept.
- **Vision-verified signatures** — an "unsigned" text verdict is settled by
  one image call; draft watermarks are their own finding. Where no page can
  be checked, the finding says "verify visually" instead of a false
  "0 signed of N".
- **Race and ethnicity from the household's own form** — read from the
  image, unticked is unknown, delivered in Cartograph's exact vocabulary.
- **Stale-wage guard** — EIV / Work Number wage history (job ended) is
  excluded from current income and the score, surfaced as its own finding.
- **Frequency-aware annualisation** — self-declared monthly benefits are
  annualised (×12/×26/…).
- **Self-cert scoring honesty** — AR-SC packets do not lose score for
  wage-verification fields self-certification never contains.
- **Stable finding keys** — same code + same subject = same key, so a
  re-audit updates in place and reviewer resolutions survive.
- **Manifest linking** — payload rows matched to Cartograph's existing
  records on identity evidence, never on an ambiguous name.
- **Failure recovery and OCR forensics** — transient model/API/network
  failures are retried, every terminal failure is reported to Cartograph
  with an error code, and every job stores page-level OCR quality, fallback
  provenance and suspected-content-loss flags.

---

## 3. Resolved issues (with case evidence)

1. **Classification LLM failure produced garbage audits** — total failure
   used to yield all-`Unknown` pages and a 7% "done" audit (CAS610991/993).
   Now raises `ClassificationUnavailableError`, always retryable; model API
   failures likewise never mark a case completed.
2. **OCR silent content loss** — pages with heavy ink but tiny text output
   are detected by ink-density ratio and routed to vision fallback.
3. **Signature false-CRITICAL epidemic** — "NOT signed" fired on 81% of
   reports with no correlation to reviewer rejections. Fixed via the vision
   check + honest unverifiability wording.
4. **Field-scorer dishonesty on self-certs** — AR-SC packets scored RED for
   absent wage docs that self-certification does not require; now N/A'd.
5. **Catastrophic regex backtracking froze the whole service** — an O(n³)
   employment-table regex held the GIL for 1h+; rewritten linear, 58KB
   poison input parses in 0.29s.
6. **Effective date off-by-one-year cluster** — the extractor read the TIC
   header's adjacent "Move-in Date" or a previous year's form; prompt
   hardened.
7. **Monthly benefit amounts delivered raw** — the annualisation helper
   existed as dead code; now wired in (CAS611132: $1,098/mo → $13,176).
8. **Stale EIV wage history counted as current income** — CAS612199
   annualised $84k from 2022–2023 quarters on a 2026 cert. 15-month recency
   guard marks such evidence `[historical]`.
9. **Unbounded concurrency killed the engine** — 34 notifications in one
   hour, 26 packets rendering at once, 16 GB cap reached, kernel OOM kill
   with all of them half done (2026-10-13). Now the bounded intake.
10. **Restart failed the in-flight batch** — fifteen cases of one morning's
    batch were failed by the watchdog two days late, and Cartograph's import
    took each failure notice for a result. Now the resume path.
11. **Slow packets mistaken for dead workers** — a 119-page sideways-scanned
    packet took 42 minutes legitimately and the watchdog failed it, which
    released the dedupe and let a re-notification audit it twice. Now the
    heartbeat.
12. **Repeat notifications cost a second audit** — during the 2026-10-29
    batch Cartograph re-sent 29 of 61 cases between seconds and 94 minutes
    after accepting their results. Now the repeat guard.
13. **An unhandled exception left a case `extracting` forever** — a PNG
    attachment on J-CCAC-06750 escaped the task; every re-notification was
    then ignored as a duplicate. The task wrapper now records and reports
    whatever the inner audit did not expect, and images are paged.
14. **Sideways scans read as invented tables** — a sideways HUD 50059
    rendered as 35KB of plausible content scoring 0.62. Now the rotation
    probe, run before the second OCR tier (21 min → 3 min on a 119-page
    packet).
15. **Repetitive OCR output shipped as read** — a benefit letter came back a
    third garbage (`I1I1I1…`) at composite 0.81 with no flag, and the head of
    household's income was delivered as $0.00. Repeated-pattern and
    service-flag gates now queue the page for vision; GLM reads, whose score
    and flags used to be discarded for a fabricated 0.6, now pass through.
16. **Mid-JSON truncation on asset-heavy cases** — 8192 max tokens was too
    small once thinking tokens shared the budget; now 16384.
17. **Untranslated cert types disabled the rules** — Cartograph's `annual`
    passed straight through and AR-SC exemptions never fired; now mapped to
    the engine's vocabulary, unknown values left to the documents.

### Measurement loop

Reviewer verdicts arrive nightly on `/integration/findings_feedback` and are
stored per finding key. `scripts/score_calibration.py` joins them with the
delivered scores (precision per finding code, score of cases with an invalid
verdict against those without); thresholds are set by hand from what it
shows once enough verdicts exist on the current rules.
`scripts/replay_corpus.py` re-judges every rule change against every stored
packet before it ships.

---

## 4. Known issues & open items

- **Divider pages read as OCR failures** — near-blank section separators
  ("EIV", "Student Verification") produce "N pages could not be read"
  findings. Proposed: ink-density divider recognition → ignore type.
- **No document refresh or reconciliation poll.** The engine uses only the
  URLs in the notification; the contract's documents and `audit_pending`
  flows are not called. A notification lost in transit relies on
  Cartograph's retry.
- **A resumed case loses attachment boundaries** when it runs from the kept
  packet, so its rent figures carry no source `job_document_id`.
- **Watchdog-failed cases need re-notification**; the engine does not re-run
  a case whose worker died inside a live process.
- **Single inbound secret** — the dual-secret rotation the contract asks for
  is not implemented in `verify()`. **`expense_records`** is not delivered.
- **Empty picklists collapse everything to `other`** until
  `IDP_CARTOGRAPH_INCOME_TYPES` / `_ASSET_TYPES` are configured.
- **Extractor↔classifier coupling is by label** (mirror renames in
  `doc_taxonomy.py`); **180° scans** are outside the rotation probe's scope.

---

## 5. Operations quick reference

- **Run / restart**: `scripts/engine.sh start | stop | restart | status`.
  Pushed code takes effect only after `restart`; `status` prints the
  checkout the running process started from. `/post_start.sh` calls `start`
  at pod boot. Env: `IDP_PORT` (8001), `IDP_STOP_GRACE` (30).
- **Logs**: `engine.log` in the repo root (uvicorn output plus supervisor
  notes). Pidfiles and the stop flag under `run/`.
- **Job store**: SQLite at `IDP_AUDIT_JOB_DB` (default `output/audit_jobs.db`),
  tables `audit_jobs` and `finding_feedback`. WAL mode, so ad-hoc reads do
  not block the worker.
- **Check a case**:
  ```bash
  sqlite3 output/audit_jobs.db "select case_id, state, confidence, error,
    datetime(completed_at,'unixepoch') from audit_jobs where case_id='J-XXXX-00000'"
  sqlite3 output/audit_jobs.db "select cartograph_delivery, cartograph_import_result
    from audit_jobs where case_id='J-XXXX-00000'"
  sqlite3 output/audit_jobs.db "select source, finding_key, verdict, verdict_reason, description
    from finding_feedback where case_id='J-XXXX-00000'"
  ```
- **Re-run a case**: have Cartograph notify again. A `done` case within the
  repost window with the same documents is ignored (`already_delivered`);
  one with a different document list, or outside the window, is audited. An
  owned row answers `already_in_progress` until it finishes or the watchdog
  fails it.
- **Diagnose a case**: `findings_text` and `extraction_result` on the row
  (groups, `page_ocr`, `income_calculations`, `field_scores`) → the kept
  extraction JSON under `output/results/` → the kept packet under
  `output/pdfs/` (`<case_ref>_<stamp>.pdf`). Contested findings are always
  verified against the actual page.
- **Local round trip**: `python3 mock_cartograph.py --callback` with
  `IDP_CARTOGRAPH_INGEST_URL=http://localhost:9000/webhooks/runpod_ocr_results`.
- **Tests / replay**: `.venv/bin/python -m pytest -q tests`;
  `python scripts/replay_corpus.py check -v`; `eval/run_eval.py` (see
  `eval/README.md`).
