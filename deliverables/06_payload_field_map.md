# Payload Field Map

| | |
|---|---|
| Version | 1.1 |
| Date | 18 September 2026 |
| Author | Maria Azevedo |
| Status | Generated from delivered payloads; v1.1 adds `findings`, `pages` and `verification_status` |

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
| `schema_version` | `1.2` | Payload contract version. Changes when the shape changes |
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
| `gross_rent` | `1245.00` | |
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
| `relationship` | `Head of Household` | **Only sent for the head.** The engine does not extract relationships for other members; a warning names each one left unset |
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
| `frequency_of_pay` | `bi-weekly` | As printed; not normalized |
| `date_received` | `2026-08-04` | ISO. When the verification was received |
| `employment_start_date` | `null` | ISO |
| `employment_status` | `Active` | `Active`, `Terminated`, `On Leave`, or null. **Needs a column** |
| `termination_date` | `null` | ISO. **Needs a column** |
| `self_declared_amount` | `812.50` | The resident's own figure, distinct from anything verified |
| `source_of_declaration` | `Application` | Where the self-declared figure came from |
| `verification_status` | `verified` | `verified` (read from a third-party document), `declared_only` (the household declared it and nothing in the packet backs it), `verified_not_declared` (a document carries it and the certification does not), `self_certified` (AR-SC) |
| `pages` | `[20, 21]` | Packet pages this record was read from. Positions in the file Cartograph sent; a reviewer opens the same file and lands on the page |
| `paystubs[]` | | `pay_date`, `gross_pay`, `ytd_amount`, `pay_frequency`, `pages` |
| `vois[]` | | `voi_type`, `date_received`, `rate_of_pay`, `hours_per_pay_period`, `frequency_of_pay`, `ytd_amount`, `ytd_start_date`, `ytd_end_date` |
| `zero_income` | `null` | Object when a zero-income affidavit was filed |

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
| `bank_stmt_avg_balance` | `null` | Six-month average where stated |
| `annual_income_from_assets` | `0.00` | |
| `interest_type` | `Percentage` | **Needs a column** |
| `percentage_of_ownership` | `100` | Joint ownership share |
| `source_of_declaration` | `null` | |
| `verification_status` | `verified` | Same values as on income records |
| `pages` | `[13]` | Packet pages this record was read from |
| `bank_statements[]` | | `statement_date`, `balance`. Only statements carrying at least one of the two are sent |
| `voa` | `null` | `voa_date`, `reported_value`, `source` |

---

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

Verdicts come back on a separate `findings_feedback` event, nightly, one per case: `case_ref`, `scan_id`, then `verdicts[]` of `finding_key`, `verdict` (`valid` / `invalid`), `verdict_reason`, and `manual_findings[]` of `description`, `page`, `subject_label`, `source: "manual"`, `matched_checklist_item` (key and name). The import-result callback is not the place for them: it arrives once at import time and a later call on it would overwrite the import outcome.

**`expense_records`** is still not sent. The engine does not extract expenses.

---

## 9. On the warnings

Every payload carries a list of what the mapping could not carry: a date that would not parse, an earner who matched no member, a type with no equivalent in the picklist. They are the only record of where the two vocabularies disagree, and the accumulated list is the agenda for closing the gaps.

The same applies in the other direction. Please populate `warnings` on the import result for every value that collapses, every date that fails to parse, and every member that arrives without a relationship, without them, that information is recoverable only by manual spot-checking.
