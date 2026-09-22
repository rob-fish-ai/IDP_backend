# Extracted Data Reference

Everything the IDP audit engine extracts from a certification packet PDF, as it
appears in the `ExtractionResult` JSON.

Field names below are the **exact JSON keys** emitted by the engine (including
`bankStatment`, which is misspelled in the wire format and must be matched as
written). Every field is optional and may be `null` — the engine emits `null`
rather than omitting a key when a document does not state a value.

Source of truth: `app/schemas/extraction.py` (shapes) and
`app/services/extractor.py` (extraction rules and vocabularies).

---

## 1. Top-level structure

```
ExtractionResult
├── classification              per-page document type + category
├── document_groups             pages grouped into logical documents
├── household_demographics      → houseHold[]        (members)
├── certification_info          the certification form's own fields
├── previous_certification      prior cert, for IR delta comparison
├── income                      → sourceIncome.payStub[] / .verificationIncome[]
├── assets                      → assetInformation[]
├── income_calculations[]       computed annual income per member/source
├── questionnaire_disclosures   yes/no answers from the application
├── findings[]                  audit findings (text)
├── field_scores                per-field confidence scoring
└── page_ocr[]                  per-page OCR provenance
```

`document_inventory_financial` and `document_inventory_hud` still exist in the
schema for backward compatibility but are **always empty** — the extractors that
populated them were removed as dead code (commit `26f4da0`).

---

## 2. Document classification

Every page is classified into one canonical document type and one of three
categories. Only `include` types are sent for data extraction.

**Categories:** `include` (data-extracted) · `compliance` (required forms,
presence checked but not extracted) · `ignore` (not processed)

### include

| Type | Notes |
|---|---|
| HUD 50059 | HUD Owner's Certification of Compliance |
| Tenant Income Certification (TIC) | LIHTC / state HFA TIC forms |
| HUD 3560 Form | USDA RD 3560-8 Tenant Certification |
| HUD Model Lease | Section 8/202/236 lease — carries rent + effective date |
| Application / Housing Questionnaire | |
| Verification of Income (VOI) | |
| Verification of Assets (VOA) | |
| Work Number / Equifax Report | |
| Paystub | |
| SSA Benefit Letter | |
| SSI Benefit Letter | |
| SSDI Benefit Letter | |
| Verification of Disability Benefits | private LTD/STD insurer letters |
| Pension Statement | |
| TANF Verification | |
| Child Support Statement | |
| Bank Statement | |
| Life Insurance Policy | |
| Asset Self-Certification | |
| Student Status Certification | |
| Zero Income Certification | |
| Self-Employment Affidavit | |
| Debit Card Asset Self-Certification | |
| HomeBASE Verification | |
| Unemployment Affidavit | |
| Notice of Rent Change | |
| Identity Document | driver license / state ID / SSN card |

### compliance

HUD 9887 · HUD 9887-A · HUD 92006 · HUD Race and Ethnic Data Form ·
Citizenship Declaration · Acknowledgement of Receipt · Tenant Release and
Consent Form · VAWA Lease Addendum · Lead-Based Paint Certification ·
EIV Summary Report

### ignore

Income Calculation Worksheet · Certification Review · Receipt / Purchase
Documentation · File Order Form · Blank Page · Blank Form · Correspondence ·
Fax Cover Sheet · Credit Screening Report · Screening Affidavit ·
Maintenance / Inspection Form · Unknown

### Per-page record (`classification.pages[]`)

| Field | Description |
|---|---|
| `page` | 1-indexed page number |
| `document_type` | one of the canonical types above |
| `category` | `include` / `compliance` / `ignore` |
| `person_name` | person the document belongs to, when identifiable |
| `confidence` | 0.0–1.0 classifier confidence |
| `notes` | free text |

### Document group (`document_groups[]`)

Contiguous pages of one logical document: `document_type`, `category`,
`person_name`, `pages[]`, `page_range` (e.g. `"4-6"`), `combined_text`, `notes`.

---

## 3. Household demographics — `household_demographics.houseHold[]`

One entry per household member.

| Field | Description |
|---|---|
| `householdMemberNumber` | 2-digit string with leading zero (`"01"`). TIC "HH Mbr#" / 50059 field 33 / RD 3560 row order |
| `FirstName` | |
| `MiddleName` | |
| `LastName` | |
| `socialSecurityNumber` | transcribed **as printed** (full or masked); masked at every egress — see §10 |
| `DOB` | date of birth as printed |
| `gender` | `"M"` / `"F"` — only when a document states it (50059 field 38, Race & Ethnic Data form, ID). Never inferred from names |
| `head` | head-of-household indicator |
| `disabled` | disability indicator |
| `student` | student indicator |
| `email` | |
| `phone` | |

**Not extracted:** street address, race, ethnicity, marital status, citizenship,
relationship-to-head. The engine reads the unit's address from the case record,
not from the packet.

---

## 4. Certification info — `certification_info`

Fields from the certification form itself (TIC / 50059 / RD 3560 / lease).

| Field | Description |
|---|---|
| `certificationType` | `MI` · `AR` · `AR-SC` · `IR` |
| `effectiveDate` | certification effective date |
| `numberOfBedrooms` | |
| `grossRent` | |
| `tenantRent` | |
| `utilityAllowance` | |
| `rentLimit` | program rent limit |
| `householdIncome` | total annual household income as stated on the form |
| `householdSize` | |
| `unitNumber` | |
| `signatureDate` | date the cert form was signed |
| `isSigned` | whether required signatures are present |
| `applicationSignDate` | date the application/questionnaire was signed |
| `formsPresent[]` | compliance forms found in the packet |
| `missingForms[]` | compliance forms expected but absent |
| `complianceStatus` | `Complete` · `Incomplete` · `Pending Review` |

---

## 5. Previous certification — `previous_certification`

Extracted only when the packet contains a prior certification (used for
income-recertification delta comparison).

`effectiveDate`, `certificationType`, `householdIncome`, `tenantRent`,
`grossRent`, `utilityAllowance`, `householdSize`, `source_pages[]`

`income_by_source[]` — per prior income source:
`incomeType`, `sourceName`, `memberName`, `annualAmount`

---

## 6. Income — `income.sourceIncome`

Two parallel arrays: raw paystubs, and everything else (verifications,
benefit letters, self-declarations).

### 6.1 `payStub[]`

One entry **per individual paystub**, not per employer.

| Field | Description |
|---|---|
| `sourceName` | employer name |
| `memberName` | household member the stub belongs to |
| `socialSecurityNumber` | as printed on the stub |
| `grossPay` | gross pay for the period |
| `payDate` | |
| `payInterval` | pay period frequency |
| `ytdGross` | year-to-date gross on that stub |

### 6.2 `verificationIncome[]`

One entry per income source per member.

| Field | Description |
|---|---|
| `sourceName` | employer / agency / program name |
| `memberName` | |
| `socialSecurityNumber` | |
| `programName` | benefit program, where applicable |
| `incomeType` | see vocabulary below |
| `type_of_VOI` | see vocabulary below |
| `selfDeclaredAmount` | amount the household stated |
| `selfDeclaredSource` | which document the self-declaration came from |
| `rateOfPay` | |
| `frequencyOfPay` | |
| `hoursPerPayPeriod` | |
| `overtimeRate` | |
| `overtimeFrequency` | |
| `ytdAmount` | |
| `ytdStartDate` | |
| `ytdEndDate` | |
| `employmentStatus` | `Active` · `Terminated` · `On Leave` |
| `hireDate` | |
| `terminationDate` | |
| `dateReceived` | date the verification was signed/received by the source |
| `address` | `{street, city, state (2-letter), zip (5-digit)}` |

**`incomeType` vocabulary:** Non-Federal Wage · Federal Wage · Social Security ·
Supplemental Security Income · Social Security Disability · Pension ·
Temporary Assistance · Child Support · Self-Employment · Zero Income ·
Other Income

**`type_of_VOI` vocabulary:** Employer Verification · SSA Benefit Letter ·
Agency Benefit Letter · Child Support Order · Pension Statement ·
Self-Declaration · Work Number · ScreeningWorks · Vault Verify

**`selfDeclaredSource` vocabulary:** Questionnaire · Application ·
Self-Certification TIC · Resident Affidavit/Certification ·
Asset Under 5,000 or 50,000 Form · Other

---

## 7. Assets — `assets.assetInformation[]`

| Field | Description |
|---|---|
| `documentType` | source document kind (see vocabulary) |
| `assetOwner` | household member who owns the asset |
| `socialSecurityNumber` | |
| `sourceName` | institution name |
| `accountType` | see vocabulary |
| `accountNumber` | |
| `currentBalance` | |
| `averageSixMonthBalance` | |
| `selfDeclaredAmount` | |
| `selfDeclaredSource` | same vocabulary as income |
| `dateReceived` | |
| `incomeAmount` | income generated by the asset |
| `interestType` | |
| `percentageOfOwnership` | |
| `address` | `{street, city, state, zip}` — for real estate |
| `bankStatment[]` | **(sic)** nested statements — see below |
| `verificationOfAsset` | nested VOA record — see below |

**`accountType` vocabulary:** Checking · Savings · CD · Investment ·
Retirement · Life Insurance · Cryptocurrency · Prepaid Card · Peer-to-Peer ·
ABLE Account · Real Estate · Cash · Annuity · Direct Express

**`documentType` vocabulary:** Bank Statement — Checking · Bank Statement —
Savings · Verification of Assets · Life Insurance · Investment Account ·
Asset Self-Certification · ABLE Account · Real Estate · Certificate of
Deposit · Cryptocurrency · Prepaid Card · Annuity · Direct Express Card

### 7.1 `bankStatment[]` (nested)

`statementDate`, `balance`, `accountNumber`, `income`, `incomeFixedValue`,
`incomeFromAsset`, `interestRate`, `currentMortgageBalance`,
`netValueRealEstate`, `realEstateCurrentMarketValue`, `totalClosingCosts`,
`percentageOfOwnership`

### 7.2 `verificationOfAsset` (nested)

`accountNumber`, `currentBalance`, `averageSixMonthBalance`, `dateReceived`,
`incomeAmount`, `interestType`, `interestRate`, `percentageOfOwnership`

---

## 8. Income calculations — `income_calculations[]`

**Computed, not extracted.** One entry per member per income source per method.

| Field | Description |
|---|---|
| `memberName` | |
| `sourceName` | |
| `incomeType` | carried from the source record |
| `method` | `self-declared` · `paystub-based` · `ytd-based` · `voi-based` |
| `annualIncome` | numeric string, 2 decimals |
| `details` | human-readable explanation of the calculation |

`details` may be prefixed `[audit]` or `[historical]` for supplementary
calculations that should not be summed into the household total.

---

## 9. Questionnaire disclosures — `questionnaire_disclosures`

Yes/no answers extracted from the application/questionnaire, used to detect
income and asset sources that were disclosed but never documented.

`has_employment` (plus `employers[]`) · `has_student_status` ·
`has_ssa_benefits` · `has_checking_account` · `has_savings_account` ·
`has_child_support` · `has_pension` · `has_self_employment` ·
`has_other_income` · `has_real_estate` · `has_life_insurance`

---

## 10. Quality, provenance, and findings

### `findings[]`
Audit findings as plain strings (discrepancies, missing documents, calculation
mismatches). *Not yet structured objects — see integration notes.*

### `field_scores`
Per-field confidence scoring: each extracted field carries stage scores
(extraction quality, source verification, cross-document consistency) that roll
up to a record-level and case-level confidence.

### `page_ocr[]`
Per-page OCR provenance, persisted so a later review can tell an OCR failure
from an extraction failure.

| Field | Description |
|---|---|
| `page` | page number |
| `flag` | `green` · `yellow` · `red` |
| `score` | composite OCR quality score |
| `chars` | character count |
| `flags[]` | `blank_page`, `vision_fallback`, `suspected_content_loss`, `unread_region`, `auto_rotated`, `possible_hallucination`, … |
| `text` | the sanitized text extraction actually consumed |

### SSN handling
SSNs are transcribed **as printed** (full digits when the document shows them)
and stored as captured internally, but **masked at every egress**: API
responses, findings, and any write-back are masked to `***-**-1234`. The only
unmasked surface is the analyst review export, which is generated on request
and must be handled as sensitive.

---

## 11. Notes for downstream integration

Gaps to be aware of when mapping this output into another system:

1. **No street address per member.** The engine reads the unit address from the
   case record, not the packet.
2. **No race / ethnicity / marital status / citizenship / relationship-to-head.**
   These arrive through the application flow, not certification extraction.
3. **Vocabularies are document-flavored, not system-flavored.** Values here are
   normalized to *this* engine's vocabulary (e.g. `incomeType: "Non-Federal
   Wage"`, `method: "paystub-based"`). A consuming system with its own enums
   needs an explicit mapping layer; do not assume string equality.
4. **`findings[]` are unstructured strings.** Anything that needs to reference a
   specific finding (corrections tied to a finding, accept/reject workflows)
   requires findings to become structured objects with stable IDs first.
5. **`bankStatment` is misspelled** in the wire format. Match it as written.
6. **Income calculations are opinionated.** If the consuming system computes its
   own annual income, expect divergence and decide explicitly which number is
   authoritative.
