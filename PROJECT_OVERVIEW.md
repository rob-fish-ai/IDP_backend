# IDP_backend — Project Overview

_Last updated: 2026-07-24_

A shadow-mode **audit service** for affordable-housing certification packets
(LIHTC TIC, HUD 50059, RD 3560-8, HUD Section 8 AR/MI/IR). It independently
re-extracts every packet with OCR + Claude, reconciles the result against the
MuleSoft IDP data already in Salesforce, and writes an analyst-readable
findings report back to the Case. MuleSoft and this service are **peer
extractors**: neither is assumed correct; disagreements are surfaced for
human analysts with severity and materiality attached.

---

## 1. How it works

### 1.1 Runtime shape

```
Salesforce (Cases, files, Certification_Review__c)
        │  poll every 60s (IDP_AUDIT_MODE=poll)          FastAPI :8001
        ▼                                                    │
┌──────────────────────────── uvicorn worker ────────────────┴─────────┐
│  poller thread ──► job pipeline (extract → compare → write-back)     │
│  maintenance thread ──► watchdog / retention / revisit sweeps        │
│  HTTP: /audit/cases, /audit/cases/{id}, webhook, admin reset         │
└──────────────────────────────────────────────────────────────────────┘
        │
   SQLite JobStore  /var/data/audit_jobs.db
```

- **Job state machine**: `pending → extracting → extracted → comparing → done`,
  with terminal failure states `extraction_failed`, `comparison_failed`,
  `mulesoft_timeout`. Every transition is persisted; a watchdog re-queues or
  terminalizes wedged in-flight jobs, a retention sweep prunes old terminal
  rows, and a **revisit sweep** re-opens failed *and* done-but-unauditable
  cases when new attachments arrive (see §3.9).
- **Models**: extraction runs on Claude Sonnet 4.6 (`claude-sonnet-4-6`);
  page classification runs on Haiku 4.5. Targeted image checks (signatures,
  draft watermarks) use Claude vision.
- **OCR**: external DeepSeek-OCR primary, GLM-OCR fallback, Claude Vision
  fallback below a 0.5 quality score; an ink-density detector catches pages
  where OCR silently returned a fraction of the visible content.

### 1.2 Document acquisition (merge-all)

A case accumulates files over its review lifecycle (original packet,
supplements after rejections, reviewer reports). The service:

1. Queries all case PDFs newest-first.
2. Drops denylisted titles (certification reviews, audit reports, review
   notes) and any PDF whose embedded first-page text matches the denylist
   (reviewer report under a neutral title).
3. Clusters **multi-part packets** — titles differing by exactly one numeric
   fragment (`part 2`, `Pt2`, `.2`, `p2`, `2 of 3`, bare trailing digits) —
   and merges each set in part order.
4. **Merges every surviving file into one packet**, newest file first, up to
   a 150-page cap. Classification routes the pages afterwards, so
   supplements and packets coexist without a "pick one file" decision.
5. Records per-file page boundaries. If two *current* cert forms of the same
   type appear in **different source files**, the one appearing later in the
   merged page order (= from the older file) is demoted to
   `(Superseded)`/ignored — stale resubmissions never feed extraction.
6. The min-pages gate (default 4) applies to the merged whole.

### 1.3 Extraction pipeline (per job)

| Step | What happens |
|---|---|
| 1–2 | OCR all pages; two-pass classification (keyword pass, then one Haiku call over page snippets) into typed, person-scoped document groups; post-group splitting by effective date / windowed regex |
| 3 | Routed LLM extraction — demographics, certification info, income (paystubs + verification records), assets — each call sees only the doc types it needs |
| 3b | Signature vision check: if text-level `isSigned == "No"`, one vision call on the cert's signature page distinguishes wet-signed / blank / watermarked-draft |
| 3c | Income calculations: source-of-truth routing (paystub-avg / VOI rate×freq / fixed-monthly ×12 / self-declared×frequency), YTD audit cross-checks, **stale-wage guard** (EIV/Work Number history >15 months before the effective date is `[historical]`, excluded from current income) |
| 4 | Deterministic document inventories (financial + HUD) |
| 4b | Questionnaire disclosure extraction (LLM-first) and affirmative-response validation |
| 5 | Findings generation: missing compliance documents, signature requirements (Section 11), cross-document consistency (TIC totals vs source sums), special scenarios, per-field scoring with OCR-quality awareness |
| 6 | Persisted `ExtractionResult` incl. per-page OCR quality (`page_ocr`) for later diagnosis |

### 1.4 Comparison & confidence

When MuleSoft data is available (`Certification_Review__c` + member/income/
asset child records), the comparator reconciles:

- **Members** by (DOB, SSN-last4) keys with fuzzy-name fallback.
- **Income** keyed by benefit *program* for SSA-family income (payer vs
  program naming never causes false pairs), org-name normalization,
  placeholder-source matching, first+last member tokens. Amounts compare
  annual-to-annual; a mismatch at almost exactly ×12/24/26/52 (either
  direction) routes to REVIEW as a **basis mismatch**, and every income
  mismatch displays the AI row's `frequencyOfPay` and YTD.
- **Assets** in tiers (account number → name+type → balance stubs), sibling
  checking/savings duplicate collapse (keeping the MuleSoft-corroborated
  twin), and a **$100 materiality floor** — one-sided prepaid cards and
  near-empty accounts are NOTES, not CRITICALs.
- **Certification scalars** (effective date, cert type, unit, income total,
  rent — gross-to-gross with LIHTC fallback). Unit numbers match on
  punctuation-joined and last-token forms (`211-A` ≡ `211A`,
  `Bldg 2 Unit 27` ≡ `27`). An effective date off by exactly one year with
  the same month/day is flagged as probable move-in/previous-form confusion
  (REVIEW, not CRITICAL).

**Confidence** = 0.6 × extraction score + 0.4 × agreement rate;
flags: GREEN ≥ 0.85, YELLOW ≥ 0.65, RED below. The findings text is written
to `Case.IDP_Testing_Results__c` (32k cap guarded) and
`IDP_Audit_Complete__c` is set; a MuleSoft snapshot is stored with the job
so the analyst UI shows exactly the data the findings were computed against.

### 1.5 API surface

- `GET /audit/cases` — paginated lightweight rows (id, number, state,
  cert type, funding, confidence, timestamps).
- `GET /audit/cases/{case_id}` — full job: `findings_text`, complete
  `extraction_result` (14 sections incl. `page_ocr`, `income_calculations`),
  `mulesoft_data` (snapshot or live) + provenance flag.
- Webhook endpoints for push-mode operation; admin reset endpoint for
  re-queuing (`done` / failed / all scopes).

---

## 2. Core features

- **Convention-agnostic multi-part merging** — proven across 16 real
  naming conventions in production (`part N`, `Pt N`, `.N`, `p N`,
  `N of M`, bare digits, infix digits).
- **Merge-all acquisition** — supplements can no longer shadow the real
  packet; contested "newest file" heuristics eliminated.
- **Vision-verified signatures** — handwriting never survives OCR, so
  "unsigned" verdicts are settled by one image call; detects
  "This is Not a Final Document" draft watermarks as a distinct CRITICAL.
- **Honest unverifiability findings** — "signatures could not be verified
  from document text — verify visually" replaces false "0 signed of N"
  CRITICALs; compliance-form *absence* remains CRITICAL.
- **Materiality-aware severity** — $0/no-materiality income clusters,
  sub-$100 assets, IR-scoped noise, and basis mismatches are demoted so the
  CRITICAL list is worth an analyst's time.
- **Stale-wage guard** — EIV / Work Number wage *history* (job ended) is
  excluded from current income and surfaced as its own REVIEW finding.
- **Frequency-aware annualization** — self-declared monthly benefits are
  annualized (×12/×26/…); mismatch findings expose `frequencyOfPay` + YTD.
- **Self-cert scoring honesty** — AR-SC packets no longer bleed score for
  wage-verification fields that self-certification never contains.
- **Failure recovery** — retryable classification/API failures never
  mark a case done; revisit sweeps re-audit failed and
  done-with-missing-cert cases automatically when corrected packets arrive.
- **Per-page OCR forensics** — every job stores page-level OCR quality,
  fallback provenance, and suspected-content-loss flags for post-hoc review.

---

## 3. Resolved issues (with case evidence)

1. **Multi-part packets failed or half-audited** — single-file downloads
   missed parts (CAS610430 terminal failure → 89% after part grouping +
   merge). Later generalized to merge-all.
2. **Classification LLM failure produced garbage audits** — total failure
   used to yield all-`Unknown` pages and a 7% "done" audit
   (CAS610991/993 → 79%/73%). Now raises `ClassificationUnavailableError`,
   always retryable, with retry budgets; Anthropic API failures likewise
   never mark a case completed.
3. **OCR silent content loss** — pages with heavy ink but tiny text output
   are detected by ink-density ratio and routed to vision fallback
   (calibrated on real half-lost pages).
4. **Signature false-CRITICAL epidemic** — "NOT signed" fired on 81% of
   reports with zero correlation to reviewer rejections (lift 0.97).
   Fixed via the vision check + honest unverifiability wording; validated
   live on both a wet-signed page and a watermarked draft.
5. **Comparator false CRITICALs batch** — unit-number formats, zero-income
   records keyed by affiant name, duplicate checking/savings extraction
   twins (kept the MuleSoft-corroborated copy — CAS611474 0.46 with both
   false CRITICALs gone), placeholder sources, benefit-program keying
   (SSA family), terminated-employment N/A scoring, `(unnamed)` rendering.
6. **Field-scorer dishonesty on self-certs** — CAS611655-class AR-SC
   packets scored RED for absent wage docs that self-certification doesn't
   require; now N/A'd with reasons.
7. **Catastrophic regex backtracking froze the whole service** — an O(n³)
   employment-table regex in previous-cert parsing held the GIL for 1h+ on
   one case; the API (single process) stopped answering ("Failed to
   fetch"). Rewritten linear; 58KB poison input now parses in 0.29s.
8. **Supplement shadowed the packet** — newest-file selection audited a
   7-page questionnaire while the 50-page packet sat unread
   (CAS611336 31% → 77% on merged replay). Fixed by merge-all +
   superseded-cert demotion; 16 affected cases re-queued.
9. **Done-but-unauditable cases never recovered** — revisit sweep extended
   to `done` rows carrying the missing-cert headline; re-audits
   automatically when a new attachment lands.
10. **Effective date off-by-one-year cluster (8 cases)** — extractor read
    the TIC header's adjacent "Move-in Date" (verified against the rendered
    PDF, CAS611404) or a previous year's form. Prompt hardened; comparator
    demotes the exact-one-year pattern to REVIEW.
11. **Monthly-vs-annual income mismatches (7+ cases)** — monthly benefit
    amounts compared raw against MuleSoft's annual figures. The correct
    annualization helper existed as dead code; now wired in
    (CAS611132: $1,098/mo → $13,176 — matches MuleSoft to the cent), with a
    ratio-based comparator safety net for the inverse (over-annualized)
    cluster.
12. **Stale EIV wage history counted as current income** — CAS612199
    annualized $84k from quarters dated 2022–2023 on a 2026 cert (job had
    ended; MuleSoft carried only the SSA benefit). 15-month recency guard
    marks such evidence `[historical]`; the false 482% income-total CRITICAL
    disappears and the TIC total agrees exactly.
13. **Trivial-asset noise** — 11 cases carried CRITICAL/REVIEW findings for
    prepaid cards and balances of $0–$77; now NOTES under the materiality
    floor.

### Score validity work

A backtest against human reviewer verdicts established the score is
directionally meaningful but weakly calibrated: rejection ("miss") rates of
**43% for green / 58% for yellow / 70% for red** cases. Finding-type lift
analysis identified date mismatches as the strongest predictor (lift 1.28)
and unsigned-cert findings as pure noise (0.97) — which drove fixes #4 and
#10 above. Re-running the backtest on post-fix data is the standing
measurement loop.

---

## 4. Known issues & open items

- **Cross-unit misfiled packets** (CAS612207/CAS612195): two sibling cases
  hold each other's packet parts; unit 121's Part 1 (with its 50059) was
  never uploaded to either. Proposed: unit-scope filter at merge time
  (review's unit vs unit tokens in file titles) + a headline misfiled-
  attachment finding. *Awaiting approval; needs analyst action regardless.*
- **Divider pages read as OCR failures** — near-blank section separators
  ("EIV", "Student Verification") produce scary "11 pages could not be
  read" REVIEWs. Proposed: ink-density divider recognition → ignore type.
- **Bank-name aliasing** — "Jpmorgan Chase" vs "Chase Bank" produces a
  false missing/missed asset pair (CAS611607); needs org-alias
  normalization like the income side.
- **Small-denominator confidence collapse** — 1–2 disagreements among 3–6
  compared items crater the agreement rate (most of the 0.50–0.65 band);
  reweighting the confidence formula from measured finding-type lifts is
  the planned fix once post-fix data accumulates.
- **Cert-type-aware min pages** — 3-page AR-SC packets are rejected by the
  global min-pages gate (CAS610281).
- **CAS610911** — stuck `pending` since mid-July; needs investigation.
- **Reviewer-rejection mining** — Review_Line_Items / reviewer PDFs hold
  labeled rejection reasons (initialed-changes, rent-limit verification)
  that could seed new targeted checks.
- **Frontend** (separate repo): income table renders each paystub as its own
  row repeating the source's annual total (5 stubs read as $437k); stubs
  should group per source. `frequencyOfPay` / ANNUAL columns landed; YTD for
  real paystubs will populate now that `ytdGross` is extracted.
- **Parked by decision**: Salesforce session re-auth; auth/CORS pinning on
  the GET endpoints; `.gitignore` hygiene. Git remote still points at the
  old URL (redirects to `rob-fish-ai/IDP_backend`).

---

## 5. Operations quick reference

- **Run the worker** (user-managed, in a terminal):
  `uvicorn app.main:app --host 0.0.0.0 --port 8001` with
  `IDP_AUDIT_MODE=poll`. Restart after each deploy — `--reload` has not
  proven reliable, and one wedged process freezes both pipeline and API.
- **Job store**: SQLite at `/var/data/audit_jobs.db` (table `audit_jobs`).
  Confidence, findings text, extraction JSON, and the MuleSoft snapshot
  live on the row.
- **Re-queue a case**: flip `IDP_Audit_Complete__c` to false in Salesforce
  and reset the row to `pending` (`reset_to_pending` preserves identity,
  wipes prior results). For rows pruned by retention, the flag flip alone
  is enough — the poller re-discovers the case.
- **Diagnose a case**: findings text → `extraction_result` (groups,
  `page_ocr`, `income_calculations`) → MuleSoft snapshot → rendered PDF.
  Contested findings are always verified against the actual page images.
- **Key settings** (`app/core/config.py`): `min_pdf_pages` (4),
  `max_merged_pages` (150), poll interval/batch, OCR endpoints and
  fallback threshold, model ids.
