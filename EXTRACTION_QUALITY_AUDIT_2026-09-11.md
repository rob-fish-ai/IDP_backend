# Extraction Quality Audit — 2026-09-11

Scope: the extraction path only — OCR and page quality, classification and routing,
the four LLM extractors and their retries, post-processing, cross-document checks and
findings, and confidence scoring. Not covered: job store, Salesforce path, Cartograph
transport, security (see the 2026-09-08 report for those).

Method: six independent reviews, one per stage, each working from the code and the
stored runs of the three real Cartograph packets (J-PORT-05318, J-PORT-05319 /
J-PORT-05320 which are the same packet, J-PORT-05754). Every finding ranked here was
reproduced against the code or the stored data before inclusion. Items the reviews
raised that did not reproduce are omitted or marked.

Two corrections to statements made earlier on 2026-09-10:

- **The "fabricated SSN" on Arnold Lyons's income records was not fabricated.** Page 24
  (Oklahoma Child Support statement) prints `SSN: 441- 66- 8882`, which the exact-string
  search missed because of the spaces. It agrees with the TIC's masked `8882`. The outlier
  is the questionnaire's handwritten `441-60-8888`, which the demographics extractor chose
  for the household record. The defect is real but different: no authority ordering and no
  conflict check for identity fields.
- **05320 does not carry different asset documents from 05319.** Pages 1–32 are identical;
  six pages were simply labelled differently by the classifier on identical text.

---

## 1. What limits extraction quality today, in order of consequence

| # | Defect | Where | Verified how |
|---|---|---|---|
| 1 | Income and asset extraction are one call over every document of the category, with the certification form and questionnaire inside the same context; the income prompt explicitly tells the model to back-solve a child-support gap from the certification total. Records fuse across documents with no provenance. | `extractor.py` INCOME prompt ~L262; `_build_texts`; `pipeline.py` group routing | code; token sizes measured on the three rows (11–15k tokens per income call) |
| 2 | "Verified in source" is an unanchored substring match for any value over three characters. `953.00` verifies against a BNC reference number, `82.05` against `1,082.05`, `58.00` against the income limit `58,200`, `116.00` against `HUD-116`. | `field_scorer.py:357-410` | reproduced with `_value_in_source` |
| 3 | The TIC-total check and the income-consistency check key records by payer name alone. Three household members paid by SSA collapse into one bucket: 05318 reported a 53% mismatch when the true gap was 6%, and the finding disputed every income record. | `cross_doc_validator.py:664-700, :57` | reproduced on the 05318 row |
| 4 | One case-level dispute repaints every field of every record in its category at weight 0.40; the absent-record fallback stamps every certification field. On 05319 the correct, source-verified rent figures went yellow because no income record existed. Removing the finding stage alone moves 05318 from 0.780 to ~0.89 and 05319 to ~0.91. | `field_scorer.py:574-662`; `scoring.py:224-231` | replayed |
| 5 | Retry gates manufacture records. The asset gate assumes one record per asset document, so self-certification and second verification pages are re-read alone and added as restatements (05318 payload $12,835 vs true $6,377). The income coverage gate is keyed on the classifier label, so a retirement letter labelled "SSI" produced a $0.00 SSI record. | `extractor.py:1219-1266, 1126-1210` | log + DB on both 05318 runs |
| 6 | The self-declaration dedupe can delete real income: keyed on (source, member) ignoring program, tie-broken by latest date, and `"0.00"` counts as a value. A later-dated $0.00 SSI record deletes the verified $1,489.50 retirement record; retirement + SSDI from the same payer keeps only one. | `pipeline.py:1467-1497` | reproduced |
| 7 | Income calculations run before the income list is final (before questionnaire stubs, name reconciliation, and the dedupe above), so calculation rows and records disagree; 05318 shows 5 calc rows for 4 records. | `pipeline.py:389-473` vs `:499-587` | DB |
| 8 | Completeness absorbs the single earner's line into the household total: the 50059 prints `SS = Soc. Sec. 21,720`, the total is `21,723`, and the 0.1% tolerance treats them as one figure, so the very income record the extraction missed raises no finding. It also lists historical "Household Income at Move-in" as unaccounted. | `completeness.py:351, :62-65` | reproduced on 05319 |
| 9 | Classification flips on identical text because the taxonomy forces a nearest label: 6 of 32 pages relabelled between two runs of the same packet, and the model's own `notes` name the true document each time ("General authorization letter", "Medical expense worksheet", "Wage Match Agreement"). The classifier sees a 350-character head, largely letterhead. | `two_pass_classifier.py:111-133, 378-384` | DB, both Buck rows |
| 10 | Document-type literals are restated in seven modules and have drifted from the taxonomy: the signature validator looks up "Tenant Release and Consent" (label is "…Form") and "Acknowledgement of Receipt of HUD Forms" (label is "Acknowledgement of Receipt"), so 05319 reports a present form as missing; the scorer's income map lacks SSI/SSDI/pension/child-support letters, so correct values from them are demoted to "found elsewhere". | `signature_validator.py:66, :174`; `pipeline.py:1839`; `field_scorer.py:235-255` | DB (missingForms) + code |
| 11 | Identity fields have no authority ordering and no conflict check. The certification's masked last-four, the questionnaire's handwriting, and benefit letters are never compared; whichever the first pass chose ships, and it varies run to run (Arnold: last-four 8882 one run, 8888 the next). A DOB-discrepancy list is computed and discarded. | `validation.py:252-268`; `pipeline.py:1514-1583, :1903-1916` | code + DB |
| 12 | The income calculator has no unit or plausibility model: `rateOfPay` has no unit so an annual salary × hours × periods gave $48,360,000 on 05754 and scored green; `biweekly`, `hourly`, `yearly`, `twice a month` have no multiplier so those records produce no calculation; child support from a 13-month payment table is annualised as latest month × 12 ($976 vs $3,359.56). | `income_calculator.py:20-34, 103-105, 277-334, 481-490` | reproduced |
| 13 | Normalisers pass junk through or fabricate: `normalize_money("N/A")` returns `"N/A"`; `normalize_ssn("02/20/1959")` returns `***-**-1959` and a phone number becomes an SSN; two-digit years (`7/15/49`) and OCR forms (`071/15/1949`) become `None`; `2020-02-30` is accepted. | `validation.py:40-55, 138-203` | reproduced |
| 14 | Extraction calls run with Sonnet 5 adaptive thinking on (no `thinking` argument is passed) inside a shared 16,384-token budget; truncation and JSON failure are swallowed by `_llm_fallback`, which returns an empty category, so a failed extractor ships as "no income / no assets" marked done. | `llm_service.py:46-77`; `pipeline.py:1500-1511` | code + log (18:25 failure) |
| 15 | Absence is scored red regardless of whether the form carries the field (`disabled` on a TIC with no such column; interest on a checking statement). 7 of 10 stored `disabled` fields are red; they move runs across the 0.80 line. | `field_scorer.py:986-993` | DB |

---

## 2. The plan, in the order I would do it

### Step 0 — Measurement (prerequisite for everything below)

There is no `tests/` directory and no evaluation set. Every change so far has been judged
by reading one run. On identical bytes the engine produced 29 findings one time and 20
the next, and OCR itself read the rent cells in one run and dropped them in the next.

Build: `eval/packets/<case>/packet.pdf` + `gold.json` (the verified values already
recorded for 05754, 05318, 05319; `CAS570103` has a PDF on disk at
`/root/testpackets/packet.pdf` and a stored row), a runner that executes the pipeline
N=3 times per packet and reports per-field match, per-field spread across runs,
classification agreement per page, and the four headline numbers (household income,
asset total, member count, finding set). Run it before and after every change below.
Until it exists, treat every accuracy claim, including the ones in this report, as
anecdotal.

### Step 1 — Extract per document, with provenance, and reconcile in code (items 1, 5)

- One LLM call per document group for income and assets. The certification form and the
  questionnaire are extracted by their own calls into a **declared** bucket; the model
  never merges declared figures into source records.
- Every amount carries `page` and a verbatim `quote` (≤ 40 chars). A deterministic
  post-step drops any amount whose digit-normalised quote is not on the cited page.
- Reconciliation happens in code: declared amounts are matched to verified sources by
  member and payer; unmatched declared amounts become findings ("declared, not
  verified"); unmatched verified sources become findings ("verified, not declared").
- Coverage replaces the record-count gates: a document is covered when every dollar
  figure on it (≥ $1, digit-tolerant, ignoring rates and percentages) is accounted for
  by a record. Declaration documents (self-certifications, affidavits, TIC/50059 asset
  parts) never owe a record; a program line reading $0.00 never owes a record; retry
  prompts never assert the type the label implies.
- Remove the "back-solve the child support gap from the certification" instruction and
  the "output array length should equal document count" instruction from the prompts.

Cost on 05318-size packets: roughly 2–3× today's per-case API spend (~$0.40–0.60),
lower wall-clock because calls parallelise. This is the change that removes the
derivation channel rather than catching it afterwards.

### Step 2 — Parse the certification's own tables into per-member claims (items 3, 8)

The TIC and 50059 income/asset tables are the only per-member ground truth in the
packet. Today the per-member summary check has a regex that requires a name in the
first cell, while the forms print a member number there, so it never fires.

- One parser for income and asset rows keyed by `householdMemberNumber` and column.
- Key every cross-document comparison on record identity `(member, source, program)`,
  never on payer name or member name alone. Carry the record id on
  `IncomeCalculationResult`.
- Completeness treats each table row as one expected record; a row is never absorbed by
  a scalar total. Labelled comparatives and historicals ("at move-in", "prior",
  "adjusted", "limit") are excluded as a label class.

This fixes the 53% false mismatch, catches 05319's missed $21,720 line, and gives every
dispute a precise subject.

### Step 3 — Make "verified in source" mean it (items 2, 10)

- Whole-token anchoring for every numeric value (the regex already exists for ≤ 3
  chars); money fields accept only currency-shaped forms; a short number without a
  field-label keyword within ~60 characters is a weak match capped at 0.85.
- Three tiers once provenance exists: found in the record's own document → 1.0; found
  only on the certification → 0.85 "copied from certification"; elsewhere → ≤ 0.70.
- Derive the scorer's document map from the taxonomy's evidence classes instead of a
  literal list; drop `compliance` groups from the fallback pool (on 05319 it is 96k
  characters of EIV boilerplate against 41k of evidence).
- Vocabulary fields (`frequencyOfPay`, `employmentStatus`, `incomeType`) verify through
  a synonym normaliser or stay unverified; never "poor OCR".

### Step 4 — Attribute disputes to the fields they are about (items 4, 15)

- A dispute names its fields in `subject_ref["field"]`; the scorer lowers those fields
  only, falling back to the record, then the category, only when nothing is named.
- The absent-record fallback lands on `certification.householdIncome`, not every field.
- Case-level penalties are shared across candidate records, not stamped on each.
- Completeness sets the finding category from the table the amount sits in (income vs
  asset), not a constant.
- Form-aware requiredness: a field the source form does not carry is N/A, not red; a
  field the form carries but OCR lost keeps the 0.30 "verify" score.
- Register the disputing codes that are missing (`SIGNATURE_VERDICT_CONFLICTS_WITH_DATE`,
  `ASSET_SELF_DECLARED_VS_VERIFIED`, `CALC_WORKSHEET_AS_VOI`, `FIXED_INCOME_PAYSTUB`)
  and dedupe findings before scoring so one dispute is never counted twice.

### Step 5 — Fix the post-processing order and the dedupe rules (items 6, 7)

- Compute `income_calculations` last, as a pure function of the final record list.
- Self-declaration dedupe: key on `(member, source, program)`; a zero or amountless
  record never displaces a non-zero one; rank by evidence class (third-party letter >
  certification > questionnaire) before date; emit a finding when collapsing.
- Asset claims: a record from a self-certification, TIC, or questionnaire (no account
  number, no statement) is a claim about an asset, not an asset. Merge it into the
  verified record of the same owner and type family within tolerance (≤ 1% or one
  digit edit, the tolerance completeness already uses), writing it to
  `selfDeclaredAmount`, and raise the discrepancy finding when they differ. Never merge
  two records that each carry an account number; never merge across owners.

### Step 6 — Identity fields: authority and conflict detection (item 11)

- Resolve SSN, DOB, and relationship by source authority: certification form > identity
  document > application/questionnaire > benefit letter. Handwriting never overrides a
  printed certification value.
- One `MEMBER_IDENTITY_CONFLICT` check over every record carrying (name, SSN, DOB):
  roster, income records, paystubs, asset owners, identity documents. Compare masked-aware
  last-four and normalised DOB; emit with member and field; register as disputing; run
  before member merge; merge keeps the variant list rather than the first value.
- Remove `socialSecurityNumber` from the income and asset prompts. No consumer reads it
  (the adapter sends member last-four only) and it is a propagation surface.

### Step 7 — Classification: code-owned taxonomy and a disagreement signal (items 9, 10)

- The taxonomy lives in code: label → category, routing family, inventory, signature
  rule. Every module imports it; a startup assertion checks each literal set is a
  subset; the prompt's list is generated from it. This removes the seven drifted sets.
- The classifier returns `observed_title` + canonical label + `fit: exact | nearest |
  none`. Category and routing are derived in code, never taken from the model. `fit !=
  exact` routes to a reviewable bucket that still reaches inventory and signature checks
  instead of silently extracting.
- Add labels for the families that actually recur and currently have none: Third-Party
  Authorization/Release, Verification of Deposit, Expense/Allowance Declaration (medical
  expense worksheet), Background Screening Report, Court Order / Legal Document,
  "Compliance — Other Signed HUD Form" (Wage Match Agreement).
- Collapse SSA / SSI / SSDI letters into one issuer-based label; coverage expects a
  record from that issuer, not a program type.
- Snippets: strip lines that repeat on ≥ 3 pages (letterhead) before windowing; include
  headings plus the first 600 characters; carry amounts with their preceding five words.
- A free title-vote (first heading → label family) flags disagreement with the model's
  label; log the per-page disagreement rate as the stage's quality metric. A second
  classification call with a tie-break on disputed pages costs ~$0.04 per packet if the
  vote is not enough.
- `person_name` gets a rule (the member the document is about; null when none) and is
  canonicalised against the roster before any extractor sees it.

### Step 8 — Income calculator: units, frequencies, histories (item 12)

- Add `rateUnit` (hourly | weekly | bi-weekly | semi-monthly | monthly | annually)
  transcribed from the form's own label or checkbox; hours multiply only when hourly; an
  "hourly" rate over ~$300 is unit-unknown → null plus finding.
- Canonical frequency vocabulary with every spelling the forms use.
- `paymentHistory: [{date, amount}]` for any source whose document is a history (child
  support records, Work Number quarterlies, EIV); annualise by trailing-12-month sum or
  average × 12; never × 12 a single row when a history exists.
- A plausibility envelope per source type; outside it, fall back to the next method and
  raise a finding, never persist. Derive a YTD audit row from paystub `ytdGross`.

### Step 9 — Normalisers return None, not junk (item 13)

`normalize_money` returns `None` plus a note on failure; `normalize_ssn` accepts only
SSN-shaped input; a two-digit-year pivot and leading-zero tolerance for dates; calendar
validation; title-case preserves all-caps tokens ≤ 4 characters (LLC, TBK, OK) and
suffixes.

### Step 10 — LLM plumbing (item 14)

- Make thinking explicit per call and measure it on the harness: extraction is closer to
  transcription than reasoning, so `disabled` or low effort is the likely winner, but
  decide on data.
- Treat `stop_reason == "max_tokens"` and JSON failure as typed failures that mark the
  case retryable. An extractor failure must never become an empty category delivered
  as done (this is item 2 of the 09-08 report and is still open).
- Stream calls with large budgets; retry 5xx explicitly; reuse one client.

### Step 11 — OCR and vision policy

See section 4 (pending the OCR review's result).

### Step 12 — Findings hygiene

- Wording: "declared income has no extracted record" when the certification declares
  income; the zero-income finding only when the certification also shows zero.
- One owner per rule: the signature problem is reported twice; the 9887 page-count rule
  lives in two modules and fires on correctly split packages — assert required content
  (title, consent text, signature lines, one per adult) rather than page counts.
- All required-form tests go through taxonomy predicates; one required-forms table keyed
  by certification type (the signature validator demands Citizenship Declaration and
  Race/Ethnic forms on every packet; the cert-type rules require them only for MI).
- Migrate the remaining string emitters (`pipeline.py` steps 1–14, `signature_validator`,
  `name_reconciler`, `income_calculator`) to `make_finding`; register name-variant,
  DOB-discrepancy, and member-merge as disputing with a member subject.

---

## 3. Status of the 2026-09-08 report's extraction items

Fixed since: B1 (hours unit), B8 (brand-map substring), B12 (completeness), B13
(rent assistance asserted unread), B11 partially (source_normalizer), taxonomy label
validation (root cause 1, partially), `_RECORD_DOC_MAP` for assets (B23, partially).

Still open and relevant to extraction: B2 (extractor failure → empty audit delivered),
B3 (rent supplement overwrites without a shape guard or finding), B4/B5/B6 (income mode
by string equality; `hourly` frequency; rate without hours), B7 (date parser order),
B14 (GLM fallback tier stamps a fabricated 0.6 composite that blocks vision — see
section 4), B20 (disability/student null unreported on RD 3560), B21 (cert-summary
cross-check dead on real layouts), B22, B23 (income/household maps), B25 (orphan
paystubs inherit the first Work Number entry), B26, B28 (`NO_ASSET_CERT_MISSING`
recognises two labels), B29, B30, B31 (`HomeBASE Verification` routed nowhere), B32
(`incomeType="Employment"`), B34 (9887-A page count in two modules), B53 (truncation
discarded), B54 (vision call with zero images).

---

## 4. OCR and page quality

This stage turned out to be the largest single source of lost content, and the cheapest
to fix. All items below were reproduced on the stored rows.

Third correction to 2026-09-10: **"05319 has no third-party income verification" is not
established.** Its EIV Summary Report (pages 7–11) was never read. OCR returned
hallucination loops ("Mouth Line Thickness" ×700, "Data of Birth:" ×1,800; zlib ratio
0.013–0.024 against 0.50 for a real page) scored green at 0.79–0.86, and nothing re-read
them. The conclusion needs the PDF.

1. **Service reliability flags never queue vision; the local repeat detector sees only
   runs of ≤ 3 characters.** Loops like `I/we,` and `Data of birth:` pass. Lost: 05318
   p12 (questionnaire income section and signature block, 89% `I/we,`, green 0.83),
   05319 p13 (the bank verification of deposit, 2,896 `9` tokens, 0.568, above the
   vision threshold), 05319 p7–p11 (EIV). Fix: any service flag queues vision; replace
   the run regex with a compression-ratio test on sanitised text plus a
   whitespace-tolerant repeated-token fraction. `pdf_service.py:44-49, 441-503`. Effort S.
2. **Sideways scans on a portrait canvas are invisible to the rotation probe**, which
   tests the ink bounding box. Every recognised box on the EIV pages is a tall strip on
   the right margin. Fix: trigger the probe from the OCR's own line geometry. Effort S–M.
3. **Vision replacement compares against markup-inflated OCR length** (13–66% of raw
   chars on sparse pages), so pages queued for an unread region keep the unread text;
   the 12% area gate misses handwritten value blocks. Fix: compare on sanitised text;
   append for unread-region queues. `pdf_service.py:475, 559-566`. Effort S.
4. **The GLM fallback tier stamps a fabricated 0.6 composite with no flags**, bypassing
   every gate; none of the engine's sampling controls are used. `ocr_service.py:32-55`.
   Effort S.
5. **Run-to-run variance is the service's sampling plus best-of-3 retry**; `dpi` is sent
   to an endpoint that ignores it; the PDF text layer is never used. Fix: read the text
   layer when present; second OCR read on the self-hosted engine for include-category
   pages, vision on disagreement. Effort M.
6. **No notion of document value in second-read decisions.** A double OCR read is near
   free on the A100; value-gated vision would cost $0.2–1.1 per packet. Effort M.
7. **Vision sees the preprocessed grayscale canvas with no contract** for blanks,
   checkboxes, handwriting, or strikethrough; truncated responses are accepted. 05754's
   struck-through $40.55 came through this path. Effort S.
8. **Provenance flags are misread as quality downstream**: a vision-repaired page is
   flagged yellow and the scorer then scores "not found" on it at 0.30 "poor OCR";
   `low_quality_scan` pages stay skipped after repair. `pdf_service.py:571`,
   `field_scorer.py:338-341`, `pipeline.py:112`. Effort S.

In the plan of section 2 these belong ahead of Step 1: they are the smallest changes
with the largest yield, and they would have caught every high-value page the stored
rows silently lost.

---

## 5. Per-stage detail

The six reviews' full findings, condensed. "R" = reproduced by me; "C" = verified in
code by the reviewer; "P" = plausible, not reproduced.

### Classification and routing
1. Forced nearest-label choice is the flip mechanism; notes name the true document (R).
2. Program-specific benefit-letter labels couple to a coverage gate that manufactures records (R).
3. 350-char head snippet, letterhead-dominated; discriminating text sits past the window (R).
4. Label literals duplicated in seven modules and drifted; two verified false outcomes (R).
5. `person_name` has no rule and is asserted to every extractor as fact (C).
6. The ignore bucket is final; Correspondence/Unknown pages that carry facts are never re-read (C).
7. No determinism control or disagreement detection; 12.5% of groups flipped between runs (R).
8. Group construction has no contiguity or duplicate-page checks (C).

### Extraction prompts and retries
1. One call per category over all documents; prompt licenses derivation (C, sizes measured).
2. Asset gate and prompt manufacture restatement rows (R, log).
3. Income coverage gate keyed on label with a leading retry prompt (R).
4. Amount retry re-sends the whole corpus including the certification with "look hard" (C).
5. `rateOfPay` has no unit (R on 05754).
6. No rule for payment histories; single month × 12 (R).
7. Grounding and number-format guards exist only in the cert-info prompts (C).
8. Income and asset records do not need an SSN (C).
9. Adaptive thinking on by default in a shared 16k budget; truncation → empty category (C + log).
10. Questionnaire extractor gated on a substring, reads checkboxes from OCR text (C).

### Post-processing
1. Calculations computed before the income list is final (R).
2. Self-declaration dedupe deletes real income (R).
3. No unit/plausibility model in the calculator (R).
4. Asset dedupe key is exact-money; claims survive as assets (R).
5. No authority ordering for identity fields; DOB check dead (R).
6. TIC-total keyed by sourceName (R).
7. Normalisers alter or fabricate values (R).
8. Blanket list rewrites without provenance: orphan paystubs, AR-SC seed (C).
9. Rent supplement overrides silently; supersession pass only runs with multiple files (C).
10. Coverage retry keyed by type, accepts $0.00 (R).

### Cross-document checks and findings
1. TIC total buckets by sourceName; every income record repainted (R).
2. No per-member identity reconciliation; DOB check dead (R).
3. Disputes land on records they say nothing about (R).
4. Completeness absorbs the single earner's line; historicals listed as missing (R).
5. Per-member cert-summary regex dead on real layouts (R).
6. "Zero income worksheet required" when income is declared (R).
7. Signature reported twice (R); contradiction between reads (P).
8. Page-count rules fire on correctly split packages, duplicated across modules (C).
9. Missing documents by exact label; required-form tables disagree between modules (R).
10. Most emitters are still strings (C).

### Scoring
1. Case-level dispute repaints every field in its category (R).
2. Source verification is an unanchored substring match (R).
3. Verification pool is the whole category plus the certification, never the record's own document (C).
4. `_RECORD_DOC_MAP` drifted; fallback pool is mostly compliance paper (R).
5. Absence scored red regardless of whether the form carries the field (R).
6. Right-magnitude misreads and cert-copied values are green; paystub second signal unused (R on 05754).
7. The composite moves for the wrong reasons on all three rows (R).
8. Normalised picklist values get the "poor OCR" penalty (C).
9. Findings scored before dedupe; terminated-record rule reads a field never scored (C).
10. No per-field confidence reaches Cartograph today; record-level flag plus reasons would be honest (C).

## 6. Implementation status (2026-09-11, end of day)

Every step is implemented and committed. Commit numbering runs one ahead of this
document's step numbering from Step 1 on, because the OCR work (this document's Step 11)
was done first as commit "Step 1".

| Doc step | Commit | Measured after it (live replays of stored OCR) |
|---|---|---|
| 0 harness | 611da21 | baseline 05318 32/37, 05319 23/23, 05754 17/22 (stored rows) |
| 11 OCR gating | 2c3e41a | 05319 EIV pages read for the first time (22:49 live run) |
| 1 per-document extraction | 5e7cd07 | 05318 payload $12,835 → $6,377 |
| 2 identity-keyed checks | c4668e5 | false 53% TIC mismatch gone |
| 3 source verification tiers | c6172a2 | anchored numerics, vocabulary synonyms |
| 4 dispute attribution | 0d57b07 | 05319 rent fields no longer yellow for a missing income record |
| 5 calc order / dedupe | 42b956f | $0.00 SSI no longer deletes the retirement record |
| 6 identity resolver | 8a8f60f | Arnold's SSN stable at 8882 |
| 7 taxonomy / classifier | b7d8684 | 05318 36/38, 05319 23/23, 05754 20/21 |
| 8 income calculator | 97e112e | child support $976 → $4,203.06 (ledger sum); $48M salary case rejected; 05318 36/37, 05754 20/21, 05319 20/23 |
| 9 normalisers | 04b9adb | 05318 37/38, 05754 20/21, 05319 21/23 |
| 10 LLM plumbing | f21497d | thinking A/B: equal scores on 05754/05319, disabled 2–3× faster; default disabled |
| 12 findings hygiene | 78ff957 | 05754 20/21, 05319 21/23, 05318 37/37 |
| follow-ups | 9c8112d | total-row guard, household-level income, cross-owner asset match, form-page signature date, JSON escape repair, failed document read fails the case; 05318 37/37 ×2 |

Remaining harness misses are not code: 05754's handwritten $40.55 interest (needs the page
image), 05754's signature date 08/12/2026 printed on packet p14 inside the TIC group (the
gold says undated — PDF decides), and 05319's p13 VOD, which the 22:49 vision re-read now
carries as a completed form (savings $1,123.81, checking avg $185.12) against a gold that
predates that read.
