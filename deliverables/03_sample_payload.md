# Sample Ingest Payload

| | |
|---|---|
| Version | 1.1 |
| Date | August 2026 |
| Author | Maria Azevedo |
| Companion to | Ingest Payload Contract v1.2 |

One fully populated request body for `POST /webhooks/runpod_ocr_results`, provided so the importer can be built and tested against a concrete example.

All names, identifiers, addresses, and amounts in this document are invented. The record structure reflects a typical certification packet; the data does not correspond to any resident, property, or case.

The example exercises every path: a two-member household, one member with paystubs and a verification of income, one member with a zero-income affidavit, an asset with both statements and a VOA, an asset whose type collapses to `other`, an expense record, a finding carrying a checklist `item_code`, and findings at three different subject levels.

---

## Request

```
POST /webhooks/runpod_ocr_results HTTP/1.1
Host: cartograph.example.com
Content-Type: application/json
X-Timestamp: 1787059329
X-Signature: 5f2c9a41e7b3d08c6114ae59f37206bd8c4a1e93b27d5f60a8e14c72d9038bfe
```

---

## Body

```
{
  "schema_version": "1.2",
  "extraction_id":  "ext_01JQ4XKM7P2N8VBC3RTY6WZQ",
  "engine_version": "idp-2.5.0",
  "extracted_at":   "2026-08-17T14:22:09Z",

  "case_ref":       "CAS600142",
  "target": {
    "job_id":       4821,
    "community_id": 67,
    "unit_number":  "2-114"
  },

  "cert_review": {
    "cert_type":              "annual",
    "effective_date":         "2026-07-01",
    "move_in_date":           "2019-03-15",
    "unit_number":            "2-114",
    "hh_size":                2,
    "annual_income":          "46377.18",
    "annual_assets":          "3362.55",
    "head_of_household_name": "Marcus Halvorsen",
    "tenant_rent":            "865.00",
    "gross_rent":             "940.00",
    "utility_allowance":      "75.00",
    "max_program_rent":       "1024.00"
  },

  "household_members": [
    {
      "ref":               "m01",
      "first_name":        "Marcus",
      "last_name":         "Halvorsen",
      "date_of_birth":     "1981-03-14",
      "relationship":      "Head of Household",
      "is_hoh":            true,
      "is_disabled":       false,
      "full_time_student": false,
      "ssn_last4":         "3168",
      "email":             "m.halvorsen@example.com",
      "cell_phone":        "555-0142",
      "sort_order":        1
    },
    {
      "ref":               "m02",
      "first_name":        "Alina",
      "last_name":         "Halvorsen",
      "date_of_birth":     "2008-09-27",
      "is_hoh":            false,
      "is_disabled":       false,
      "full_time_student": true,
      "ssn_last4":         "7204",
      "sort_order":        2
    }
  ],

  "income_records": [
    {
      "member_ref":            "m01",
      "income_type":           "non_federal_wages",
      "source_name":           "Northgate Logistics LLC",
      "verification_status":   "received",
      "frequency_of_pay":      "biweekly",
      "date_received":         "2026-06-18",
      "employment_start_date": "2019-04-01",
      "employer_address":      "1400 Depot Rd, Salem, OR 97301",
      "self_declared_amount":  "45900.00",
      "source_of_declaration": "Application",

      "paystubs": [
        { "pay_date": "2026-05-08", "gross_pay": "1742.10",
          "ytd_amount": "15678.90", "pay_frequency": "biweekly" },
        { "pay_date": "2026-05-22", "gross_pay": "1742.10",
          "ytd_amount": "17421.00", "pay_frequency": "biweekly" },
        { "pay_date": "2026-06-05", "gross_pay": "1908.65",
          "ytd_amount": "19329.65", "pay_frequency": "biweekly" },
        { "pay_date": "2026-06-19", "gross_pay": "1742.10",
          "ytd_amount": "21071.75", "pay_frequency": "biweekly" }
      ],

      "vois": [
        { "voi_type":             "Employer Verification",
          "date_received":        "2026-06-18",
          "rate_of_pay":          "21.78",
          "hours_per_pay_period": "80.00",
          "frequency_of_pay":     "biweekly",
          "ytd_amount":           "18406.00",
          "ytd_start_date":       "2026-01-01",
          "ytd_end_date":         "2026-06-05",
          "source_phone":         "555-0188" }
      ],

      "zero_income": null
    },
    {
      "member_ref":          "m02",
      "income_type":         "other",
      "source_name":         "Zero Income Certification",
      "verification_status": "received",
      "paystubs":            [],
      "vois":                [],
      "zero_income": {
        "affidavit_date": "2026-06-02",
        "signed":         true,
        "notes":          "Full-time student, no earned income declared"
      }
    }
  ],

  "asset_records": [
    {
      "member_ref":                "m01",
      "asset_type":                "checking",
      "institution_name":          "Cascade Credit Union",
      "current_value":             "3234.11",
      "bank_stmt_avg_balance":     "2988.40",
      "annual_income_from_assets": "12.40",
      "interest_rate":             "0.004000",
      "percentage_of_ownership":   "100",
      "verification_status":       "received",

      "bank_statements": [
        { "statement_date": "2026-04-30", "balance": "2841.77",
          "source_label": "April statement" },
        { "statement_date": "2026-05-31", "balance": "3104.02",
          "source_label": "May statement" },
        { "statement_date": "2026-06-30", "balance": "3019.41",
          "source_label": "June statement" }
      ],

      "voa": { "voa_date":       "2026-06-11",
               "reported_value": "3234.11",
               "source":         "Cascade Credit Union" }
    },
    {
      "member_ref":            "m01",
      "asset_type":            "other",
      "institution_name":      "Cardinal Prepaid Services",
      "current_value":         "128.44",
      "manual_balance":        "128.44",
      "source_of_declaration": "Asset Self-Certification",
      "verification_status":   "pending",
      "notes":                 "Original asset type: Prepaid Card",
      "bank_statements":       [],
      "voa":                   null
    }
  ],

  "expense_records": [
    { "member_ref":    "m02",
      "expense_type":  "dependent_care",
      "provider_name": "Bright Start Childcare",
      "annual_amount": "6240.00" }
  ],


  "findings": [
    {
      "finding_key":  "INCOME_YTD_MISMATCH:m01:northgate",
      "item_code":    "INC_FOUR_PLUS_PAYSTUBS",
      "category":     "income",
      "subject_ref":  { "type": "income_record", "member_ref": "m01",
                        "source_name": "Northgate Logistics LLC" },
      "label":        "Paystub year-to-date does not match verification",
      "result":       "non_compliant",
      "assignment":   "client",
      "correction_required":
        "Obtain a corrected verification of income or a further paystub for this source",
      "confidence":       0.82,
      "resolution_type":  "recalculation",
      "pages":            [12, 13, 14],
      "position":         1,
      "detail":
        "Paystub year-to-date is 19,329.65 at 2026-06-05. The employer verification reports 18,406.00 for the same period end. The 923.65 difference is not explained by the pay schedule."
    },
    {
      "finding_key":  "ASSET_NO_VERIFICATION:m01:cardinal_prepaid",
      "category":     "asset",
      "subject_ref":  { "type": "asset_record", "member_ref": "m01",
                        "institution_name": "Cardinal Prepaid Services" },
      "label":        "Asset self-declared with no statement or third-party verification",
      "result":       "non_compliant",
      "assignment":   "client",
      "correction_required":
        "Obtain a current statement or a verification of assets for this account",
      "confidence":       0.91,
      "resolution_type":  "presence_only",
      "pages":            [38],
      "position":         2
    },
    {
      "finding_key":  "CERT_NO_PREVIOUS_CERT:case",
      "category":     "file_review",
      "subject_ref":  { "type": "case" },
      "label":        "Annual recertification with no previous certification in file",
      "result":       "non_compliant",
      "assignment":   "internal",
      "correction_required":
        "Locate the prior certification for year-over-year comparison",
      "confidence":       0.88,
      "resolution_type":  "presence_only",
      "pages":            [],
      "position":         3
    },
    {
      "finding_key":  "HH_CITIZENSHIP_UNVERIFIED:m02",
      "category":     "household_member",
      "subject_ref":  { "type": "household_member", "member_ref": "m02" },
      "label":        "Citizenship declaration not located",
      "result":       "na",
      "assignment":   "internal",
      "confidence":       0.55,
      "resolution_type":  "presence_only",
      "pages":            [],
      "position":         4
    }
  ]
}
```

---

## Expected response

```
HTTP/1.1 202 Accepted

{ "ok": true, "import_id": "imp_01JQ4XKQ9R3T5YVA7BND2MFH" }
```

---

## Expected result callback

Posted by the background job to the engine once the import completes.

```
POST /integration/import_result

{
  "import_id":      "imp_01JQ4XKQ9R3T5YVA7BND2MFH",
  "extraction_id":  "ext_01JQ4XKM7P2N8VBC3RTY6WZQ",
  "job_id":         4821,
  "cert_review_id": 8891,
  "status":         "ok",
  "created": {
    "members": 2, "income_records": 2, "paystubs": 4, "vois": 1,
    "zero_income": 1, "assets": 2, "bank_statements": 3, "voa": 1,
    "expenses": 1, "findings": 4
  },
  "warnings": [
    "asset_records[1].asset_type collapsed to 'other' (original: Prepaid Card)",
    "household_members[1].relationship absent; left unset"
  ],
  "errors": []
}
```

---

## Notes for the implementer

- **`ref` values are scoped to this payload.** They carry no meaning outside the request and should not be persisted.
- **The four paystubs belong to one income record.** The engine extracts one entry per physical stub; grouping by member and employer happens engine-side, so the importer receives them already nested.
- **The second income record uses `income_type: "other"`** because `zero_income` is not a valid type. The meaning is carried by the `zero_income` child object.
- **Checklist determinations ride on findings.** A finding that answers a checklist item carries that item's `item_code`; findings not tied to an item omit the key. There is no separate checklist array, because `cert_review_checklist_items` was dropped on 2026-08-18.
- **Findings use three subject types**: income record, asset record, and case level (`file_review`, null subject). The importer resolves `subject_ref` against records created earlier in the same transaction.
- **`detail` is reviewer-facing internal guidance** and is not shown to clients.
- **The item code here is a placeholder.** Codes are per-program and editable, so they must be supplied rather than assumed. This also requires `item_code` on `cert_review_findings`, which does not exist yet.
- **Amount consistency in this example**: the four paystubs average 1,783.74 gross, which annualises at 26 periods to 46,377.18, matching `cert_review.annual_income`. The two asset balances sum to 3,362.55, matching `annual_assets`. Cartograph recomputes both from the child records; the values are included so the comparison can be verified.
