# Payload Field Map

| | |
|---|---|
| Version | 1.4 |
| Date | 23 September 2026 |
| Author | Maria Azevedo |
| Status | Generated from delivered payloads; v1.1 added `findings`, `pages`, `verification_status`; v1.2 added `confidence`; v1.3 adds the engine's annual figure and calculation, `rate_unit`, `payment_history`, relationship in Cartograph's vocabulary and `member_status`; v1.4 adds the Work Number header to `vois[]` (`overtime_rate`, `overtime_frequency`, `employment_start_date`), drops the empty VOI row, and gives the questionnaire disclosure findings a category and subject |

Every field the audit engine sends today, what each one means, and what it needs on the Cartograph side.

This replaces the ordering in *Schema Change Request v1.1*. Since v1.1 of this document the payload carries `findings` (section 8) and every income and asset record names the packet pages it was read from. What follows is what is arriving now.

Both delivered payloads are already in `raw_payload` and can be read alongside this document. This exists because the payload shows the shape and not the meaning, and several of the meanings decide compliance answers.

---

## 1. Six things that are not visible in the JSON

Read these before the tables. Each is a distinction the payload depends on and does not announce.

**`current_value` and `manual_balance` are not two amount columns.** `current_value` is a balance backed by a bank statement or a verification of assets. `manual_balance` is the resident's own figure with no third-party evidence. If they merge, a self-declaration satisfies a checklist item that exists to require verification. On both cases delivered so far, every asset arrived as `manual_balance`, none had supporting evidence.

**`null` is not `false`.** `is_disabled` and `full_time_student` are true, false, or null, and null means the packet never stated it. A default that turns null into false converts "unknown" into "not disabled", which is also one of the audit's own findings.

**`ssn_last4` is four digits by design.** Not truncated data. The engine holds full SSNs internally and masks them at every outbound point. Full digits are not sent because `CertHouseholdMember#ssn` has no `encrypts` declaration; that remains open in the schema request.

**`ref` and `member_ref` are scoped to one request.** They exist so income and asset records can point at a member inside a single payload. They carry no meaning outside it and should not be persisted as identifiers.

**`income_type` or `asset_type` of `other` is a vocabulary gap, not a description.** It means the extracted term matched nothing in the supplied picklist. The original value is named in the warnings, and those warnings are the list of terms still to agree.

**Absent is not empty.** Sections not listed here are omitted rather than sent empty, because an empty array asserts that nothing exists while absence asserts nothing at all. `expense_records` and `findings` are absent for that reason.

---

## 2. Envelope

Sent once at the top of every payload. These identify the case and the run that produced it.

| Field | Example | Meaning |
|---|---|---|
| `schema_version` | `1.3` | Payload contract version. Changes when the shape changes |
| `extraction_id` | `ext_0ca98dfd...` | Unique per run. A re-audit of the same case produces a new one, so it identifies the attempt rather than the case |
| `engine_version` | `idp-1.0.0` | Which build produced the extraction. Worth storing: it is what makes a result reproducible when a mapping question comes up months later |
| `extracted_at` | `2026-09-07T17:06:12Z` | UTC, ISO 8601. When extraction finished, not when the case was received |
| `case_ref` | `J-PORT-05754` | **Your `Job#ref_number`, echoed back.** This is the field to resolve against |
| `target.job_id` | `5754` | Echoed from the notification. Null if it was not sent |
| `target.community_id` | `67` | Echoed from the notification |
| `target.unit_number` | `3-207` | Echoed from the notification, and separately extracted onto `cert_review` from the certification itself. The two can disagree, which is worth surfacing rather than reconciling silently |

Everything in `target` is echoed from what the notification carried, not read from the documents. `cert_review.unit_number` is the extracted one.

---

## 3. `cert_review`

One per case.

| Field | Example | Meaning |
|---|---|---|
| `cert_type` | `annual` | One of `initial`, `annual`, `ar_self_cert`, `interim`. Omitted entirely if the type cannot be determined — see the AR-SC note in section 7 |
| `effective_date` | `2026-10-01` | ISO. Certification effective date |
| `unit_number` | `3-207` | As printed on the certification, which may differ from the unit record |
| `hh_size` | `1` | Count of members sent, not a figure read off the form |
| `annual_income` | `42180.00` | Household total as the certification declares it |
| `annual_assets` | `915.00` | Summed from the asset records sent, since the form carries no total |
| `head_of_household_name` | `Dolores Ackerman` | Convenience field; the authoritative flag is `is_hoh` on the member |
| `tenant_rent` | `1180.00` | |
| `contract_rent` | `1180.00` | The unit's full rent before assistance, where the form prints one (50059 field 29, lease, 3560-8); omitted otherwise |
| `gross_rent` | `1245.00` | As the form prints it: contract + UA on HUD forms, tenant + UA on a TIC |
| `gross_rent_basis` | `contract_plus_allowance` | Which definition the form's figures settle: `contract_plus_allowance` or `tenant_plus_allowance`; omitted when neither |
| `utility_allowance` | `65.00` | |
| `max_program_rent` | `1245.00` | Program rent limit. Null when not extracted, which disables the rent-limit checks |

All amounts are plain decimal strings: no currency symbol, no thousands separator, two decimal places.

---

## 4. `household_members`

| Field | Example | Meaning |
|---|---|---|
| `ref` | `m01` | Request-scoped handle. Do not persist |
| `first_name` | `Dolores` | |
| `middle_name` | `R.` | Often absent |
| `last_name` | `Ackerman` | |
| `date_of_birth` | `1987-05-19` | ISO. Null when the printed date could not be parsed, and reported in warnings |
| `is_hoh` | `true` | Exactly one member carries true |
| `relationship` | `Minor Child` | In Cartograph's vocabulary: `Head of Household`, `Spouse`, `Co-Head`, `Minor Child`, `Other Adult`, `Live-in Aide`, `Foster Child`, `Unborn`. The form's word (`Dependent`, `Son`, a 50059 code) is mapped by word and by age at the effective date: a child relationship under eighteen is `Minor Child`, from eighteen `Other Adult`. A word with no equivalent is sent as printed with a warning |
| `relationship_as_printed` | `Dependent` | The form's own word, unmapped |
| `member_status` | `Active` | `Active`, or `Unborn` for an expected child listed on the certification: no name, date of birth or SSN, counted in household size |
| `is_disabled` | `null` | true / false / **null**. See section 1 |
| `full_time_student` | `false` | Same three-state rule |
| `ssn_last4` | `4417` | Four digits. See section 1 |
| `email` | `null` | |
| `cell_phone` | `555-0164` | As printed; not normalized |
| `sort_order` | `1` | Order on the certification |

---

## 5. `income_records`

One per income source. Paystubs arrive nested underneath rather than as a flat list, because grouping by member and employer happens engine-side.

| Field | Example | Meaning |
|---|---|---|
| `member_ref` | `m01` | Null when the earner could not be matched to a member, with a warning. The record is still sent, the income is real even when its owner is uncertain |
| `income_type` | `wages_and_salaries` | From the supplied picklist. `other` means no match; see section 1 |
| `source_name` | `Redwood Facilities Group LLC` | Employer or payer as extracted |
| `frequency_of_pay` | `bi-weekly` | How often the person is paid. As the record states it; when it does not, the rate's own unit (a monthly benefit is paid monthly). Null for an hourly rate with no stated pay period |
| `rate_unit` | `hourly` | What `rate_of_pay` is per: `hourly`, `daily`, `weekly`, `bi-weekly`, `semi-monthly`, `monthly`, `quarterly`, `annually`, `per_period`. Null when the record has no rate |
| `annual_income` | `29133.00` | **The engine's annual figure for this source.** Null when no method could produce one (a record with no amount, a declaration with no period) |
| `calculation` | `{method, annual_income, details, alternatives[]}` | How the figure was reached. `method` is `paystub-based`, `history-based`, `voi-based` or `self-declared`; `details` is the arithmetic in words (`avg(2 stubs) = 560.25 × 52 = 29133.00 — only 2 pay stub(s) in the file`). `alternatives[]` are the other rows the engine computed for the source, each with `status` `audit` (a year-to-date projection run beside the primary), `rejected` (a method that produced an implausible figure, with why) or `historical` (income from a period before the certification). The methods-disagree findings are made from this comparison |
| `payment_history` | `[{date, amount}]` | For child support, alimony and benefit payment records: every payment line the document prints, as printed. `date` is null when the line prints no date (a numbered worksheet). The engine annualises from these rows; it never sums them on the document's behalf |
| `date_received` | `2026-08-04` | ISO. When the verification was received |
| `employment_start_date` | `null` | ISO |
| `employment_status` | `Active` | `Active`, `Terminated`, `On Leave`, or null. **Needs a column** |
| `termination_date` | `null` | ISO. **Needs a column** |
| `self_declared_amount` | `812.50` | The resident's own figure, distinct from anything verified |
| `source_of_declaration` | `Application` | Where the self-declared figure came from |
| `verification_status` | `verified` | `verified` (read from a third-party document), `declared_only` (the household declared it and nothing in the packet backs it), `verified_not_declared` (a document carries it and the certification does not), `self_certified` (AR-SC) |
| `pages` | `[20, 21]` | Packet pages this record was read from. Positions in the file Cartograph sent; a reviewer opens the same file and lands on the page |
| `confidence` | `{score, flag, review[]}` | The engine's confidence in this record: `score` 0 to 1, `flag` green / yellow / red, and `review`, the fields a reviewer should look at with `field`, `flag` and `reason`. See section 8a |
| `paystubs[]` | | `pay_date`, `gross_pay`, `ytd_amount`, `pay_frequency`, `pages` |
| `vois[]` | | `voi_type`, `date_received`, `rate_of_pay`, `rate_unit`, `hours_per_pay_period`, `frequency_of_pay`, `ytd_amount`, `ytd_start_date`, `ytd_end_date`, `overtime_rate`, `overtime_frequency`, `employment_start_date`. Sent only when the verification stated something (a rate, hours, a YTD figure or an overtime rate); a source verified by its stubs alone has no VOI row, so nothing arrives as a placeholder. `frequency_of_pay` here is the pay frequency, read from the stubs first. A Work Number report fills both: its pay period rows are the `paystubs[]`, its header (rate, hours, start date, YTD as of a date, status) is the VOI row |
| `zero_income` | `null` | Object when a zero-income affidavit was filed |

The `annual_income` on each record and the `annual_income` on `cert_review` answer different questions: the record's is what the engine computed from the documents, the review's is what the certification declares. When they differ the `CERT_SUMMARY_INCOME_MISMATCH` finding says by how much.

A source can arrive with paystubs and no verification entry. That is not an error: when an employer verification comes back blank, the manager substitutes paystubs, and the engine reconstructs the source from them. Those records carry no `self_declared_amount` and no verification fields, because there is no third-party document behind them.

---

## 6. `asset_records`

| Field | Example | Meaning |
|---|---|---|
| `member_ref` | `m01` | Null when the owner could not be matched, with a warning |
| `asset_type` | `checking` | From the supplied picklist. `other` means no match |
| `institution_name` | `Meridian Savings Bank` | Bank or source |
| `current_value` | `4830.00` | **Verified** balance. Present only with a statement or a VOA behind it. See section 1 |
| `manual_balance` | `915.00` | **Self-declared** balance |
| `bank_stmt_avg_balance` | `null` | Six-month average where stated, or the mean of the VOA's monthly balances when those were sent instead |
| `annual_income_from_assets` | `0.00` | |
| `interest_type` | `Percentage` | **Needs a column** |
| `percentage_of_ownership` | `100` | Joint ownership share |
| `source_of_declaration` | `null` | |
| `verification_status` | `verified` | Same values as on income records |
| `pages` | `[13]` | Packet pages this record was read from |
| `confidence` | `{score, flag, review[]}` | As on income records |
| `bank_statements[]` | | `statement_date`, `balance`. Only statements carrying at least one of the two are sent |
| `voa` | `null` | `voa_date`, `reported_value`, `source`, and `month_1_balance`…`month_6_balance` (oldest first) when the VOA lists monthly balances instead of an average |

---

## 6b. `checklist_matches`

| Field | Example | Notes |
|---|---|---|
| `finding_id` | `88412` | The case's own checklist row id, from the case request |
| `found` | `true` | The form is in the packet |
| `pages` | `[2, 3]` | Packet pages it was read from |
| `confidence` | `0.85` | Certainty of the match and the note together, 0-1 |
| `note` | `HUD 50059 present, pages 2-3. Not signed: …` | For the row's note field; no marker in the text |
| `note_source` | `scan` | Always `scan`; what tells an engine note from one staff typed, so Cartograph clears only scan notes on a rerun |

Rows the engine cannot map to a document type are not sent.

## 7. What needs adding

Three columns are being sent right now with nowhere to land:

| Table | Column | Type |
|---|---|---|
| `cert_income_records` | `employment_status` | string |
| `cert_income_records` | `termination_date` | date |
| `cert_asset_records` | `interest_type` | string |

Two decisions, neither blocking:

**AR-SC — this one does block.** `CertReview::CERT_TYPES` allows `initial`, `annual`, `interim`. The engine audits four types, and AR-SC now arrives as **`ar_self_cert`**, a fourth value. It is not collapsed onto `annual`: AR-SC applies a different rule set — the certification form is the source of truth and no third-party wage verification is expected — and sending it as an ordinary annual produces false findings about documents the file is not supposed to have. `CERT_TYPES` needs the fourth value before an AR-SC case can import.

An unmappable or missing type is now **omitted** rather than defaulted. `cert_type` selects the checklist template through `cert_type_scope`, so a guessed value produces a clean-looking audit against the wrong rule set. A missing one is visible; a plausible wrong one is not.

**`source` on the cert child tables.** Re-import clears extraction-owned records and rewrites them. Without a marker separating what the importer created from what an analyst typed, the delete removes both. This only matters on the second import of a case, by which point reviewer work is in place. Until it exists, treat a cert review that already has records as a conflict rather than proceeding.

---

## 8. `findings`

One object per finding of the audit, in the shape of the Scan Findings thread. Field-level review notes ("Review recommended" on one field of one record) are the scorer's commentary, not findings, and are not sent.

| Field | Example | Meaning |
|---|---|---|
| `finding_key` | `ASSET_SELF_DECLARED_VS_VERIFIED:randy_buck:savings` | Stable identity: code plus the subject. A re-scan that reports the same finding sends the same key, so it updates the row rather than opening another |
| `code` | `SIGNATURE_DATE_MISSING` | The finding type. `NOTE` marks a finding that is still plain text; its key is a hash of the wording with digits removed, so a re-read figure updates the same row |
| `category` | `income` | `file_review`, `income`, `asset`, `household_member`, `unit_rent` |
| `result` | `non_compliant` | `non_compliant` is an issue to review. `compliant` and `na` are informational (a name spelled two ways, an amount that is a subtotal) and should not ask for a verdict |
| `subject_type` | `income_record` | `household_member`, `income_record`, `asset_record`, `document`, or null for the case |
| `subject_label` | `Randy Buck — Midstates Bank` | Human-readable subject |
| `member_ref` | `m01` | The member the finding is about, when it names one |
| `pages` | `[13]` | Where it was found. Empty when the finding is about the case as a whole (a missing form) |
| `label` | `Self-declared asset balance differs from the verified balance` | Short title |
| `description` | | The full wording |
| `correction_required` | | What resolves it, when the rule knows |
| `assignment` | `client` | `internal`, `client`, `procedural_issue`, or null |
| `resolution_type` | `recalculation` | `presence_only` or `recalculation` |
| `disputes_extraction` | `true` | The finding says the extraction contradicts the packet; these lower the engine's own confidence |

Every scan sends the complete set. A key present in an earlier scan and absent from the latest one is cleared, not still open.

**Category and `subject_type` are the key for the next action.** A finding whose category is `asset` and whose subject is `asset_record` with no `subject_label` is a record that should exist and does not; its `correction_required` says so ("Add an asset record for the disclosed life insurance …"). The questionnaire disclosure findings arrive this way as of 23 September: `QUESTIONNAIRE_LIFE_INSURANCE_UNVERIFIED`, `_REAL_ESTATE_`, `_CHECKING_` and `_SAVINGS_` as `asset` / `asset_record`; `_EMPLOYMENT_`, `_SSA_`, `_CHILD_SUPPORT_` and `_PENSION_` as `income` / `income_record`; `QUESTIONNAIRE_STUDENT_UNVERIFIED` as `file_review` with no subject, since the missing item is a certification and not a record. Each is assigned to the client and keyed per case, so a re-scan updates the row.

Verdicts come back on a separate `findings_feedback` event, nightly, one per case: `case_ref`, `scan_id`, then `verdicts[]` of `finding_key`, `verdict` (`valid` / `invalid`), `verdict_reason`, and `manual_findings[]` of `description`, `page`, `subject_label`, `source: "manual"`, `matched_checklist_item` (key and name). The import-result callback is not the place for them: it arrives once at import time and a later call on it would overwrite the import outcome.

**`expense_records`** is still not sent. The engine does not extract expenses.

---

## 8a. `confidence`

On every household member, income record and asset record, on `cert_review`, and once at the top level for the case.

| Field | Example | Meaning |
|---|---|---|
| `score` | `0.79` | 0 to 1. Green is 0.80 and above, yellow 0.50 and above, red below. A value never found on a source page is capped at 0.79, so green means "found in the document" |
| `flag` | `yellow` | What a reviewer triages by. A record the household only declared, or one a finding disputes, cannot be green |
| `review` | `[{"field": "rateOfPay", "flag": "yellow", "reason": "Found only on the certification form, not in this record's documents"}]` | The fields to look at, each with the reason. Empty on a green record |

The top-level object carries `score`, `flag` and the field counts (`green`, `yellow`, `red`, `na`). The case flag is bounded by the worst disputed record and is yellow whenever any income is declared-only; it is a triage signal, not an average.

Green fields can go straight in. Yellow and red are the review list; the reason says what to check.

---

## 9. On the warnings

Every payload carries a list of what the mapping could not carry: a date that would not parse, an earner who matched no member, a type with no equivalent in the picklist. They are the only record of where the two vocabularies disagree, and the accumulated list is the agenda for closing the gaps.

The same applies in the other direction. Please populate `warnings` on the import result for every value that collapses, every date that fails to parse, and every member that arrives without a relationship, without them, that information is recoverable only by manual spot-checking.
