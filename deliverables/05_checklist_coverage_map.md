# Checklist Coverage Map

| | |
|---|---|
| Version | 1.0 |
| Date | August 2026 |
| Author | Maria Azevedo |
| Scope | The 41 `CORE-` codes applied to production 2026-08-26 |

What the audit engine can determine for each generic checklist item, and what
each unanswered one needs. This is the specification for the checklist emitter.

Coverage is assessed against the engine's actual capabilities: its document
classification taxonomy, its extraction schema, its signature validator, and
its certification-type rules. It is not an estimate of what could be built.

---

## 1. Summary

| Coverage | Count | Meaning |
|---|---|---|
| **Auto** | 21 | Answerable today from the packet alone |
| **Partial** | 6 | Answerable in most cases; some inputs missing or reviewer confirms |
| **Needs reference data** | 5 | Requires Cartograph's stored rent and income limits |
| **Needs expense extraction** | 3 | The engine extracts no expense data at all |
| **Contested** | 1 | Both systems compute it; authority already agreed |
| **Reviewer only** | 5 | Compliance judgments, not facts in the packet |

**27 of 41 are answerable today.** Adding the reference-data comparison takes
it to 32; adding expense extraction takes it to 35. The remaining 5 are
programme and set-aside determinations that live in Cartograph, not in a
certification packet, and should stay with the reviewer.

---

## 2. Three things that apply throughout

**Signature items mean "present and dated", not "signed".** The engine treats a
form that is present but shows no signature as *unverifiable* rather than
proven-missing. Handwritten signatures do not survive OCR, and text-level
signature counting fired on 99% of cases with no correlation to reviewer
rejections. Where a signature genuinely must be confirmed, the engine performs a
targeted vision check on that page.

**Scope differs by item.** Some are per-case, some per household member, some
per income or asset record. The engine emits one finding per subject, so a
six-member household produces six results for a member-scoped item.

**Category is Cartograph's to resolve.** Four labels appear under `file_review`
at one community and `general_requirements` at another while sharing a code. The
engine sends `item_code` and lets the template row determine the category.

---

## 3. Unit and rent (9 items)

| Code | Coverage | Basis, or what is missing |
|---|---|---|
| `CORE-UR-TENANTRENT` | Auto | Tenant rent extracted from the certification form |
| `CORE-UR-UAAPPLIED` | Partial | Arithmetic check: gross rent = tenant rent + utility allowance |
| `CORE-UR-UNITTYPE` | Partial | Bedroom count extracted; needs the unit record to confirm |
| `CORE-UR-RENTLIMIT` | Reference data | Gross rent extracted. The authoritative limit is Cartograph's |
| `CORE-UR-RENTCAP` | Reference data | Same comparison as above, differently worded |
| `CORE-UR-INCCAP` | Reference data | Household income extracted. Income limit is Cartograph's |
| `CORE-UR-UA` | Reference data | Utility allowance extracted. Whether it is *current* needs the schedule |
| `CORE-UR-SETASIDE` | Reviewer only | Set-aside assignment lives in Cartograph, not the packet |
| `CORE-UR-SETASIDETIER` | Reviewer only | As above |

This category was empty for CT 8-30G and is the one most changed by Cartograph
storing rent and income limits per unit. Four of these become answerable the
moment those values reach the engine.

`CORE-UR-RENTLIMIT` and `CORE-UR-RENTCAP` appear to be the same requirement
worded twice. Worth confirming before the engine answers both from one check.

---

## 4. Household members (6 items)

Per member.

| Code | Coverage | Basis, or what is missing |
|---|---|---|
| `CORE-HHM-AGE` | Auto | Date of birth extracted, cross-checked against the certification form |
| `CORE-HHM-SSN` | Auto | Identity document and social security card against the form's last four |
| `CORE-HHM-STUDENT` | Auto | Student flag from the certification form and questionnaire |
| `CORE-HHM-STUDENTCERT` | Auto | Student Status Certification classified, presence and date checked |
| `CORE-HHM-9887` | Auto | HUD 9887 and 9887-A classified; page counts validated per adult |
| `CORE-HHM-CITIZEN` | Partial | Citizenship Declaration classified, but only expected on HUD and USDA properties. Blank on LIHTC-only files rather than failed |

---

## 5. Income (6 items)

Per income record.

| Code | Coverage | Basis, or what is missing |
|---|---|---|
| `CORE-INC-TYPE` | Auto | Income type extracted per source |
| `CORE-INC-PAYSTUBS` | Auto | Paystub count and date span per employer |
| `CORE-INC-PERIOD` | Auto | Hire date, termination date, employment status, and paystub span |
| `CORE-INC-3PVERIFY` | Auto | Verification of income present for the source |
| `CORE-INC-VOE` | Auto | Same evidence as third-party verification |
| `CORE-INC-ANNUALCALC` | Contested | The engine computes annual income; Cartograph computes its own. Per the agreed rule Cartograph is authoritative and the engine raises material divergence as a finding rather than asserting the item |

`CORE-INC-3PVERIFY` and `CORE-INC-VOE` resolve to the same evidence. Confirm
whether they are meant to differ.

---

## 6. Assets (5 items)

Per asset record.

| Code | Coverage | Basis, or what is missing |
|---|---|---|
| `CORE-AST-STMT` | Auto | Bank statement or verification of assets classified for the account |
| `CORE-AST-BALANCE` | Auto | Current balance against statement and verification values |
| `CORE-AST-INCCALC` | Auto | Income from asset extracted |
| `CORE-AST-PASSBOOK` | Partial | The engine determines whether household assets exceed $5,000; applying the passbook rate is Cartograph's calculation |
| `CORE-AST-IMPUTE` | Partial | As above. The engine can flag assets over threshold with no imputed income |

---

## 7. General requirements and file review (12 items)

Per case.

| Code | Coverage | Basis, or what is missing |
|---|---|---|
| `CORE-GEN-TIC` | Auto | TIC classified, signature date extracted, vision-verified |
| `CORE-GEN-9887` | Auto | HUD 9887 and 9887-A classified, page counts validated per adult |
| `CORE-GEN-9200` | Auto | HUD 92006 classified and date-checked |
| `CORE-GEN-RACEETH` | Auto | Race and Ethnic Data form classified per member |
| `CORE-GEN-LEASE` | Auto | HUD Model Lease classified |
| `CORE-GEN-PRIORCERT` | Auto | Previous certification extracted when present in the packet |
| `CORE-GEN-APPSIGNED` | Auto | Application classified, signature date extracted, adult count known |
| `CORE-GEN-CERTPERIOD` | Partial | Effective date extracted; confirming the period is *correct* needs the expected schedule |
| `CORE-GEN-INCLIMITS` | Reference data | Needs Cartograph's income limits for the certification year |
| `CORE-GEN-8823` | Reviewer only | IRS 8823 and 9001 reporting is a compliance judgment, not a document fact |
| `CORE-GEN-SETASIDE` | Reviewer only | Set-aside confirmation lives in Cartograph |
| `CORE-GEN-SETPCT` | Reviewer only | As above |

`CORE-GEN-9200` is labelled "Form 92006". If the code is meant to read `92006`,
better corrected before anything depends on it.

---

## 8. Expenses (3 items)

| Code | Coverage | Basis, or what is missing |
|---|---|---|
| `CORE-EXP-AMOUNT` | Not extracted | The engine extracts no expense data |
| `CORE-EXP-ELIGIBLE` | Not extracted | As above |
| `CORE-EXP-DOCS` | Not extracted | As above |

These are live at five communities and will return blank on every case until
expense extraction exists. Medical, dependent-care, and disability-assistance
deductions affect adjusted income on HUD certifications, so this is a real gap
rather than a cosmetic one.

---

## 9. What unlocks the remaining items

1. **Rent and income limits reaching the engine.** Unlocks 5 items and improves
   3 more. Whether they arrive in the trigger payload or through a lookup is an
   open question; the trigger is simpler.
2. **Expense extraction.** Unlocks 3 items. New extraction work.
3. **The unit record** (bedroom count, set-aside). Improves `CORE-UR-UNITTYPE`
   and would move the two set-aside items out of reviewer-only, though those may
   be better left with a human.

---

## 10. Open questions

1. `CORE-UR-RENTLIMIT` and `CORE-UR-RENTCAP` appear to be the same requirement.
2. `CORE-INC-3PVERIFY` and `CORE-INC-VOE` resolve to the same evidence.
3. Should `CORE-INC-ANNUALCALC` be answered by the engine at all, given
   Cartograph computes the authoritative figure?
4. Confirm the engine sends `item_code` only and Cartograph resolves category.
5. `CORE-GEN-9200` versus `92006`.
