# Schema Change Request

| | |
|---|---|
| Version | 1.1 |
| Date | August 2026 |
| Author | Maria Azevedo |
| Status | For review. Reconciled against the 2026-08-23 snapshot |

Everything the audit engine needs added to the Cartograph schema, with the feature each change enables.

Verified against the repository at the 2026-08-23 snapshot (model validations, strong-parameter lists, and 456 migrations), so nothing here duplicates a column that already exists. Items satisfied since v1.0 have been removed.

Organised in three tiers: **behaviour changes** (Section 1) that alter how the importer works and carry consequences if omitted; **new columns** (Section 2) that enable specific features; and **defects** (Section 3) found during review that apply regardless of this integration.

---

## 1. Behaviour changes

These are not column additions. Each changes how something works, and each has a consequence attached.

### 1.1 `source` column on the cert child tables

**Timing: not needed for the first import. Required before the second.**

| Table | Column | Type | Default |
|---|---|---|---|
| `cert_household_members` | `source` | string | `"manual"` |
| `cert_income_records` | `source` | string | `"manual"` |
| `cert_asset_records` | `source` | string | `"manual"` |
| `cert_expense_records` | `source` | string | `"manual"` |
| `cert_review_findings` | `source` | string | `"manual"` |

**Why.** Re-import clears extraction-owned child records and rewrites them. Without a marker distinguishing what the importer created from what an analyst typed by hand, the delete removes both. The failure appears only on the second import of a case, by which point reviewer work is already in place.

With the column, the delete becomes `where(source: "extraction")`. Existing rows default to `"manual"` and are never touched. The change is additive, with no behaviour change for anything already in the database.

Until this exists, the importer must treat a cert review that already has records as `409` rather than proceeding.

### 1.2 AR-SC in `CERT_TYPES`

`CertReview::CERT_TYPES` currently allows `initial`, `annual`, `interim`. The engine audits four certification types: MI, AR, **AR-SC**, and IR. Three map cleanly; AR-SC has no target.

Either add a fourth value, or agree that AR-SC maps to `annual` with the distinction carried in another field. The engine applies different rules to AR-SC, which was specific development work, so collapsing it silently would lose a real distinction.

### 1.3 Finding reference on correction uploads

`JobDocument.document_class` includes `corrections`, which tells the engine that a file *is* a correction but not *which finding* it answers.

A `cert_review_finding_id` (nullable, indexed) on `job_documents`, or an equivalent parameter on the client upload path, enables the targeted verification flow: the engine verifies one document against one finding rather than re-auditing the whole packet. A targeted verification costs roughly 2 to 5 cents and completes in seconds, against approximately 60 cents and several minutes for a full re-audit. It is also the mechanism behind auto-approving a resolved item.

### 1.4 `item_code` on `cert_review_findings`

**This is the single highest-priority item in this document.**

| Table | Column | Type | Notes |
|---|---|---|---|
| `cert_review_findings` | `item_code` | string, indexed | Nullable. Null for findings not tied to a checklist item |

`item_code` was added to `cert_checklist_templates` on 2026-08-18, and `cert_review_checklist_items` was dropped the same day. That leaves the template holding item definitions and the finding holding per-case results, with **nothing connecting them**: a finding has no way to reference the item it answers.

Until this column exists, the engine can determine that a checklist item passes or fails but has nowhere to record it, and the checklist cannot render pre-answered. `ProcessRunpodOcrScanJob`'s own TODO refers to pre-answering `CertReviewChecklistItem` rows, which no longer exist.

Also needed, and not a code change: the **`item_code` values for each program**. They are editable per community funding program, so the engine cannot hardcode them.

---

## 2. New columns

### 2.1 `cert_review_findings` (highest-value additions)

| Column | Type | Enables |
|---|---|---|
| `item_code` | string, indexed | See Section 1.4. Links a finding to the checklist item it answers |
| `finding_key` | string, indexed | Stable identity across re-imports, so reviewer resolutions survive and findings do not duplicate |
| `confidence` | decimal(4,3) | **Auto-approve gating.** A determination's confidence, alongside its nature, is what decides whether an item can clear without manual review |
| `resolution_type` | string | `presence_only` or `recalculation`. Tells Cartograph whether resolving this finding is a document check or triggers a recalculation, which is the other half of the auto-approve decision |
| `pages` | integer array or JSON | **Reviewer guidance.** Lets the UI link to the pages the finding came from, for example "see pages 12 to 14" |
| `detail` | text | Case-specific reviewer guidance, internal only, never client-visible |
| `source_document_id` | bigint, nullable | Which uploaded document the finding came from, for page links to resolve |

`finding_key`, `confidence`, and `resolution_type` block features. `pages` and `detail` are required for reviewer guidance.

**Constraint on confidence.** The score currently blends extraction quality with agreement against the MuleSoft extraction. When MuleSoft is retired, that second component loses its counterparty and the score requires rebuilding and recalibration. The column can be added now; the thresholds that gate auto-approval should be set once verified-case data is available to support them.

### 2.2 `cert_reviews` (previous-certification comparison)

| Column | Type | Enables |
|---|---|---|
| `prior_effective_date` | date | AR and IR comparison. The engine extracts the previous certification from the packet and currently has nowhere to put it |
| `prior_annual_income` | decimal(12,2) | Year-over-year income delta |
| `prior_tenant_rent` | decimal(12,2) | Rent change detection |
| `prior_gross_rent` | decimal(12,2) | |
| `prior_utility_allowance` | decimal(12,2) | |
| `prior_household_size` | integer | Household composition change |
| `extraction_id` | string | Provenance. Links a cert review back to the extraction that populated it |

Interim recertifications are defined by what changed since the last certification. Without these columns the comparison has no destination, and the reviewer sees the new figures with no baseline.

### 2.3 `cert_income_records` (finding evidence)

| Column | Type | Enables |
|---|---|---|
| `employment_status` | string | `Active` / `Terminated` / `On Leave`. Drives terminated-employment and stale-wage findings |
| `termination_date` | date | Same |

Without these, Cartograph can display *that* a finding fired but not the evidence behind it, which undercuts reviewer guidance.

### 2.4 `cert_asset_records` (completeness)

| Column | Type | Enables |
|---|---|---|
| `account_number` | string | Matching an asset to its statements; detecting duplicate disclosures |
| `interest_type` | string | Distinguishes actual from imputed asset income |

Lower priority than the preceding sections: useful detail rather than blocked features.

### 2.5 `cert_asset_bank_statements` (completeness)

| Column | Type | Enables |
|---|---|---|
| `account_number` | string | Matching statements to the right account when a household holds several |

---

## 3. Defects found during review

Independent of this integration, and each verified against the migration history.

### 3.1 Three permitted parameters have no backing column

Records are built by mass assignment, so including any of these raises `ActiveModel::UnknownAttributeError` and returns a 500.

- `cert_asset_record[self_declared_balance]`: no column. `self_declared_balance` is a seeded calculation-method code. The real columns are `self_declared_income` and `manual_balance`.
- `cert_expense_record[monthly_amount]`: no column.
- `cert_expense_record[verification_status]`: no column. Income and asset records have one, expenses do not.

### 3.2 The Salesforce mapper emits values the validators reject

`map_income_type` produces `zero_income`, `unemployment`, and `disability`; `map_asset_type` produces `cash` and `life_insurance`. None appear in `INCOME_TYPES` or `ASSET_TYPES`. Assets are created with `create!`, so those raise `RecordInvalid`. Note `unemployment` versus the valid `unemployment_benefits` in particular. The fallback path also slugifies arbitrary input, which can emit anything.

### 3.3 SSN is stored in plaintext

`CertHouseholdMember#ssn` is a plain text column with no `encrypts` declaration anywhere in the model layer. Until that changes, the audit engine sends `ssn_last4` only and never full digits. Its own store keeps SSNs masked at every egress point, and writing them into an unencrypted column would be a downgrade in handling.

Adding `encrypts :ssn` plus a backfill would allow full write-back later if it is ever needed.

### 3.4 Webhook signature verification fails open

The three existing webhook controllers proceed when their signing secret environment variable is unset: Stripe parses unsigned JSON, Resend logs a warning and continues, Auth0 skips the check. The new ingest endpoint should return `401` instead, and reject any request whose timestamp is more than 300 seconds old.

### 3.5 Paystub and VOI data have two homes

`cert_income_records` carries JSONB columns `sf_paystubs`, `sf_voi_records`, and `sf_zero_income_worksheets`, written by the Salesforce import via `update_columns` (which skips validation), alongside the real associated tables written by the UI. `CertIncomeCalculationService` reads the relational tables. The engine targets the relational tables; the JSONB columns appear to be a migration artifact and are candidates for retirement.

---

## 4. Summary by priority

| Priority | Item | Blocks |
|---|---|---|
| 1 | `item_code` column on `cert_review_findings` | Checklist pre-answering. Nothing currently links a determination to an item |
| 1 | `item_code` values per program *(export, not a code change)* | Same. Codes are editable per program, so they cannot be assumed |
| 2 | `source` column on five tables | Safe re-import; data loss without it |
| 3 | `finding_key`, `confidence`, `resolution_type` on findings | Auto-approve, resolution survival |
| 4 | `pages`, `detail`, `source_document_id` on findings | Reviewer guidance |
| 5 | AR-SC decision | One of four certification types |
| 6 | Finding reference on correction uploads | Targeted correction verification |
| 7 | Prior-certification columns on `cert_reviews` | AR and IR comparison |
| 8 | `employment_status`, `termination_date` | Finding evidence |
| 9 | Remaining completeness columns | Detail |
