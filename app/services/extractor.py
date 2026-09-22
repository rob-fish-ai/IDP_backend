"""Field extraction service — uses LLM to extract structured data per MuleSoft schema."""

import logging
import re

from app.core.config import Settings
from app.schemas.extraction import (
    AssetExtraction,
    CertificationInfo,
    DocumentGroup,
    HouseholdDemographics,
    IncomeExtraction,
)
from app.services.doc_taxonomy import assert_known, is_current_certification_form
from app.services.llm_service import call_llm_json
from app.services import validation
from app.services.text_sanitizer import (
    strip_html,
    drop_records_without_identity,
    scrub_extracted_dict,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cert-type context injected into every extraction prompt (Section 12)
# ---------------------------------------------------------------------------

_CERT_TYPE_CONTEXT = {
    "MI": (
        "\n\nCERTIFICATION TYPE: MI (Move-In / Initial Certification)\n"
        "- Extract ALL income sources, assets, and household composition from scratch.\n"
        "- TIC/HUD 50059 is reference only — source documents (VOI, paystubs, bank statements) are the primary source of truth.\n"
        "- Self-declared amounts come from the Application for Housing.\n"
        "- There is NO previous certification to compare against."
    ),
    "AR": (
        "\n\nCERTIFICATION TYPE: AR (Annual Recertification)\n"
        "- Extract ALL current income sources and assets — full verification required.\n"
        "- TIC/HUD 50059 is reference only — source documents are the primary source of truth.\n"
        "- Self-declared amounts come from the Recertification Questionnaire.\n"
        "- A previous certification may exist for comparison but do NOT extract from it."
    ),
    "AR-SC": (
        "\n\nCERTIFICATION TYPE: AR-SC (Annual Recertification — Self-Certification)\n"
        "- CRITICAL: The TIC form IS the source of truth for all income and asset data.\n"
        "- Values from the TIC can be used as self-declared amounts on Income and Asset Worksheets.\n"
        "- Full third-party verification is NOT required — tenant self-certifies.\n"
        "- If TIC shows income amounts, use those as selfDeclaredAmount with selfDeclaredSource='Self-Certification TIC'."
    ),
    "IR": (
        "\n\nCERTIFICATION TYPE: IR (Interim Recertification)\n"
        "- Triggered by a CHANGE in income or household composition.\n"
        "- Focus on extracting the CHANGED income source(s), not re-extracting everything.\n"
        "- The Recertification Questionnaire/Report indicates what changed.\n"
        "- A previous certification exists — do NOT extract from previous cert pages.\n"
        "- TIC/HUD 50059 is reference only — source documents are the primary source of truth."
    ),
}


def _get_cert_context(certification_type: str | None) -> str:
    """Get cert-type context string for injection into LLM prompts."""
    if not certification_type:
        return ""
    return _CERT_TYPE_CONTEXT.get(certification_type.upper(), "")

# ---------------------------------------------------------------------------
# System prompts — one per MuleSoft schema
# ---------------------------------------------------------------------------

DEMOGRAPHICS_SYSTEM_PROMPT = """\
You are an expert data extractor for HUD/Affordable Housing certification documents.

Extract household member demographics from the provided document text.

CRITICAL RULES:
- SSN: transcribe EXACTLY as the document shows it — the full nine digits when printed in full, the document's own masked form (***-**-XXXX) otherwise. NEVER invent or guess digits.
- NAME FORMATTING: ALWAYS Title Case for ALL names.
- If the document is a Calculation Worksheet (keywords: "Calculation", "Calculator", "Worksheet", "Calc Sheet", "PCAP", "CF-51", "LIHTC Calc"), return {"houseHold": []} immediately.

EXTRACTION RULES:
- householdMemberNumber: 2-digit string with leading zero ("01", "02"). From TIC → "HH Mbr#"; HUD 50059 → Field 33 "No."; RD 3560 → derive from row order. null for pay stubs, bank statements, etc.
- FirstName: Title Case. null if only single name field.
- MiddleName: Title Case, or null.
- LastName: Title Case. Include suffixes (Jr., Sr., III). Preserve hyphens and multi-word names.
- socialSecurityNumber: exactly as printed (full NNN-NN-NNNN or masked ***-**-XXXX). null if not found.
- DOB: YYYY-MM-DD format. "01/15/1990" → "1990-01-15". If no day, default DD to 01.
- gender: "M" or "F" when a document states it — HUD 50059 field 38 "Sex", Race and Ethnic Data forms, ID documents. null when no document states it (do NOT infer from names).
- SOURCE PRIORITY for DOB and SSN: printed identity documents (driver
  license, state ID, Social Security card pages) are AUTHORITATIVE — when
  an Identity Document page shows a member's DOB or SSN, use that value
  over any handwritten application/questionnaire entry (handwriting is
  routinely misread). Same priority for name spellings.
- head: "H" for head of household/primary applicant. null for all others. Maximum ONE "H".
- relationship: relationship to the head of household exactly as the form states it, Title Case
  ("Head", "Spouse", "Co-Head", "Daughter", "Son", "Granddaughter", "Foster Child", "Other Adult").
  TIC → "Relationship to Head of Household" column; HUD 50059 → field 36 "Relat" (codes: H=Head,
  S=Spouse, K=Co-Head, D=Dependent, F=Foster, L=Live-in Aide, O=Other Adult — expand the code);
  RD 3560 → relationship column. null only when no form states it.
- disabled: "Y" if member is disabled, "N" if not disabled, null if unknown/not documented.
  HUD 50059 has MULTIPLE disability indicators:
  (a) Per-member: Section C column "Special Status" or "Disab" or "H/C" — check marks, "Y", "1", "X" = "Y"; blank = "N"
  (b) Family-level: Fields like "Family has Mobility Disability? N", "Family has Hearing Disability? N", "Family has Visual Disability? N" — if ALL are "N", set disabled="N" for ALL members. If ANY is "Y", set disabled="Y" for the head of household (member 1) unless a specific member is identified.
  (c) On TIC: look in HOUSEHOLD COMPOSITION for "Disability" or "Handicapped" column.
  IMPORTANT: If disability fields exist anywhere in the document (even as family-level fields), you MUST set disabled to "Y" or "N" for each member. Only set to null if NO disability information exists at all.
- student: "Y" if member is a student, "N" if not, null if unknown/not documented.
  HUD 50059: Section C column "Stdnt" or "FT Student" — check marks, "Y", "1", "X" = "Y"; blank column with no marks = "N" for all members.
  On TIC: look in HOUSEHOLD COMPOSITION for "F/T Student" or "Student" column.
  IMPORTANT: If a student column EXISTS in the document (even if all blank/empty), set student="N" for all members. Only set to null if no student column exists at all.
- email: Exact as shown. null if no valid email.
- phone: Normalize to (XXX) XXX-XXXX. null if not 10 digits.

DEDUPLICATION: Each unique member appears only once. Match by: member number (primary), SSN last 4 (secondary), FirstName+LastName (tertiary).

Return ONLY valid JSON: {"houseHold": [...]}"""

CERT_INFO_SYSTEM_PROMPT = """\
You are an expert data extractor for HUD/Affordable Housing certification documents.

Extract certification-level information from TIC forms, HUD 50059 forms, HUD 3560 forms,
or HUD Model Lease agreements. Only extract from the CURRENT certification — ignore any
document classified as "(Previous)".

CRITICAL RULES:
- If the document is a Calculation Worksheet, return {"certificationInfo": {}} immediately.
- If the document is a previous certification, return {"certificationInfo": {}} immediately.
- A HUD Model Lease is a valid source for effectiveDate, grossRent, tenantRent,
  utilityAllowance, unitNumber, and signatureDate even if the cert form itself is missing
  those fields. The lease uses plain-English numbered paragraphs, not form field codes.

ANTI-HALLUCINATION GUARD (CRITICAL):
- If a numeric field is BLANK on the form — empty line, "$" with no number,
  "$0" / "$0.00" / "0.00", dashes, "N/A", or literally nothing next to the
  label — return null for that field. Do NOT invent a plausible value.
- Every extracted value must be a literal string that appears in the document
  text. Before outputting a number, verify the exact digits are present.
- If income shows $0 across ALL sources (TIC Part III totals = $0, HUD 50059
  field 86 = $0, RD 3560-8 Line 18.f = $0), then householdIncome = "0.00"
  (not null, not a guess). Zero-income households are legitimate.
- For rent fields: if the primary form is blank, check secondary forms per
  the MULTI-SOURCE FALLBACK rules. If ALL are blank, return null.
- NEVER map unrelated numbers (e.g., security deposit, utility schedule,
  passbook rate, field numbers like "30" or "31") to rent fields.

NUMBER FORMAT — THOUSANDS SEPARATORS (CRITICAL):
US dollar amounts use COMMA as thousands separator and PERIOD as decimal.
  "$2,418"     → 2418.00 (NOT 2.42)
  "$2,418.00"  → 2418.00
  "$1,234.56"  → 1234.56
  "$54,403"    → 54403.00 (NOT 54.40)
  "$700,000"   → 700000.00 (NOT 700.00)
Always strip commas before parsing. The comma is a separator, NEVER a decimal.
A rent or income figure under $50 is almost always wrong — re-read the source
and check whether you dropped digits after a comma.

MAGNITUDE SANITY CHECKS:
- grossRent / tenantRent / utilityAllowance: typical range $50–$5,000/month.
  Values under $50 are implausible — verify against source text.
- householdIncome: typical range $5,000–$200,000/year.
- rentLimit: typical range $200–$5,000/month.
- numberOfBedrooms: 0–8 (studio = 0).
If your extracted value falls outside these ranges, re-read the document and
look for missed digits before/after a decimal or comma. Better to return null
than a value that's off by 100×.

FIELDS TO EXTRACT:
- certificationType: Certification type code. Values: "MI" (Move-In/Initial), "AR" (Annual Recertification), "AR-SC" (Annual Recert Self-Certification), "IR" (Interim Recertification). Look for: "Type of Certification" field, checkboxes for Initial/Annual/Interim, or coded fields on the form.
- effectiveDate: Effective date of the certification. YYYY-MM-DD format.
  CAUTION: TIC headers show "Effective Date" and "Move-in Date" stacked
  next to each other and OCR often interleaves them. Take the value on the
  "Effective Date" line ONLY. On a Recertification (AR/IR) the move-in
  date is an EARLIER year than the effective date — if your candidate
  date is a year (or more) before the certification period, you likely
  grabbed the move-in date; re-read the header. Never use a previous
  year's certification form for this field when a current one is present.
- numberOfBedrooms: Number of bedrooms. Numeric string.
- grossRent: Total tenant payment or gross rent amount. Numeric string with 2 decimals, no $ or commas.
  CAUTION: values labeled "Current rent limit for this unit", "Maximum
  Gross Rent Limit", "Current Maximum Gross Rent Limit" or similar are the
  LIMIT, not the rent — they belong in rentLimit, NEVER in grossRent.
  Gross rent = tenant-paid rent + utility allowance (+ rent assistance);
  on a TIC Part IX take "Total Tenant Payment" / "Tenant rent plus utility
  allowance", not the limit line above it.
- tenantRent: Tenant rent portion. Numeric string with 2 decimals.
- utilityAllowance: Utility allowance amount. Numeric string with 2 decimals.
- rentLimit: Rent limit for the unit (incl. "Current rent limit for this
  unit" / "Maximum Gross Rent Limit" lines). Numeric string with 2 decimals.
- federalRentAssistance / nonFederalRentAssistance: rent assistance the form records
  (TIC Part VI "Federal rent assistance" / "Non-federal rent assistance", HUD 50059
  assistance payment lines, Section 8 / voucher / HomeBASE subsidy amounts). Numeric
  string with 2 decimals. Record "0.00" only when the form shows a zero; use null when
  the form has no such line — a zero that was printed and a field that is absent mean
  different things to the audit.
- householdIncome: Total annual household income. Numeric string with 2 decimals.
- householdSize: Number of household members. Integer as string.
- unitNumber: Unit number or apartment number.
- signatureDate: Date the form was signed. YYYY-MM-DD.
- isSigned: "Yes" if signature is present, "No" if signature line is blank.
- applicationSignDate: Application sign date if present. YYYY-MM-DD.

DOCUMENT-SPECIFIC GUIDANCE:
- TIC Form: Cert type is in the header area (checkboxes for Initial/Annual/Interim/Other). Effective date is labeled "Effective Date" — the adjacent "Move-in Date" line is a DIFFERENT field; do not confuse them (on recertifications they differ by one or more years). Income is in Part III "Income". Rent fields are in Part IV "Rent".
- HUD 50059: Cert type is field 2b "Type of Action" (1=Initial, 2=Annual, 3=Interim, etc.). Effective date is field 2a. Field 29 = Contract Rent, Field 30 = Utility Allowance, Field 31 = Gross Rent (this is the true grossRent, NOT field 29). Field 110 = Tenant Rent. Field 86 = Total Annual Income.
- HUD 3560 (RD 3560-8 / USDA): Line 30.a = Note Rate Rent (use as tenantRent or grossRent depending on form), Line 30.b = Utility Allowance, Line 30.c = Gross Note Rate Rent (use as grossRent). Line 33 = Final NTC (Net Tenant Contribution = tenantRent). Line 18.f = Monthly Income, Line 20 = Adjusted Annual Income.
- HUD Model Lease: Gross Rent = Contract Rent + Utility Allowance. Record grossRent from the "Gross Rent" line if shown, otherwise compute Contract Rent + UA. "Tenant Rent" / tenant's portion = tenantRent. "Utility Allowance" = utilityAllowance. "Unit" / dwelling unit number = unitNumber. Lease commencement date = effectiveDate. Signature date on the lease = signatureDate.
- Self-Certification forms (OHCS "Self-Certification of Household Annual Income", NY "AR Self Certification" / "Owner's Eligibility Determination" — classified as TIC): header "Effective Date" or "Recert Yr & Effective Date" = effectiveDate; "Unit Number" / "Apt #" = unitNumber; "Add Total Annual Household Income from all Sources" (a+b) = householdIncome; the owner section's "Rent" = tenantRent, "Utility Allowance" = utilityAllowance, "Current Income Limit" and "Current Maximum Gross Rent Limit" = limits (rentLimit), NOT rent; resident + owner signature blocks = isSigned/signatureDate.

MULTI-SOURCE FALLBACK (CRITICAL):
When the primary certification form (TIC / HUD 50059 / HUD 3560) has a BLANK
field, look for the same data on a secondary form within the same group:
  1. TIC rent fields blank → check RD 3560-8 Line 30.a/b/c (on same cert)
  2. TIC income blank → check household income on RD 3560-8 Line 18.f × 12 or Line 20
  3. HUD 50059 fields missing → check attached HUD Model Lease / Notice of Rent Change
  4. Any primary form missing effectiveDate → fall back to:
     - TIC Part X "Date Signed" or Part VII "Move-in Date"
     - HUD 50059 owner signature date
     - HUD Model Lease commencement date
     - Notice of Rent Change "effective with the rent due for"
NEVER invent a rent figure that is not present somewhere in the document text.
If all sources are blank, return null for that field.

Return ONLY valid JSON: {"certificationInfo": {...}}"""

INCOME_SYSTEM_PROMPT = """\
You are an expert data extractor for HUD/Affordable Housing income documents.

Extract income data from the provided document into the MuleSoft Income schema.
Each request contains ONE document; extract only what this document states.

CRITICAL RULES:
- SSN: transcribe exactly as printed — full when shown in full, masked as shown otherwise. Never invent digits.
- NAME FORMATTING: ALWAYS Title Case.
- If document is a Calculation Worksheet, return {"sourceIncome": {"payStub": [], "verificationIncome": []}} immediately.

DOCUMENT ROUTING:
- payStub route: Pay stubs, pay-slips, Work Number/Equifax/ScreeningWorks/Vault Verify wage records
- verificationIncome route: SSA/EIV benefit letters, TANF, child support, cash contributions, pension, self-employment, sworn statements, VOI/VOE

SSA COLA NOTICE — EXTRACT THE LISTED MONTHLY AMOUNT:
A "Notice of Cost-of-Living Adjustment (COLA)" letter from SSA is a CURRENT
benefit statement, not a future projection. The monthly amount in the
"How Much You Will Get In [year]" table IS the current monthly benefit:
  - rateOfPay: the "before deductions" monthly amount (e.g. $925.90)
  - frequencyOfPay: "monthly"
  - selfDeclaredAmount: same monthly figure (extractor will annualize)
  - incomeType: "Social Security"
  - type_of_VOI: "SSA Benefit Letter"
Do NOT skip the dollar amount because the letter says "will increase" — the
current rate IS the new rate.

OTHER AGENCY BENEFIT LETTERS (Department of Veterans Affairs, pension plan,
unemployment agency, state assistance) — same treatment as the SSA letter:
  - rateOfPay: the current benefit as stated, rateUnit as the letter states it
    ("$1,435.02 monthly" → rateUnit "monthly"), frequencyOfPay the same
  - incomeType: "Veterans Benefits" for any VA letter (disability compensation,
    VA pension, DIC), "Pension" for a pension plan, "Temporary Assistance" for
    state assistance, otherwise "Other Income"
  - sourceName: the paying agency, e.g. "Department of Veterans Affairs"
  - type_of_VOI: "Agency Benefit Letter"

CHILD SUPPORT STATEMENT — ALWAYS EXTRACT:
Any "Child Support Statement", "Child Support Order", "Child Support Verification",
court order with ordered amounts, or DOR/State Disbursement Unit statement → create
a verificationIncome entry:
  - sourceName: payer name, or "Child Support" / state agency name if payer unknown
  - memberName: the custodial parent/head of household receiving support
  - incomeType: "Child Support"
  - type_of_VOI: "Child Support Order"
  - rateOfPay + rateUnit + frequencyOfPay: ONLY a court-ordered or agency-stated
    regular amount ("$162.70 per month ordered"), when the document states one
  - paymentHistory: when the document is a payment record (one line per month, week
    or payment), copy EVERY line as {"date": ..., "amount": ...} exactly as printed —
    date as YYYY-MM-DD (a month-only label such as "08/26" becomes "2026-08-01"),
    amount as a numeric string of what was actually paid that period (disbursements),
    never an arrears balance. The date is the date PRINTED on that line: a worksheet
    that numbers its payments (1, 2, 3 …) without dates gets rows with "date": null —
    never invent dates or spread numbered rows across months. Do NOT add the lines
    up, do NOT pick one line as the rate, and do NOT put a total in
    selfDeclaredAmount — the engine annualises the history itself.
Do NOT skip child support just because the form is brief or lacks typical wage fields.

PAYSTUB FIELDS:
- sourceName: the employer that ISSUED the stub — the company named in the stub's
  header (name, address, phone), the payer of record. A staffing or payroll stub
  also prints where the person was placed ("Customer", "Client", "Assignment",
  "Worksite", "Department"): that is NOT the employer. If the header is not
  legible, leave sourceName null rather than naming the customer.
- employeeId: the employee / ID number the stub prints ("Employee ID: 1342892",
  "Emp #"), verbatim. null if none.
- memberName: employee name, Title Case
- socialSecurityNumber: exactly as printed on the stub
- grossPay: exact dollar amount with cents, numeric string ("1250.00"), no $ or commas
- payDate: YYYY-MM-DD
- payInterval: lowercase (weekly / bi-weekly / semi-monthly / monthly)
- ytdGross: year-to-date gross on the stub ("YTD Gross", "YTD Earnings"),
  numeric string, no $ or commas. null if the document shows no YTD figure
  (EIV/Work Number quarterly rows have none).

SPECIAL PAYSTUB RULES:
- Work Number/Equifax: take the 6 most current entries only
- Child support and other benefit payments are NEVER pay stubs — they belong in the
  verificationIncome entry's paymentHistory
- Do NOT create pay stubs for SSA, pension, or TANF (these go to verificationIncome)

VERIFICATION INCOME FIELDS:
- sourceName, memberName, socialSecurityNumber (same rules as payStub)
- programName: official program name for benefit income
- selfDeclaredAmount: from self-cert forms, applications, or questionnaires, numeric string. Match to the corresponding employer/source by name when possible.
- dateReceived: date the VOI form was received or date signed by employer. YYYY-MM-DD. null if not shown.
- rateOfPay: numeric string — the rate exactly as the document states it, in the unit
  named by rateUnit
- rateUnit: what rateOfPay is per, as the document labels it: "hourly", "daily", "weekly",
  "bi-weekly", "semi-monthly", "monthly", "quarterly", "annually", or "per_period".
  "$18.00/hr" → "hourly"; "Salary $48,360 per year" → rateOfPay "48360.00", rateUnit
  "annually"; "$1,489.50 per month" → "monthly"; "$1,250 per pay period" → "per_period".
  REQUIRED whenever rateOfPay is set; null only when the document does not say what
  the number is per. Never write "hourly" into frequencyOfPay.
- paymentHistory: list of {"date": "YYYY-MM-DD", "amount": "123.45"} — ONLY when this
  document is a payment record listing individual payments over time (child support
  ledger, agency payment history, benefit payment record). One row per printed line,
  in the order printed, amounts as printed, date null when the line prints no date.
  Empty list otherwise. Never for pay stubs or employer wage tables (those are
  payStub entries).
- frequencyOfPay: lowercase. This is how often the person is PAID (weekly / bi-weekly / semi-monthly / monthly), NOT the rate unit. If rate is "hourly" but pay dates are 14 days apart, frequencyOfPay is "bi-weekly". Determine from pay period structure, not from rate label.
- hoursPerPayPeriod: ONLY when rateUnit is "hourly" (or "daily": then days). null for a
  salary or a periodic rate — hours never multiply those. When hourly: hours worked in
  ONE pay period — the same period frequencyOfPay names, NOT hours per week. If the document states a weekly figure, convert it: 40 hrs/week paid
  bi-weekly is 80; paid semi-monthly is 86.67; paid monthly is 173.33; paid weekly is 40.
  The annual calculation is rateOfPay x hoursPerPayPeriod x (pay periods per year), so a
  weekly figure reported here halves or quarters the person's income.
- overtimeRate: only if person actually receives overtime
- overtimeFrequency: same frequency as regular pay
- ytdAmount: only if document explicitly states "year to date". MUST BE null for SSA/fixed income.
- ytdStartDate, ytdEndDate: YYYY-MM-DD
- incomeType: one of: Non-Federal Wage, Federal Wage, Social Security, Supplemental Security Income, Social Security Disability, Pension, Veterans Benefits, Temporary Assistance, Child Support, Self-Employment, Zero Income, Other Income
- type_of_VOI: Employer Verification, SSA Benefit Letter, Agency Benefit Letter, Child Support Order, Pension Statement, Self-Declaration, Work Number, ScreeningWorks, Vault Verify
- address: {street, city, state (2-letter), zip (5-digit)} or null
- employmentStatus: "Active" if currently employed, "Terminated" if employment has ended, "On Leave" if on leave. Extract from "Presently Employed" checkbox or employment status field. This is CRITICAL for understanding the income picture.
- terminationDate: YYYY-MM-DD. Extract if employment has ended (last day worked, termination date, or separation date).
- hireDate: YYYY-MM-DD. Extract from "Date Hired", "Start Date", "Original Hire Date", or "Date of Hire".

*** EQUIFAX / WORK NUMBER SPECIAL RULES (CRITICAL — OVERRIDE DEFAULTS) ***:
- ytdStartDate: MUST be the "Original Hire Date" or "Most Recent Start Date" shown on the Equifax report. NEVER use January 1 or any calendar year start. Example: if report says "Original Hire Date: 02/13/2026", then ytdStartDate = "2026-02-13".
- ytdEndDate: MUST be the report's "Current As Of" date or "Inquiry Date". Example: if "Current As Of: 03/03/2026" or "Inquiry Date: 03/10/2026", use whichever is the later date.
- frequencyOfPay: MUST be determined from the "Pay Cycle" field or pay period dates, NOT from the rate label. "Pay Cycle: Biweekly" → frequencyOfPay = "bi-weekly". "Pay Frequency: Hourly" is the RATE unit (goes in rateOfPay), not the pay frequency. If "Pay Cycle" says "Biweekly" and rate says "$18.00 Hourly", then rateOfPay = "18.00" and frequencyOfPay = "bi-weekly".
- hireDate: Extract from "Original Hire Date" or "Most Recent Start Date".

SELF-DECLARED AMOUNTS:
- The certification form and the questionnaire are read separately and are never
  part of this request. Leave selfDeclaredAmount null unless THIS document is
  itself a self-declaration (affidavit, sworn statement, self-employment statement).

FORMATTING:
- Monetary: numeric string with 2 decimal places, no $ or commas
- Dates: YYYY-MM-DD
- Names: Title Case
- payInterval/frequencyOfPay: lowercase

Return ONLY valid JSON: {"sourceIncome": {"payStub": [...], "verificationIncome": [...]}}"""

ASSET_SYSTEM_PROMPT = """\
You are an expert data extractor for HUD/Affordable Housing asset documents.

Extract asset data from the provided document into the MuleSoft Asset schema.
Each request contains ONE document; extract only the accounts and property it shows.

CRITICAL RULES:
- SSN: transcribe exactly as printed — full when shown in full, masked as shown otherwise.
- NAME FORMATTING: ALWAYS Title Case.
- If document is a Calculation Worksheet, return {"assetInformation": []} immediately.
- Each distinct account or property on this document = ONE entry.

DOCUMENT ROUTING:
- bankStatment route: actual bank statements with transactions
- verificationOfAsset route: VOA/VOD forms, life insurance, investment statements
- Self-declared amounts: from asset self-certification, applications, questionnaires

FIELDS:
- documentType: Bank Statement — Checking, Bank Statement — Savings, Verification of Assets, Life Insurance, Investment Account, Asset Self-Certification, ABLE Account, Real Estate, Certificate of Deposit, Cryptocurrency, Prepaid Card, Annuity, Direct Express Card
- assetOwner: account holder name, Title Case
- socialSecurityNumber: exactly as printed
- sourceName: institution name, Title Case
- selfDeclaredAmount: from self-cert forms only
- accountType: Checking, Savings, CD, Investment, Retirement, Life Insurance, Cryptocurrency, Prepaid Card, Peer-to-Peer, ABLE Account, Real Estate, Cash, Annuity, Direct Express
- accountNumber: as shown on document
- currentBalance, averageSixMonthBalance: numeric string, 2 decimals
- dateReceived: YYYY-MM-DD
- incomeAmount: income from asset
- interestType: "Dollar Amount" or "Percentage"
- percentageOfOwnership: numeric string (e.g., "100", "50")
- address: {street, city, state, zip} or null

NESTED OBJECTS:
- bankStatment: array of bank statement entries. Each entry has:
  {statementDate, balance, accountNumber, currentMortgageBalance, income, incomeFixedValue, incomeFromAsset, interestRate, netValueRealEstate, percentageOfOwnership, realEstateCurrentMarketValue, totalClosingCosts}
  All monetary fields are numeric strings with 2 decimals. Empty [] if no statements.
- verificationOfAsset: {accountNumber, currentBalance, averageSixMonthBalance, dateReceived, incomeAmount, interestType, interestRate, percentageOfOwnership}. null if no VOA.

SPECIAL RULES:
- Life insurance: ALWAYS use cash/surrender value, NEVER use face value. If only face value is shown, set currentBalance to null and add note "Only face value available — cash value not provided"
- Thomson Reuters / WestlawNext VOA forms: treat as Verification of Assets. Extract per account: account number, account type (checking/savings), account balance, average balance, date received
- Joint/shared accounts: capture percentageOfOwnership. If ownership is split (e.g., 50% with non-household member), record the percentage
- Each distinct account = separate array entry
- Do NOT extract from manager worksheets (Asset Self-Certification — Manager's Worksheet)

REAL ESTATE WORKSHEET / DEED / APPRAISAL:
Always extract real estate as a separate asset record. Use the LABELS on
the form to find values — do NOT trust line numbers, since worksheet
formatting varies. Look for these labels (in this order of preference):
  - currentBalance: "Total Cash Value", "Net Value", "Cash Value of Real
    Estate", "Equity" — the value of the OWNER'S equity (after mortgage
    and closing costs). This is typically the largest dollar figure on
    the worksheet, often $50,000+ for owned property.
  - incomeAmount: "Net Income from Asset", "Annual Net Income", or
    "Income from Asset". Can be negative (rental loss) or zero.
  - realEstateCurrentMarketValue: "Current Market Value", "Market Value",
    "Appraised Value" — typically larger than currentBalance.
  - totalClosingCosts: "Total Closing Costs" or 10% of market value.
  - currentMortgageBalance: "Current Mortgage Balance".
  - sourceName: property address (e.g. "50 Juniper Lane, Framingham, MA").
  - documentType: "Real Estate"
  - accountType: "Real Estate"

MAGNITUDE CHECK FOR REAL ESTATE:
- currentBalance < $1,000 is implausible for owned real estate. If your
  parsed value is small, you've picked up a "Total Rental Income: $0" or
  a "Net Income: -$9,124" line by mistake. Re-read for the larger equity
  figure (typically 5-7 digits).
- realEstateCurrentMarketValue should be ≥ currentBalance (market value
  ≥ owner's equity).

TD BANK VOA / VERIFICATION OF DEPOSIT format:
A TD Bank VOA shows a table: Account Number | Type | Open Date | Current
Balance | Average Balance (6 months) | APR. Extract:
  - documentType: "Verification of Assets"
  - sourceName: "TD Bank"
  - accountType: "Checking" or "Savings" from Type column
  - accountNumber: last 4-8 digits
  - currentBalance: Current Balance column
  - averageSixMonthBalance: Average Balance column
  - verificationOfAsset: populate the nested object

Return ONLY valid JSON: {"assetInformation": [...]}"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_texts(groups: list[DocumentGroup]) -> list[str]:
    """Build LLM input texts from pre-routed groups. No filtering — trust the router."""
    texts = []
    for g in groups:
        if g.category == "ignore":
            continue
        texts.append(
            f"[Document: {g.document_type}, Pages: {g.page_range}, "
            f"Possibly about: {g.person_name or 'not stated'}]\n{g.combined_text}"
        )
    return texts


build_group_texts = _build_texts


def _retry_if_incomplete(label: str, *, gate, fill) -> None:
    """Run one self-healing retry pass on a just-extracted result.

    Every extractor has the same shape: do a first LLM pass, decide whether the
    result is incomplete, and if so re-ask for just the missing parts. LLM
    extraction is non-deterministic, so a single targeted retry recovers
    fields/records the first pass dropped. This driver is that shape, factored
    out so each extractor only declares what "incomplete" means and how to fix
    it; adding a retry to a new schema is then just two closures.

      gate() -> spec | falsy : a truthy "what's missing" spec when a retry is
          warranted (null fields, missed groups, amountless records, ...),
          else falsy to skip.
      fill(spec) -> None     : mutate the result in place to fill the gap.

    fill may raise — a retry failure is logged and swallowed so the first-pass
    result is never lost. Max 1 retry (no loop).
    """
    spec = gate()
    if not spec:
        return
    logger.info("%s: incomplete after first pass — running targeted retry", label)
    try:
        fill(spec)
    except Exception:
        logger.exception("%s: retry failed — keeping first-pass result", label)


# ---------------------------------------------------------------------------
# Extraction functions
# ---------------------------------------------------------------------------

def extract_demographics(
    groups: list[DocumentGroup],
    settings: Settings,
    certification_type: str | None = None,
) -> HouseholdDemographics:
    """Extract household demographics from pre-routed document groups."""
    relevant_texts = _build_texts(groups)
    if not relevant_texts:
        logger.info("No demographic documents found")
        return HouseholdDemographics()

    user_prompt = (
        "Extract household member demographics from these documents:\n\n"
        + "\n\n---\n\n".join(relevant_texts)
    )
    user_prompt += _get_cert_context(certification_type)

    result = call_llm_json(DEMOGRAPHICS_SYSTEM_PROMPT, user_prompt, settings)
    result = validation.validate_household(result)

    # Strip HTML tags and entity-only/punctuation-only values from every
    # extracted string. Then drop members with no usable name — those
    # records can't be linked downstream and would generate per-field
    # noise findings against junk identifiers.
    result = scrub_extracted_dict(result) or {}
    if isinstance(result.get("houseHold"), list):
        kept, dropped = drop_records_without_identity(
            result["houseHold"],
            identity_fields=("FirstName", "LastName", "MiddleName"),
        )
        if dropped:
            logger.warning(
                "Demographics: dropped %d member record(s) with no usable name "
                "(HTML/markup-only or empty after sanitization)", dropped,
            )
        result["houseHold"] = kept

    members = result.get("houseHold", [])

    # --- Self-healing retry: cells every certification form carries ---
    # A member row on a TIC / HUD 50059 / RD 3560 always has a DOB, an SSN
    # slot (full or masked) and a relationship. A null there after the first
    # pass is far more often the model skipping a cell than the form lacking
    # it, so re-ask for exactly those cells from the certification form's
    # text. Records that may legitimately be absent (income, assets) are
    # never retried this way — absence there is the audit fact.
    cert_texts = _build_texts(
        [g for g in groups if is_current_certification_form(g.document_type)]
    ) or relevant_texts

    def _gate():
        return required_member_gaps(members)

    def _fill(gaps: list[dict]) -> None:
        retry = retry_member_fields(cert_texts, gaps, 0, certification_type, settings)
        recovered = merge_member_fields(members, retry, allow_new=0)
        if recovered:
            logger.info("Demographics retry recovered %d member field(s): %s",
                        len(recovered), recovered)

    _retry_if_incomplete("Demographics", gate=_gate, fill=_fill)

    logger.info("Extracted %d household members", len(members))
    return HouseholdDemographics.model_validate(result)


def extract_certification_info(
    groups: list[DocumentGroup],
    settings: Settings,
    certification_type: str | None = None,
) -> CertificationInfo:
    """Extract certification-level info from pre-routed cert form groups.

    After the first extraction, checks for critical fields that came back
    null and issues ONE targeted retry for just those fields. LLM extraction
    is non-deterministic — the same prompt on the same document can skip
    fields on one run and populate them on the next. A focused retry
    ("extract ONLY these 4 fields") usually recovers them because the model
    isn't juggling 10 fields at once.

    Retry policy:
      - Trigger: any of the _CRITICAL_FIELDS is null after the first pass
      - Scope: re-ask only for the null fields (constrained prompt)
      - Merge: retry values fill nulls; already-populated fields are kept
      - Max retries: 1 (no loop)
    """
    relevant_texts = _build_texts(groups)
    if not relevant_texts:
        logger.info("No certification documents found")
        return CertificationInfo()

    user_prompt = (
        "Extract certification information from these documents:\n\n"
        + "\n\n---\n\n".join(relevant_texts)
    )
    user_prompt += _get_cert_context(certification_type)

    result = call_llm_json(CERT_INFO_SYSTEM_PROMPT, user_prompt, settings)
    result = validation.validate_certification_info(result)

    cert_info_dict = result.get("certificationInfo", {}) or {}

    # --- Self-healing retry: recover critical fields that came back null ---
    def _gate():
        return [f for f in CRITICAL_CERT_FIELDS if not cert_info_dict.get(f)]

    def _fill(missing: list[str]) -> None:
        retry_dict = _retry_cert_info_fields(
            relevant_texts, missing, cert_info_dict, certification_type, settings,
        )
        # Merge: retry values fill nulls only, never overwrite populated fields
        for field in missing:
            retry_val = retry_dict.get(field)
            if retry_val not in (None, "", "null"):
                cert_info_dict[field] = retry_val
        recovered = [f for f in missing if cert_info_dict.get(f)]
        if recovered:
            logger.info("Cert info retry recovered %d/%d fields: %s",
                        len(recovered), len(missing), recovered)

    _retry_if_incomplete("Cert info", gate=_gate, fill=_fill)

    # Cert type is caller-provided (frontend upload form / API param).
    # Overwrite any LLM guess with the authoritative value.
    if certification_type:
        cert_info_dict["certificationType"] = certification_type

    # A signature date belongs to the certification form itself. The cert
    # request carries neighbouring forms too (applicant certifications,
    # policies) and each has its own signature line; a date read from one
    # of those attached to an undated TIC on 05754. When the packet has a
    # current certification form, the date must be printed on its pages.
    form_groups = [
        g for g in groups
        if g.category != "ignore" and is_current_certification_form(g.document_type)
    ]
    if form_groups:
        form_text = strip_html("\n".join(g.combined_text or "" for g in form_groups))
        form_pages = sorted(p for g in form_groups for p in g.pages)
        for field in _FORM_DATE_FIELDS:
            value = cert_info_dict.get(field)
            if value and not _date_on_text(str(value), form_text):
                logger.warning(
                    "Cert info: %s=%s is not printed on the certification form pages %s — dropped",
                    field, value, form_pages,
                )
                cert_info_dict[field] = None

    # Strip HTML/markup leakage from field values before schema validation.
    cert_info_dict = scrub_extracted_dict(cert_info_dict) or {}

    logger.info(
        "Extracted certification info: type=%s",
        cert_info_dict.get("certificationType"),
    )
    return CertificationInfo.model_validate(cert_info_dict)


# Date fields that must be printed on the certification form's own pages.
_FORM_DATE_FIELDS = ("signatureDate",)
_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december")


def _date_on_text(iso: str, text: str) -> bool:
    """Whether an ISO date is printed on the text in any of the ways a form
    prints dates: 08/12/2026, 8/12/26, 08-12-2026, 2026-08-12, August 12,
    2026, 12 Aug 2026 — with the spaces OCR drops around separators."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso.strip())
    if not m:
        return False
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    yy = y[2:]
    name = _MONTH_NAMES[mo - 1]
    month_rx = rf"(?:{name}|{name[:3]}\.?)"
    sep = r"\s*[/.\-]\s*"
    year_rx = rf"(?:{y}|{yy})"
    patterns = (
        rf"(?<!\d){mo:02d}{sep}{d:02d}{sep}{year_rx}(?!\d)",
        rf"(?<!\d){mo}{sep}{d}{sep}{year_rx}(?!\d)",
        rf"(?<!\d){mo:02d}{sep}{d}{sep}{year_rx}(?!\d)",
        rf"(?<!\d){mo}{sep}{d:02d}{sep}{year_rx}(?!\d)",
        rf"(?<!\d){y}\s*-\s*{mo:02d}\s*-\s*{d:02d}(?!\d)",
        rf"{month_rx}\s+{d}(?:st|nd|rd|th)?\s*,?\s+{y}",
        rf"(?<!\d){d}\s*{month_rx}\s*,?\s*{y}",
    )
    low = text.lower()
    return any(re.search(rx, low) for rx in patterns)


# Fields considered critical for cert_info. If any of these come back null
# from the first extraction, a targeted retry runs to try to recover them.
# These are the fields that downstream scoring, compliance checks, and the
# frontend display all depend on.
CRITICAL_CERT_FIELDS = [
    "effectiveDate",
    "grossRent",
    "tenantRent",
    "utilityAllowance",
    "householdIncome",
    "unitNumber",
    "householdSize",
    "numberOfBedrooms",
]


_CERT_INFO_RETRY_PROMPT = """\
You are re-examining a HUD/Affordable Housing certification form to extract
specific fields that were missed on the first pass.

Extract ONLY the fields listed below. Return a JSON object containing just
those field names as keys. If a field is genuinely not present in the
document (e.g., the form line is blank), return null for that key — but try
hard first: the field is almost certainly in the text somewhere.

Field meanings:
- effectiveDate: Effective date of this certification (YYYY-MM-DD). Check, in order:
  (1) TIC/HUD 50059/RD 3560 "Effective Date" field,
  (2) TIC Part X "Date Signed" or Part VII "Move-in Date",
  (3) HUD 50059 owner signature date,
  (4) HUD Model Lease commencement date,
  (5) Notice of Rent Change / Lease Amendment "effective with the rent due for [date]".
- grossRent: Gross rent amount (numeric, 2 decimals, no $ or commas). For HUD
  50059 this is FIELD 31, NOT field 29. For RD 3560-8 this is Line 30.c.
  If blank on the primary form, check RD 3560-8 / Model Lease / Notice of Rent Change.
- tenantRent: Tenant's portion of rent (numeric). HUD 50059 field 110, TIC Part IV
  "Tenant Rent", RD 3560-8 Line 33 (Final NTC). If blank, check lease or notice.
- utilityAllowance: Utility allowance amount (numeric). HUD 50059 field 30, RD 3560-8
  Line 30.b, TIC Part IV "Utility Allowance".
- householdIncome: Total annual household income (numeric). HUD 50059 field 86, TIC
  Part III "Total Income (E)", RD 3560-8 Line 18.f × 12 or Line 20.
- unitNumber: Unit number or apartment number. Strip any building prefix
  (e.g., "Bldg 2 Unit 27" → "27", "2 27" → "27").
- householdSize: Number of household members (integer as string)
- numberOfBedrooms: Number of bedrooms (numeric string)

Return ONLY valid JSON: {"field_name": value, ...}
Do NOT include fields not in the missing list.

ANTI-HALLUCINATION GUARD (CRITICAL):
- Every extracted number must be literal text in the document. If the field
  is blank (empty, "$" with no number, "$0", "N/A"), return null.
- Do NOT invent plausible values. Better to return null than to guess.
- Do NOT map field numbers ("30", "31", "86"), line numbers, percentages,
  or unrelated figures (security deposit, passbook rate) to rent fields.
- For zero-income households where all income sources show $0, return
  "0.00" for householdIncome — not a guess, not null.

NUMBER FORMAT (CRITICAL):
US dollar amounts: comma = thousands separator, period = decimal.
  "$2,418"   → 2418.00 (NOT 2.42)
  "$54,403"  → 54403.00 (NOT 54.40)
  "$700,000" → 700000.00
ALWAYS strip commas before parsing. Rent under $50 or income under $1,000
is almost always wrong — re-read for missed digits."""


def _retry_cert_info_fields(
    relevant_texts: list[str],
    missing_fields: list[str],
    already_extracted: dict,
    certification_type: str | None,
    settings: Settings,
) -> dict:
    """Targeted retry for specific missing cert_info fields.

    Sends a narrower prompt asking for ONLY the missing fields, along with
    the already-extracted values as context so the model can cross-reference.
    """
    # Show already-extracted values so the retry has context but knows not
    # to overwrite them.
    context_lines = [
        f"  {k}: {v}" for k, v in already_extracted.items()
        if v not in (None, "", "null") and k not in missing_fields
    ]
    context_block = (
        "\nAlready extracted (for context, do NOT re-extract these):\n"
        + "\n".join(context_lines) if context_lines else ""
    )

    user_prompt = (
        f"Missing fields to extract: {', '.join(missing_fields)}\n"
        f"{context_block}\n\n"
        f"DOCUMENT TEXT:\n\n"
        + "\n\n---\n\n".join(relevant_texts)
    )
    user_prompt += _get_cert_context(certification_type)

    try:
        result = call_llm_json(_CERT_INFO_RETRY_PROMPT, user_prompt, settings)
    except Exception:
        logger.exception("Cert info retry call failed — keeping original values")
        return {}

    # Accept either {"field": val} or {"certificationInfo": {"field": val}}
    if isinstance(result, dict) and "certificationInfo" in result:
        result = result.get("certificationInfo") or {}
    return result if isinstance(result, dict) else {}

def retry_cert_info_fields(
    relevant_texts: list[str],
    missing_fields: list[str],
    already_extracted: dict,
    certification_type: str | None,
    settings: Settings,
) -> dict:
    """Public entry to the targeted cert-field retry, for the pipeline's
    second-stage recovery from page images (see pipeline
    `_recover_required_fields_from_images`)."""
    return _retry_cert_info_fields(
        relevant_texts, missing_fields, already_extracted, certification_type, settings,
    )


# ---------------------------------------------------------------------------
# Household member required-field retry
# ---------------------------------------------------------------------------

# Cells every certification form prints for every member row. Null here is an
# extraction gap to retry, not a fact about the household. (An SSN slot can
# legitimately be blank for some minors and exempt statuses — that is a
# finding downstream; the retry only asks the form once more.)
REQUIRED_MEMBER_FIELDS = ("DOB", "socialSecurityNumber", "relationship")


def _member_name_key(m: dict) -> str:
    first = (m.get("FirstName") or "").strip().lower()
    last = (m.get("LastName") or "").strip().lower()
    return f"{first} {last}".strip()


def _loose_name_key(m: dict) -> str:
    """First token of the first name + last name — tolerates a retry row
    that folded a middle name into FirstName ("Arnold Ray")."""
    first = ((m.get("FirstName") or "").strip().lower().split(" ") or [""])[0]
    last = (m.get("LastName") or "").strip().lower()
    return f"{first} {last}".strip()


def required_member_gaps(members: list[dict]) -> list[dict]:
    """[{name, missing: [field, ...]}] for members with a null required cell."""
    gaps = []
    for m in members:
        missing = [f for f in REQUIRED_MEMBER_FIELDS if not m.get(f)]
        name = _member_name_key(m)
        if missing and name:
            gaps.append({"name": name, "missing": missing})
    return gaps


_MEMBER_RETRY_PROMPT = """\
You are re-examining the HOUSEHOLD COMPOSITION section of a HUD/Affordable
Housing certification form (TIC, HUD 50059, RD 3560) to fill member fields
that were missed on the first pass.

For each listed member, find their row on the form (match by name; ignore
middle names and suffixes) and return ONLY the fields listed as missing. If
the request says the form states more members than were extracted, return
every member row on the form so the missing ones can be added.

Field rules:
- DOB: YYYY-MM-DD. "01/15/1990" -> "1990-01-15". If only month and year, day = 01.
- socialSecurityNumber: exactly as printed — full NNN-NN-NNNN, or the masked
  form ***-**-XXXX when the form prints the last four only. NEVER invent digits.
- relationship: exactly as the form states it, Title Case ("Head", "Spouse",
  "Co-Head", "Daughter", "Son", "Granddaughter", "Foster Child", "Other Adult").
  HUD 50059 field 36 codes: H=Head, S=Spouse, K=Co-Head, D=Dependent, F=Foster,
  L=Live-in Aide, O=Other Adult — expand the code.
- A cell that is blank on the form is null. Do not guess, and do not copy
  another member's value.

Return ONLY valid JSON:
{"houseHold": [{"FirstName": "...", "LastName": "...", "DOB": ..., "socialSecurityNumber": ..., "relationship": ...}]}"""


def retry_member_fields(
    relevant_texts: list[str],
    gaps: list[dict],
    shortfall: int,
    certification_type: str | None,
    settings: Settings,
) -> list[dict]:
    """Re-ask the certification form for members' missing required cells.

    `gaps` is the output of required_member_gaps; `shortfall` is how many
    more members the form's own household size states than were extracted
    (0 when unknown or none). Returns normalised member dicts — the caller
    merges them with merge_member_fields. A failed call returns [] so the
    first-pass result is never lost.
    """
    lines = [f"- {g['name'].title()}: missing {', '.join(g['missing'])}" for g in gaps]
    ask = (
        "Members and the fields still missing:\n" + "\n".join(lines)
        if lines else "No known member needs fields filled."
    )
    if shortfall:
        ask += (
            f"\n\nThe form states {shortfall} more household member(s) than were "
            "extracted. Return every member row on the form so the missing "
            "one(s) can be added."
        )
    user_prompt = f"{ask}\n\nDOCUMENT TEXT:\n\n" + "\n\n---\n\n".join(relevant_texts)
    user_prompt += _get_cert_context(certification_type)

    try:
        result = call_llm_json(_MEMBER_RETRY_PROMPT, user_prompt, settings)
    except Exception:
        logger.exception("Member field retry call failed — keeping original values")
        return []

    rows = result.get("houseHold") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    cleaned = validation.validate_household(
        {"houseHold": [r for r in rows if isinstance(r, dict)]}
    )
    cleaned = scrub_extracted_dict(cleaned) or {}
    return [r for r in cleaned.get("houseHold", []) if isinstance(r, dict)]


def merge_member_fields(
    members: list[dict],
    retry_members: list[dict],
    *,
    allow_new: int = 0,
) -> list[str]:
    """Fill null required cells from retry rows, matched by first + last name.

    Populated cells are never overwritten. An unmatched retry row becomes a
    new member only while `allow_new` > 0 and the row carries a DOB or SSN —
    the form's own household size bounds additions, not the model. Returns
    "Name.field" labels of what was filled or "Name (added)".
    """
    recovered: list[str] = []
    exact = {_member_name_key(m): m for m in members}
    loose = {_loose_name_key(m): m for m in members}
    has_head = any(m.get("head") == "H" for m in members)
    for r in retry_members:
        key = _member_name_key(r)
        if not key:
            continue
        target = exact.get(key) or loose.get(_loose_name_key(r))
        if target is not None:
            for f in REQUIRED_MEMBER_FIELDS:
                if not target.get(f) and r.get(f):
                    target[f] = r[f]
                    recovered.append(f"{key.title()}.{f}")
        elif allow_new > 0 and (r.get("DOB") or r.get("socialSecurityNumber")):
            new = {
                k: r.get(k) for k in (
                    "householdMemberNumber", "FirstName", "MiddleName", "LastName",
                    "socialSecurityNumber", "DOB", "relationship", "head",
                )
            }
            if has_head:
                new["head"] = None
            members.append(new)
            exact[key] = new
            loose[_loose_name_key(new)] = new
            allow_new -= 1
            recovered.append(f"{key.title()} (added)")
    return recovered



_VI_AMOUNT_FIELDS = ("rateOfPay", "selfDeclaredAmount", "ytdAmount", "overtimeRate")

# Income types whose amount is a fixed benefit, never a YTD figure (mirrors the
# rule in validation.validate_income). YTD recovered for these is discarded.
_FIXED_INCOME_TYPES = (
    "social security", "supplemental security income",
    "social security disability", "pension", "veterans benefits",
)


def _is_zero_money(value) -> bool:
    try:
        return float(str(value).replace(",", "")) == 0.0
    except (TypeError, ValueError):
        return False


def _vi_has_amount(vi: dict) -> bool:
    """True if a verificationIncome record carries any usable dollar amount."""
    return any(vi.get(f) for f in _VI_AMOUNT_FIELDS) or bool(vi.get("paymentHistory"))


_INCOME_AMOUNT_RETRY_PROMPT = """\
You are re-examining HUD/Affordable Housing income documents to recover the
DOLLAR AMOUNT for income sources that were extracted WITHOUT one on the first pass.

Each target below is an income source already identified in the documents but
missing its amount. For each id, find the income figure in the document text.
If the document does not print an amount for a target, return null for it.

For each target return an object with:
  - id: the id exactly as given (e.g. "V0", "P1")
  - rateOfPay: periodic pay/benefit rate (hourly or monthly amount), numeric string, or null
  - frequencyOfPay: lowercase (weekly / bi-weekly / semi-monthly / monthly / hourly), or null
  - selfDeclaredAmount: amount from a self-cert / application / questionnaire, or null
  - ytdAmount: year-to-date amount ONLY if the document literally says "year to date", or null
  - grossPay: gross pay for a paystub target (P ids only), numeric string, or null

AMOUNT SOURCE BY INCOME TYPE:
- Social Security / SSI / SSDI: monthly benefit in the "How Much You Will Get"
  table → rateOfPay (monthly) + frequencyOfPay="monthly". Do NOT set ytdAmount.
- Pension: monthly or annual pension payment → rateOfPay + frequencyOfPay.
- Child Support / Alimony / Cash Contributions: ordered or received amount →
  selfDeclaredAmount (or rateOfPay + frequencyOfPay if a per-period figure is shown).
- Wages (paystub / VOI / Equifax): grossPay (paystub) or rateOfPay + frequencyOfPay (VOI).

ANTI-HALLUCINATION GUARD (CRITICAL):
- Every amount must be literal text in the document. If you cannot find an amount
  for a target, return null for all its amount fields — do NOT invent a figure.
- US dollar amounts: comma = thousands separator, period = decimal.
  "$1,250" → 1250.00 (NOT 1.25). Always strip commas before parsing.
- An amount under $20 for a monthly benefit or wage is almost always a dropped
  digit — re-read the source.

Return ONLY valid JSON: {"amounts": [{"id": "V0", ...}, ...]}"""


def _retry_income_amounts(
    relevant_texts: list[str],
    amountless_vi: list[dict],
    amountless_ps: list[dict],
    certification_type: str | None,
    settings: Settings,
) -> int:
    """Re-ask the LLM for dollar amounts on income records missing one.

    Mutates the passed-in record dicts in place, filling only amount fields
    that are currently null. Returns the count of records that gained an amount.
    """
    # id → (kind, record). The dicts here are the same objects held inside the
    # result's verificationIncome/payStub lists, so filling them updates result.
    by_id: dict[str, tuple[str, dict]] = {}
    target_lines: list[str] = []
    for i, vi in enumerate(amountless_vi):
        rid = f"V{i}"
        by_id[rid] = ("vi", vi)
        target_lines.append(
            f"  - id={rid} | source: {vi.get('sourceName') or '?'} | "
            f"member: {vi.get('memberName') or '?'} | type: {vi.get('incomeType') or '?'}"
        )
    for i, ps in enumerate(amountless_ps):
        rid = f"P{i}"
        by_id[rid] = ("ps", ps)
        target_lines.append(
            f"  - id={rid} | employer: {ps.get('sourceName') or '?'} | "
            f"employee: {ps.get('memberName') or '?'}"
        )

    user_prompt = (
        "Find the dollar amount for each of these income sources:\n"
        + "\n".join(target_lines)
        + "\n\nDOCUMENT TEXT:\n\n"
        + "\n\n---\n\n".join(relevant_texts)
    )
    user_prompt += _get_cert_context(certification_type)

    try:
        result = call_llm_json(_INCOME_AMOUNT_RETRY_PROMPT, user_prompt, settings)
    except Exception:
        logger.exception("Income amount retry call failed — keeping original records")
        return 0

    amounts = result.get("amounts") if isinstance(result, dict) else None
    if not isinstance(amounts, list):
        return 0

    recovered = 0
    for item in amounts:
        if not isinstance(item, dict):
            continue
        target = by_id.get(item.get("id"))
        if not target:
            continue
        kind, rec = target
        gained = False
        if kind == "vi":
            income_type = (rec.get("incomeType") or "").lower()
            for field in ("rateOfPay", "selfDeclaredAmount", "ytdAmount", "frequencyOfPay"):
                val = item.get(field)
                if val in (None, "", "null") or rec.get(field):
                    continue
                # Fixed benefits never carry a YTD figure (validation rule).
                if field == "ytdAmount" and income_type in _FIXED_INCOME_TYPES:
                    continue
                if field == "frequencyOfPay":
                    rec[field] = str(val).lower()
                else:
                    rec[field] = validation.normalize_money(str(val))
                if field in _VI_AMOUNT_FIELDS and rec.get(field):
                    gained = True
        else:  # paystub
            val = item.get("grossPay")
            if val not in (None, "", "null") and not rec.get("grossPay"):
                rec["grossPay"] = validation.normalize_money(str(val))
                gained = bool(rec.get("grossPay"))
        if gained:
            recovered += 1

    return recovered


# Classified income documents that should each yield an income record of a
# specific type. If the document is present but no record of that type was
# extracted, the source was dropped — re-extract that document specifically.
# Grounded in a real classified document, so recovery re-reads what's actually
# there rather than inventing income to close a dollar gap.
#   doc_type -> (label, acceptable normalized incomeType values, sourceName keywords)

def _asset_record_key(rec: dict) -> tuple:
    """Stable identity key for asset deduplication.

    Combines (sourceName, accountType, accountNumber). Two records that
    share all three are the same asset extracted twice.
    """
    src = (rec.get("sourceName") or "").lower().strip()
    acct_type = (rec.get("accountType") or "").lower().strip()
    acct_num = (rec.get("accountNumber") or "").lower().strip()
    return (src, acct_type, acct_num)


def _dedupe_asset_records(records: list[dict]) -> list[dict]:
    """Drop duplicate asset records, preferring the more complete one.

    Two records are duplicates if (sourceName, accountType, accountNumber)
    match. When merging, the record with more populated fields wins; if
    tied, the first one is kept.
    """
    seen: dict[tuple, dict] = {}
    for rec in records:
        key = _asset_record_key(rec)
        # Records with no identifying signature (all-null key) are kept
        # as-is — can't safely merge them.
        if key == ("", "", ""):
            # Use object id as unique key
            seen[("__nokey__", id(rec), 0)] = rec
            continue

        existing = seen.get(key)
        if existing is None:
            seen[key] = rec
            continue

        # Prefer the record with more non-null fields
        existing_score = sum(1 for v in existing.values() if v not in (None, "", []))
        new_score = sum(1 for v in rec.values() if v not in (None, "", []))
        if new_score > existing_score:
            seen[key] = rec

    return list(seen.values())


# ---------------------------------------------------------------------------
# Per-document extraction with provenance, and reconciliation of what the
# household declares against what the packet verifies
# ---------------------------------------------------------------------------
#
# Income and asset extraction used to be one call per category over every
# document of that category concatenated, with the certification form and
# the questionnaire inside the same context. The model then fused records
# across documents: a questionnaire SSN on a benefit-letter record, a
# questionnaire figure as a source's self-declared amount, and — worst — a
# figure derived from the certification total presented as read from an
# unreadable letter. The retries compounded it: a record-count gate re-read
# self-certification pages alone and added their restatements as new assets,
# and a coverage gate keyed on the classifier's label demanded an "SSI"
# record from a retirement letter and got a $0.00 one.
#
# Now each SOURCE document is extracted in its own call, its records are
# stamped with the pages they came from, and every amount must appear on
# those pages or it is dropped. The household's own statements — the
# certification's income and asset tables, the questionnaire, the
# self-certifications — are read by separate calls into a DECLARED bucket
# and reconciled in code: a declaration that matches a verified record
# annotates it; one that matches nothing becomes a declared-only record and
# a finding; a verified record the certification never declares is flagged
# the other way. Declaration documents never owe a record, and a document
# whose only figures are $0.00 never owes one either.

# Documents that are the household's own account rather than third-party
# evidence. They are read for declarations and never for source records.
_INCOME_DECLARATION_TYPES = frozenset({
    "Application / Housing Questionnaire",
    "Zero Income Certification",
    "Unemployment Affidavit",
    "Child Support / Alimony Affidavit",
})
_ASSET_DECLARATION_TYPES = frozenset({
    "Application / Housing Questionnaire",
    "Asset Self-Certification",
    "Debit Card Asset Self-Certification",
    "No Asset Certification",
    "Disposal of Assets Certification",
})


assert_known(_INCOME_DECLARATION_TYPES, "extractor._INCOME_DECLARATION_TYPES")
assert_known(_ASSET_DECLARATION_TYPES, "extractor._ASSET_DECLARATION_TYPES")


def _is_income_declaration(group: DocumentGroup) -> bool:
    return (is_current_certification_form(group.document_type)
            or group.document_type in _INCOME_DECLARATION_TYPES)


def _is_asset_declaration(group: DocumentGroup) -> bool:
    return (is_current_certification_form(group.document_type)
            or group.document_type in _ASSET_DECLARATION_TYPES)


_PROVENANCE_BLOCK = """

PROVENANCE (REQUIRED):
This request contains ONE document. Its pages are marked "--- Page N ---".
For every record you return, also fill:
  - "sourcePages": the page numbers you read the record from, e.g. [18, 19]
  - "evidence": an object mapping each amount field you filled to the verbatim
    text (at most 40 characters, exactly as printed on that page) that carries
    the figure, e.g. {"rateOfPay": "benefit before any deductions is $1,489.50"}
An amount you cannot quote from this document must be null. Never derive an
amount from another amount, and never carry a figure over from any document
that is not in this request."""

_GROUNDING_BLOCK = """

GROUNDING (CRITICAL):
- Every number must be literal text on this document. Return null rather
  than guess; a null is recoverable, an invented figure is not.
- US dollar amounts: comma = thousands separator, period = decimal.
  "$1,250" is 1250.00, not 1.25. Strip commas before writing the number.
- socialSecurityNumber: only when printed on THIS document, else null."""

_HOUSEHOLD_BLOCK = "\n\nHOUSEHOLD MEMBERS (from the certification form): {names}\nUse these spellings for memberName when the document names one of them."


def _single_document_prompt(group: DocumentGroup, intro: str, certification_type: str | None,
                            household_names: list[str] | None) -> str:
    text = _build_texts([group])
    prompt = intro + "\n\n" + (text[0] if text else group.combined_text)
    if household_names:
        prompt += _HOUSEHOLD_BLOCK.format(names="; ".join(household_names))
    prompt += _get_cert_context(certification_type)
    return prompt


# --- provenance check --------------------------------------------------------

_NUM_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_INCOME_AMOUNT_FIELDS = ("rateOfPay", "selfDeclaredAmount", "ytdAmount", "overtimeRate")
_PAYSTUB_AMOUNT_FIELDS = ("grossPay", "ytdGross")
_ASSET_AMOUNT_FIELDS = ("currentBalance", "averageSixMonthBalance", "selfDeclaredAmount", "incomeAmount")


def _page_number_keys(text: str) -> tuple[set[str], str]:
    """Numeric tokens of a page in comparable forms, plus its digit stream.

    The token set is the strict check ("1489.50" must be a number on the
    page). The digit stream is the lenient one for OCR that splits a figure
    with stray spaces ("1, 489.50"): the value's digits must occur in order.
    """
    plain = strip_html(text or "")
    keys: set[str] = set()
    for tok in _NUM_TOKEN_RE.findall(plain):
        raw = tok.replace(",", "")
        keys.add(raw)
        try:
            keys.add(f"{float(raw):.2f}")
        except ValueError:
            pass
    return keys, re.sub(r"\D", "", plain)


def _value_forms(value: str) -> tuple[set[str], str]:
    raw = str(value).replace("$", "").replace(",", "").strip()
    forms = {raw}
    try:
        f = float(raw)
        forms.add(f"{f:.2f}")
        if f == int(f):
            forms.add(str(int(f)))
    except ValueError:
        pass
    return forms, re.sub(r"\D", "", raw)


def _amount_on_pages(value, page_texts: dict[int, str], pages: list[int]) -> bool:
    forms, digits = _value_forms(value)
    if not digits:
        return False
    for pn in pages:
        keys, stream = _page_number_keys(page_texts.get(pn, ""))
        if forms & keys:
            return True
        if len(digits) >= 3 and digits in stream:
            return True
    return False


def _enforce_provenance(records: list[dict], amount_fields: tuple[str, ...],
                        group: DocumentGroup, label: str) -> int:
    """Stamp records with the group's pages and drop amounts the pages do not carry.

    sourcePages is set from the group, never taken from the model: the
    document was the only thing in the request, so the pages are known.
    An amount absent from every page of the group is set to null and the
    field is recorded in evidence as "not on page", so the record survives
    with an honest gap instead of a figure that came from nowhere.
    Returns the count of amounts dropped.
    """
    page_texts = _group_page_texts(group)
    dropped = 0
    for rec in records:
        rec["sourcePages"] = list(group.pages)
        evidence = rec.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        clean_evidence: dict[str, str] = {}
        for k, v in evidence.items():
            if isinstance(v, str) and v.strip():
                clean_evidence[str(k)] = v.strip()[:80]
            elif isinstance(v, dict) and isinstance(v.get("quote"), str):
                clean_evidence[str(k)] = v["quote"].strip()[:80]
        for field in amount_fields:
            value = rec.get(field)
            if value in (None, "", "null"):
                continue
            if not _amount_on_pages(value, page_texts, group.pages):
                logger.warning(
                    "%s: %s=%s is not on pages %s of '%s' — dropped (no provenance)",
                    label, field, value, group.pages, group.document_type,
                )
                rec[field] = None
                clean_evidence[field] = "not on page"
                dropped += 1
        rec["evidence"] = clean_evidence
    return dropped


# A year-to-date figure is at least the period's gross and at most a
# year's worth of periods of it; pay stubs that print figures without a
# decimal point ("YTD Gross Wages 759768") come through a hundred times
# too large when the reader does not restore the point.
_YTD_MAX_PERIODS = 60


def _name_key(text) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _unify_paystub_sources(stubs: list[dict]) -> int:
    """One member's stubs from one employer carry one employer name.

    A staffing agency's stub prints the agency in the header and the
    customer it placed the person with lower down; a scan reads the header
    on one stub and not the next; OCR spells the agency two ways. Read one
    page at a time, the same job came out as three employers with one stub
    each, and with fewer than three stubs apiece nothing was annualised.
    Within a member, stubs that share a printed employee ID or whose
    employer names are near-identical are one source: they take the name
    most of them carry (a null header yields to any read name), and each
    renamed stub records what its page printed. Returns stubs renamed.
    """
    from difflib import SequenceMatcher

    by_member: dict[str, list[dict]] = {}
    for ps in stubs:
        by_member.setdefault((ps.get("memberName") or "").lower().strip(), []).append(ps)

    changed = 0
    for group in by_member.values():
        if len(group) < 2:
            continue
        parent = list(range(len(group)))

        def _find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def _same_source(a: dict, b: dict) -> str | None:
            ida, idb = _name_key(a.get("employeeId")), _name_key(b.get("employeeId"))
            if ida and idb and ida == idb:
                return "same employee ID"
            na, nb = _name_key(a.get("sourceName")), _name_key(b.get("sourceName"))
            if na and nb and (na == nb or SequenceMatcher(None, na, nb).ratio() >= 0.8):
                return "near-identical employer name"
            return None

        reasons: dict[tuple[int, int], str] = {}
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                why = _same_source(group[i], group[j])
                if why:
                    parent[_find(i)] = _find(j)
                    reasons[(i, j)] = why

        clusters: dict[int, list[int]] = {}
        for i in range(len(group)):
            clusters.setdefault(_find(i), []).append(i)
        for members in clusters.values():
            if len(members) < 2:
                continue
            names = [group[i].get("sourceName") for i in members if (group[i].get("sourceName") or "").strip()]
            if not names:
                continue
            counts: dict[str, int] = {}
            first_seen: dict[str, str] = {}
            for n in names:
                k = _name_key(n)
                counts[k] = counts.get(k, 0) + 1
                first_seen.setdefault(k, n)
            canonical = first_seen[max(counts, key=lambda k: (counts[k], len(k)))]
            for i in members:
                ps = group[i]
                printed = ps.get("sourceName")
                if _name_key(printed) == _name_key(canonical):
                    continue
                why = next((r for (a, b), r in reasons.items() if i in (a, b)), "same source")
                evidence = ps.get("evidence") if isinstance(ps.get("evidence"), dict) else {}
                ps["evidence"] = evidence
                evidence["sourceName"] = (
                    f"page names {printed!r}; one employer with the other stub(s) ({why}) — read as {canonical!r}"
                    if printed else f"employer not legible on this page; {why} with the other stub(s) — read as {canonical!r}"
                )
                ps["sourceName"] = canonical
                changed += 1
                logger.info("Income: pay stub employer %r for %s unified to %r (%s)",
                            printed, ps.get("memberName") or "?", canonical, why)
    return changed


def _repair_paystub_ytd(stubs: list[dict]) -> int:
    """Put a source's pay-stub YTD figures back in sequence.

    Within one member's stubs from one employer, YTD must climb with the
    pay date by about the grosses in between, and no YTD can be less than
    its own gross or more than sixty periods of it. A figure that breaks
    this but fits once divided by a hundred is a lost decimal point and is
    repaired, with the printed value kept in evidence; one that fits
    neither way is dropped rather than shipped. Returns the count changed.
    """
    def _f(v):
        try:
            return float(str(v).replace(",", "")) if v not in (None, "", "null") else None
        except ValueError:
            return None

    groups: dict[tuple[str, str], list[dict]] = {}
    for ps in stubs:
        key = ((ps.get("memberName") or "").lower().strip(), (ps.get("sourceName") or "").lower().strip())
        groups.setdefault(key, []).append(ps)

    changed = 0
    for key, group in groups.items():
        dated = sorted(
            (ps for ps in group if _f(ps.get("ytdGross")) is not None),
            key=lambda ps: (str(ps.get("payDate") or ""), ),
        )
        for ps in dated:
            ytd, gross = _f(ps.get("ytdGross")), _f(ps.get("grossPay"))
            if ytd is None:
                continue
            others = [
                (str(o.get("payDate") or ""), _f(o.get("ytdGross"))) for o in dated if o is not ps and _f(o.get("ytdGross")) is not None
            ]
            date = str(ps.get("payDate") or "")

            def _fits(value: float) -> bool:
                if gross and (value < gross * 0.999 or value > gross * _YTD_MAX_PERIODS):
                    return False
                same_year = [(d, y) for d, y in others if d[:4] == date[:4]] if date else []
                before = [y for d, y in same_year if d < date]
                after = [y for d, y in same_year if d > date]
                if before and value < max(before) * 0.999:
                    return False
                if after and value > min(after) * 1.001:
                    return False
                return True

            if _fits(ytd):
                continue
            evidence = ps.get("evidence") if isinstance(ps.get("evidence"), dict) else {}
            ps["evidence"] = evidence
            printed = ps.get("ytdGross")
            if _fits(ytd / 100):
                ps["ytdGross"] = f"{ytd / 100:.2f}"
                evidence["ytdGross"] = f"printed without a decimal point ({printed}); read as {ps['ytdGross']}"
                logger.info("Income: pay stub YTD %s for %s / %s is a lost decimal — read as %s",
                            printed, key[0] or "?", key[1] or "?", ps["ytdGross"])
            else:
                ps["ytdGross"] = None
                evidence["ytdGross"] = f"printed {printed}, out of sequence with the other stubs — dropped"
                logger.warning("Income: pay stub YTD %s for %s / %s is out of sequence and fits no repair — dropped",
                               printed, key[0] or "?", key[1] or "?")
            changed += 1
    return changed


def _month_on_text(iso: str, text: str) -> bool:
    """Whether a month is printed on the text the ways a payment record
    labels one: 08/26, 08/2026, Aug 2026, August 2026."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-\d{2}", iso.strip())
    if not m:
        return False
    y, mo = m.group(1), int(m.group(2))
    name = _MONTH_NAMES[mo - 1]
    low = text.lower()
    patterns = (
        rf"(?<![\d/.-]){mo:02d}\s*/\s*(?:{y}|{y[2:]})(?![\d/])",
        rf"(?<![\d/.-]){mo}\s*/\s*(?:{y}|{y[2:]})(?![\d/])",
        rf"(?:{name}|{name[:3]}\.?)\s*,?\s*{y}\b",
    )
    return any(re.search(rx, low) for rx in patterns)


def _prune_payment_history(records: list[dict], group: DocumentGroup, label: str) -> int:
    """Keep a payment history to what the document prints.

    A ledger row is an amount like any other: one the pages do not carry
    came from nowhere and must not be annualised, so the row is dropped.
    A row's date is held to the same standard: a worksheet that numbers
    its payments prints no dates, and dates the model supplied for it ran
    from January 2026 to March 2030 and were then windowed as "the twelve
    most recent". A date the pages do not print, or one after today, is
    cleared and the row kept undated. Returns rows dropped.
    """
    from datetime import date as _date
    page_texts = _group_page_texts(group)
    joined = "\n".join(page_texts.get(pn) or "" for pn in group.pages)
    today = _date.today().isoformat()
    dropped = 0
    for rec in records:
        rows = rec.get("paymentHistory")
        if not isinstance(rows, list) or not rows:
            continue
        kept = []
        undated = 0
        for row in rows:
            amount = row.get("amount") if isinstance(row, dict) else None
            if amount in (None, "", "null"):
                continue
            if not _amount_on_pages(amount, page_texts, group.pages):
                dropped += 1
                continue
            raw_date = row.get("date")
            if raw_date not in (None, "", "null"):
                iso = str(raw_date).strip()
                printed = _date_on_text(iso, joined) or (
                    iso.endswith("-01") and _month_on_text(iso, joined)
                )
                if not printed or iso > today:
                    row = dict(row)
                    row["date"] = None
                    undated += 1
            kept.append(row)
        notes = []
        if dropped:
            logger.warning(
                "%s: %d payment-history row(s) not on pages %s of '%s' — dropped (no provenance)",
                label, dropped, group.pages, group.document_type,
            )
            notes.append(f"{dropped} row(s) not on page")
        if undated:
            logger.warning(
                "%s: %d payment-history date(s) not printed on pages %s of '%s' — rows kept undated",
                label, undated, group.pages, group.document_type,
            )
            notes.append(f"{undated} row date(s) not printed on the page — rows kept undated")
        if notes:
            evidence = rec.get("evidence")
            if isinstance(evidence, dict):
                evidence["paymentHistory"] = "; ".join(notes)
        rec["paymentHistory"] = kept
    return dropped


_REDACTION_RE = re.compile(
    r"\[(?:blank\s*-\s*)?blacked[\s-]*out\]|\[redacted\]|\bredacted\b|█{2,}|\[blank\s*-\s*(?:hidden|masked)\]",
    re.IGNORECASE,
)


def _note_redactions(records: list[dict], amount_fields: tuple[str, ...], group: DocumentGroup) -> None:
    """Mark amounts missing from a page that carries redacted cells.

    A bank's verification with the balance blacked out is not a value the
    extractor missed; the scorer reads this note and scores the field as
    not available rather than not extracted. Only the fields the document
    itself would carry are noted — a declaration's fields (selfDeclaredAmount)
    are never on a bank's page.
    """
    if not _REDACTION_RE.search(group.combined_text or ""):
        return
    for rec in records:
        evidence = rec.get("evidence")
        if not isinstance(evidence, dict):
            evidence = rec["evidence"] = {}
        for field in amount_fields:
            if rec.get(field) in (None, "", "null") and not evidence.get(field):
                evidence[field] = "redacted on page"


_PAGE_MARK_RE = re.compile(r"--- Page (\d+) ---\n?")


def _group_page_texts(group: DocumentGroup) -> dict[int, str]:
    """Split a group's combined text back into per-page text."""
    parts = _PAGE_MARK_RE.split(group.combined_text or "")
    texts: dict[int, str] = {}
    # parts = [prefix, num, text, num, text, ...]
    for i in range(1, len(parts) - 1, 2):
        try:
            texts[int(parts[i])] = parts[i + 1]
        except ValueError:
            continue
    if not texts and group.pages:
        texts[group.pages[0]] = group.combined_text or ""
    return texts


# --- what a document owes ---------------------------------------------------

_DOLLAR_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)(?!\s*%)")
_CENTS_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+\.\d{2}|\d{2,}\.\d{2})(?!\s*%)")


def _document_amounts(group: DocumentGroup) -> set[float]:
    """Dollar figures of at least $1 printed on the document.

    A document with none owes no record; a document stating only $0.00 for
    a program owes none either (the letter that says the SSI payment is
    $0.00 is not evidence of SSI income).
    """
    plain = strip_html(group.combined_text or "")
    found: set[float] = set()
    for rx in (_DOLLAR_RE, _CENTS_RE):
        for m in rx.finditer(plain):
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if v >= 1.0:
                found.add(round(v, 2))
    return found


def _run_per_group(groups: list[DocumentGroup], fn, settings: Settings, label: str) -> list:
    """Run fn(group) for every group, a few at a time.

    A document whose read fails fails the category, and with it the case,
    as ExtractionUnavailableError (retryable). This used to log and skip
    the document, which delivered the audit without it: on 05318 a
    bank-statement read that came back as invalid JSON dropped both
    checking accounts and the declared lines became "Other" claims.
    """
    if not groups:
        return []
    from concurrent.futures import ThreadPoolExecutor
    from app.core.exceptions import ExtractionUnavailableError
    results: list = []
    workers = max(1, min(4, len(groups), getattr(settings, "ocr_concurrency", 4) or 4))

    def _safe(g):
        try:
            return fn(g)
        except ExtractionUnavailableError as exc:
            logger.error("%s: extraction failed for '%s' pages %s — %s",
                         label, g.document_type, g.pages, exc)
            raise ExtractionUnavailableError(
                f"{label}: '{g.document_type}' pages {g.pages} could not be read — {exc}"
            ) from exc
        except Exception as exc:
            logger.exception("%s: extraction failed for '%s' pages %s",
                             label, g.document_type, g.pages)
            raise ExtractionUnavailableError(
                f"{label}: '{g.document_type}' pages {g.pages} failed — {type(exc).__name__}: {exc}"
            ) from exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for out in pool.map(_safe, groups):
            if out is not None:
                results.append(out)
    return results


# --- declared income ----------------------------------------------------------

DECLARED_INCOME_PROMPT = """\
You are reading the household's OWN statements of its income in a HUD /
Affordable Housing certification packet: the certification form's income
table (LIHTC TIC Part III, HUD 50059 income section fields 79-86, RD 3560-8
income lines) and the application or recertification questionnaire. These
are DECLARATIONS by the household or the manager, not third-party
verifications.

Return every income line declared, one entry per row or disclosure:
{"declared": [{"memberName": ..., "memberNumber": ..., "sourceName": ...,
  "incomeType": ..., "amount": ..., "amountPeriod": ..., "page": N,
  "quote": "..."}]}

Rules:
- memberName: the household member the row belongs to. Certification tables
  key rows by member number; resolve it against the household composition
  on the same form and give the name as printed there.
- sourceName: the employer / agency / payer as the row prints it ("SSA",
  "Soc. Sec.", "Durango Lodge"), or null when the row shows only a type.
- incomeType: one of Non-Federal Wage, Federal Wage, Social Security,
  Supplemental Security Income, Social Security Disability, Pension, Veterans Benefits,
  Temporary Assistance, Child Support, Self-Employment, Zero Income,
  Other Income.
- amount: exactly as printed, numeric string, no $ or commas.
- amountPeriod: the period the AMOUNT ITSELF covers — "annual" for an
  Annual Income column or a yearly salary, "monthly" for "$X per month",
  "weekly", "bi-weekly", "per_period". A pay-frequency checkbox or "Hours per
  Week" beside a "Salary / Rate of Pay" field says how often the person is
  paid, NOT what period the printed amount covers; when the amount's own
  period is not printed, use "unknown".
- page: the "--- Page N ---" the row is on. quote: at most 40 characters of
  verbatim text from that row containing the amount.
- NEVER return total rows, subtotals, income limits, or historical figures
  ("at move-in", "prior", "previous certification").
- A declaration of no income ("no income", "$0", zero-income certification)
  is one entry with incomeType "Zero Income" and amount "0.00".
- Do not invent a row the form does not print. Return {"declared": []} when
  the pages declare nothing.

Return ONLY valid JSON."""


def _extract_declared_income(groups: list[DocumentGroup], settings: Settings,
                             certification_type: str | None,
                             household_names: list[str] | None) -> list[dict]:
    if not groups:
        return []
    texts = _build_texts(groups)
    if not texts:
        return []
    prompt = "Read the household's declared income from these documents:\n\n" + "\n\n---\n\n".join(texts)
    if household_names:
        prompt += _HOUSEHOLD_BLOCK.format(names="; ".join(household_names))
    prompt += _get_cert_context(certification_type)
    try:
        result = call_llm_json(DECLARED_INCOME_PROMPT, prompt, settings)
    except Exception:
        logger.exception("Declared income: call failed — treating as no declarations")
        return []
    rows = result.get("declared") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    page_texts: dict[int, str] = {}
    doc_of_page: dict[int, str] = {}
    for g in groups:
        page_texts.update(_group_page_texts(g))
        for pn in g.pages:
            doc_of_page[pn] = g.document_type
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        amount = validation.normalize_money(str(r.get("amount"))) if r.get("amount") not in (None, "", "null") else None
        try:
            page = int(r.get("page")) if r.get("page") not in (None, "", "null") else None
        except (TypeError, ValueError):
            page = None
        pages = [page] if page in page_texts else list(page_texts)
        if amount is not None and float(amount) > 0 and not _amount_on_pages(amount, page_texts, pages):
            logger.warning("Declared income: %s on page %s is not printed there — dropped", amount, page)
            continue
        out.append({
            "memberName": validation.to_title_case(r.get("memberName")) if r.get("memberName") else None,
            "memberNumber": str(r.get("memberNumber")) if r.get("memberNumber") not in (None, "") else None,
            "sourceName": r.get("sourceName") or None,
            "incomeType": r.get("incomeType") or None,
            "amount": amount,
            "amountPeriod": (r.get("amountPeriod") or "unknown").lower(),
            "page": page,
            "quote": (str(r.get("quote"))[:80] if r.get("quote") else None),
            "documentType": doc_of_page.get(page) if page else (groups[0].document_type if len(groups) == 1 else None),
            "matched": False,
        })
    return out


_PERIOD_TO_FREQUENCY = {
    "annual": "annually", "annually": "annually", "yearly": "annually",
    "monthly": "monthly", "weekly": "weekly", "bi-weekly": "bi-weekly",
    "biweekly": "bi-weekly", "semi-monthly": "semi-monthly",
}
_PERIOD_MULTIPLIER = {"annually": 1, "monthly": 12, "weekly": 52, "bi-weekly": 26, "semi-monthly": 24}


def _annual_of(amount, period: str | None) -> float | None:
    try:
        v = float(str(amount).replace(",", ""))
    except (TypeError, ValueError):
        return None
    freq = _PERIOD_TO_FREQUENCY.get((period or "").lower())
    mult = _PERIOD_MULTIPLIER.get(freq or "")
    return round(v * mult, 2) if mult else None


def _name_last(name: str | None) -> str:
    parts = (name or "").strip().lower().split()
    return parts[-1] if parts else ""


def _same_member(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    la, lb = _name_last(a), _name_last(b)
    if not la or la != lb:
        return False
    fa, fb = a.strip().lower().split()[0], b.strip().lower().split()[0]
    return fa == fb or fa.startswith(fb) or fb.startswith(fa)


_TYPE_SYNONYMS = {
    "social security": {"social security", "ssa", "soc. sec.", "soc sec", "ss", "retirement"},
    "supplemental security income": {"supplemental security income", "ssi"},
    "social security disability": {"social security disability", "ssdi", "disability"},
    "child support": {"child support"},
    "pension": {"pension", "retirement"},
    "non-federal wage": {"wage", "wages", "employment", "employer", "salary"},
    "federal wage": {"wage", "federal wage"},
    "temporary assistance": {"tanf", "temporary assistance", "public assistance", "cash aid"},
    "self-employment": {"self-employment", "self employment", "business"},
}
_STOPWORDS = {"of", "the", "and", "inc", "llc", "co", "corp", "administration", "department", "services", "office"}


def _type_key(income_type: str | None) -> str:
    return (income_type or "").strip().lower()


def _same_income_type(a: str | None, b: str | None) -> bool:
    ka, kb = _type_key(a), _type_key(b)
    if ka and ka == kb:
        return True
    sa = _TYPE_SYNONYMS.get(ka, {ka} if ka else set())
    sb = _TYPE_SYNONYMS.get(kb, {kb} if kb else set())
    return bool(sa & sb)


def _source_words(name: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (name or "").lower()) if w not in _STOPWORDS}


def _source_overlap(a: str | None, b: str | None) -> bool:
    wa, wb = _source_words(a), _source_words(b)
    if wa and wb and (wa & wb):
        return True
    # the declared source is often a type word ("SSA", "Soc. Sec.")
    return _same_income_type(a, b)


source_names_overlap = _source_overlap


def _record_annual(vi: dict) -> float | None:
    from app.services.income_calculator import annualize_history, get_frequency_multiplier
    if vi.get("paymentHistory"):
        annual = annualize_history(vi["paymentHistory"])
        if annual is not None:
            return round(annual, 2)
    for field in ("rateOfPay", "selfDeclaredAmount"):
        val = vi.get(field)
        if not val:
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        mult = get_frequency_multiplier(vi.get("frequencyOfPay") or "") if vi.get("frequencyOfPay") else None
        hours = vi.get("hoursPerPayPeriod")
        if mult and hours and field == "rateOfPay":
            try:
                return round(v * float(hours) * mult, 2)
            except ValueError:
                pass
        if mult:
            return round(v * mult, 2)
    return None


def _close(a: float | None, b: float | None, rel: float = 0.02) -> bool:
    return a is not None and b is not None and abs(a - b) <= max(1.0, abs(b) * rel)


_DECLARED_ANNUAL_CEILING = 400_000.0


def _paystub_sources(ps_entries: list[dict]) -> list[dict]:
    """Employment the paystubs verify, one pseudo-record per (member, employer)."""
    out: dict[tuple[str, str], dict] = {}
    for ps in ps_entries:
        member = ps.get("memberName") or ""
        employer = ps.get("sourceName") or ""
        if not (member and employer):
            continue
        key = (member.lower(), employer.lower())
        rec = out.setdefault(key, {"memberName": member, "sourceName": employer, "pages": set(), "stubs": 0})
        rec["pages"].update(ps.get("sourcePages") or [])
        rec["stubs"] += 1
    return list(out.values())


def _declaration_read_is_complete(declared: list[dict], declared_total) -> bool:
    """True when the declared lines add up to the certification's own total.

    The declared bucket comes from one model call. When that call under-reads
    the income table, every verified source it missed would be reported as
    "not declared" — a false finding per source. The form's total is the
    check on the read: within 10% of it, the list is trusted; otherwise the
    not-declared verdict is withheld and the gap is logged.
    """
    try:
        total = float(str(declared_total).replace(",", "")) if declared_total not in (None, "") else None
    except ValueError:
        total = None
    if not total or total <= 0:
        return False
    annual = [_annual_of(d.get("amount"), d.get("amountPeriod")) for d in declared if d.get("incomeType") != "Zero Income"]
    known = [a for a in annual if a is not None]
    if not known or len(known) < len(annual):
        return False
    return abs(sum(known) - total) <= max(50.0, total * 0.10)


def _drop_total_rows(declared: list[dict], label: str, reference_total=None) -> list[dict]:
    """Remove declared lines that are the sum of other lines on the same page.

    A certification's asset or income table ends in a total, and a
    questionnaire restates one; the model sometimes returns that row as a
    line. Reconciled as a line it becomes a second record carrying the
    whole table again ("Other 6,376.79" beside the accounts that sum to
    it). A line equal to the sum of two or more other lines of its page is
    the table's total, not a claim.
    """
    def _amt(d):
        try:
            return round(float(str(d.get("amount")).replace(",", "")), 2)
        except (TypeError, ValueError):
            return None

    def _sums_to(value, others) -> bool:
        others = [o for o in others if o]
        if not value or len(others) < 2:
            return False
        # Any subset of two or more other lines summing to this one.
        from itertools import combinations
        for k in range(2, min(len(others), 6) + 1):
            for c in combinations(others, k):
                total = round(sum(c), 2)
                # to the cent, or one misread digit apart (6,376.19 read
                # where the page prints 6,376.79)
                if abs(total - value) <= 0.011 or _one_digit_apart(f"{total:.2f}", f"{value:.2f}"):
                    return True
        return False

    def _untyped(d) -> bool:
        t = (d.get("incomeType") or d.get("accountType") or "").strip().lower()
        return t in ("", "other", "other income", "total", "household", "household income", "all sources")

    try:
        ref_total = float(str(reference_total).replace(",", "")) if reference_total not in (None, "", "null") else None
    except ValueError:
        ref_total = None

    kept: list[dict] = []
    for d in declared:
        value = _amt(d)
        is_total = _sums_to(value, [_amt(o) for o in declared if o is not d and o.get("page") == d.get("page")])
        # A questionnaire's "total household income" restates lines that
        # sit on other pages (the certification's). An untyped line equal
        # to the sum of lines anywhere in the declarations is that total.
        if not is_total and _untyped(d):
            is_total = _sums_to(value, [_amt(o) for o in declared if o is not d])
        # The sum test needs every component line to have been read. When
        # one was not, the certification's own total still identifies the
        # row: an untyped line within a few percent of it is the household
        # total, not a member's income (05318: 39,326.90 against 39,819.16).
        if not is_total and _untyped(d) and value and ref_total and ref_total > 0:
            is_total = abs(value - ref_total) / ref_total <= 0.03
        if is_total:
            logger.info("%s: declared line %s on page %s is the sum of other lines — a total row, not a claim",
                        label, d.get("amount"), d.get("page"))
            d["matched"] = True
            continue
        kept.append(d)
    return kept


# Income the household receives as a unit, which the certification lists
# under whichever member the preparer chose (the custodial parent, the head)
# while the verifying statement names the payee. A declared line of such a
# type that matches no record by member still describes the household's
# one verified record of that type.
_HOUSEHOLD_LEVEL_INCOME_TYPES = ("child support", "alimony", "temporary assistance", "tanf",
                                 "public assistance", "general assistance")


def _reconcile_income(vi_entries: list[dict], declared: list[dict], certification_type: str | None,
                      ps_entries: list[dict] | None = None, declared_total=None) -> None:
    """Annotate verified records with what the household declared for them;
    keep unmatched declarations as declared-only records; mark verified
    records the certification never declares.

    Matching is member first (last name, first-name prefix), then any of:
    the same income type, a shared source word, or an annual figure within
    2%. A declared amount goes to selfDeclaredAmount only when the record
    has none and the period is compatible with the record's frequency;
    otherwise it is kept on declaredAnnualAmount so the calculator never
    multiplies an annual figure by twelve.
    """
    for vi in vi_entries:
        if vi.get("verificationStatus") is None and (_vi_has_amount(vi) or vi.get("sourceName")):
            vi["verificationStatus"] = "verified"

    stub_sources = _paystub_sources(ps_entries or [])

    for d in _drop_total_rows(declared, "Declared income", declared_total):
        if d.get("incomeType") == "Zero Income" and (d.get("amount") in (None, "0.00")):
            d["matched"] = True   # informational; nothing to reconcile
            continue
        annual_d = _annual_of(d.get("amount"), d.get("amountPeriod"))
        if annual_d is not None and annual_d > _DECLARED_ANNUAL_CEILING:
            # A salary multiplied by a pay-frequency checkbox: the period the
            # model attached is not the period the amount covers.
            logger.info("Declared income: %s × %s = %.2f is not a plausible annual figure — period treated as unknown",
                        d.get("amount"), d.get("amountPeriod"), annual_d)
            d["amountPeriod"] = "unknown"
            annual_d = None
        candidates = [
            vi for vi in vi_entries
            if vi.get("verificationStatus") != "declared_only"
            and _same_member(vi.get("memberName"), d.get("memberName"))
            and (
                _same_income_type(vi.get("incomeType"), d.get("incomeType"))
                or _source_overlap(vi.get("sourceName"), d.get("sourceName"))
                or _close(_record_annual(vi), annual_d)
            )
        ]
        if not candidates and (d.get("incomeType") or "").lower() in ("", "other", "other income"):
            # A questionnaire's "other income" line for a member is the
            # member's income the form had no box for. When that member has
            # exactly one verified record no declaration has claimed, this
            # line is about it — the $2,867.30 "other income" beside Arnold's
            # name is his child support, whichever type the read attached.
            unclaimed = [
                vi for vi in vi_entries
                if vi.get("verificationStatus") not in ("declared_only", "self_certified")
                and _same_member(vi.get("memberName"), d.get("memberName"))
                and not vi.get("declaredAnnualAmount") and not vi.get("selfDeclaredSource")
            ]
            if len(unclaimed) == 1:
                logger.info(
                    "Declared income: '%s' %s for %s matches the member's one unclaimed verified record (%s)",
                    d.get("incomeType") or "untyped", d.get("amount"), d.get("memberName"), unclaimed[0].get("sourceName"),
                )
                candidates = unclaimed
            elif not unclaimed and annual_d:
                # Every verified record of this member is already declared
                # elsewhere (the certification claimed them). An untyped
                # line on a second document is then a second declaration of
                # one of them, not a new income: attach it to the record
                # whose figure is nearest, where it is reported alongside
                # the certification's, rather than minting an "other
                # income" record nothing in the packet backs.
                # The member's own records, plus the household-level incomes
                # (child support, assistance) whichever member they sit
                # under — a questionnaire lists those under whoever filled
                # it in.
                mine = [
                    vi for vi in vi_entries
                    if vi.get("verificationStatus") not in ("declared_only", "self_certified")
                    and (_same_member(vi.get("memberName"), d.get("memberName"))
                         or (vi.get("incomeType") or "").lower() in _HOUSEHOLD_LEVEL_INCOME_TYPES)
                ]
                def _gap(vi):
                    ref = _record_annual(vi)
                    try:
                        ref = float(vi.get("declaredAnnualAmount")) if vi.get("declaredAnnualAmount") else ref
                    except ValueError:
                        pass
                    return abs((ref or 0.0) - annual_d) / max(annual_d, ref or 0.0, 1.0)
                if mine:
                    nearest = min(mine, key=_gap)
                    if _gap(nearest) <= 0.5:
                        evidence = nearest.get("evidence") if isinstance(nearest.get("evidence"), dict) else {}
                        nearest["evidence"] = evidence
                        evidence.setdefault("alsoDeclared", f"{d.get('documentType')}: {d.get('amount')} ({d.get('incomeType') or 'untyped'})")
                        d["matched"] = True
                        logger.info(
                            "Declared income: '%s' %s for %s on the %s is a second declaration of %s — noted, not a record",
                            d.get("incomeType") or "untyped", d.get("amount"), d.get("memberName"),
                            d.get("documentType"), nearest.get("sourceName"),
                        )
                        continue
        if not candidates and (d.get("incomeType") or "").lower() in _HOUSEHOLD_LEVEL_INCOME_TYPES:
            same_type = [
                vi for vi in vi_entries
                if vi.get("verificationStatus") != "declared_only"
                and _same_income_type(vi.get("incomeType"), d.get("incomeType"))
            ]
            declared_member_has_one = any(
                _same_member(vi.get("memberName"), d.get("memberName")) for vi in same_type
            )
            if len(same_type) == 1 and not declared_member_has_one:
                logger.info(
                    "Declared income: %s listed under %s matches the household's one verified %s "
                    "record, which names %s — treated as the same income",
                    d.get("amount"), d.get("memberName"), d.get("incomeType"), same_type[0].get("memberName"),
                )
                candidates = same_type
        if candidates:
            # prefer the candidate whose annual figure agrees, then the first
            candidates.sort(key=lambda vi: 0 if _close(_record_annual(vi), annual_d) else 1)
            vi = candidates[0]
            d["matched"] = True
            if annual_d is not None and not vi.get("declaredAnnualAmount"):
                vi["declaredAnnualAmount"] = f"{annual_d:.2f}"
                vi["declaredSource"] = d.get("documentType")
            period_freq = _PERIOD_TO_FREQUENCY.get((d.get("amountPeriod") or "").lower())
            if not vi.get("selfDeclaredAmount") and d.get("amount") and (
                not vi.get("frequencyOfPay") or period_freq in (None, vi.get("frequencyOfPay"))
            ):
                vi["selfDeclaredAmount"] = d["amount"]
                vi["selfDeclaredSource"] = d.get("documentType")
                if period_freq and not vi.get("frequencyOfPay"):
                    vi["frequencyOfPay"] = period_freq
            continue
        # Wages verified by paystubs alone have no verificationIncome record
        # to match; the paystub employer is the verification. Create the
        # employment record here so the paystubs attach to it downstream
        # instead of being reconstructed as an amountless orphan source.
        wage_like = _same_income_type(d.get("incomeType"), "Non-Federal Wage") or not d.get("incomeType")
        stub_match = next(
            (src for src in stub_sources
             if _same_member(src["memberName"], d.get("memberName"))
             and (not d.get("sourceName") or _source_overlap(src["sourceName"], d.get("sourceName")) or wage_like)),
            None,
        ) if wage_like or d.get("sourceName") else None
        if stub_match is not None:
            d["matched"] = True
            period_freq = _PERIOD_TO_FREQUENCY.get((d.get("amountPeriod") or "").lower())
            vi_entries.append({
                "sourceName": stub_match["sourceName"],
                "memberName": stub_match["memberName"],
                "incomeType": d.get("incomeType") or "Non-Federal Wage",
                "selfDeclaredAmount": d.get("amount"),
                "selfDeclaredSource": d.get("documentType"),
                "frequencyOfPay": period_freq,
                "type_of_VOI": "Self-Declaration",
                "sourcePages": sorted(stub_match["pages"]),
                "evidence": {"selfDeclaredAmount": d["quote"]} if d.get("quote") else {},
                "verificationStatus": "verified",
                "declaredAnnualAmount": f"{annual_d:.2f}" if annual_d is not None else None,
                "declaredSource": d.get("documentType"),
            })
            stub_sources.remove(stub_match)
            continue
        # Nothing in the packet verifies this declaration: keep it as the
        # household's own statement, flagged as such.
        period_freq = _PERIOD_TO_FREQUENCY.get((d.get("amountPeriod") or "").lower())
        vi_entries.append({
            "sourceName": d.get("sourceName") or (f"{d.get('incomeType')} (declared)" if d.get("incomeType") else "Self-Declaration"),
            "memberName": d.get("memberName"),
            "incomeType": d.get("incomeType"),
            "selfDeclaredAmount": d.get("amount"),
            "selfDeclaredSource": d.get("documentType"),
            "frequencyOfPay": period_freq,
            "type_of_VOI": "Self-Declaration",
            "sourcePages": [d["page"]] if d.get("page") else [],
            "evidence": {"selfDeclaredAmount": d["quote"]} if d.get("quote") else {},
            # On a self-certification the household's statement is the
            # verification by design; elsewhere it is a declaration nobody
            # has verified.
            "verificationStatus": "self_certified" if certification_type == "AR-SC" else "declared_only",
            "declaredAnnualAmount": f"{annual_d:.2f}" if annual_d is not None else None,
            "declaredSource": d.get("documentType"),
        })
        d["matched"] = True

    declared_real = [d for d in declared if d.get("incomeType") != "Zero Income"]
    if declared_real and not _declaration_read_is_complete(declared, declared_total):
        logger.info(
            "Declared income: %d line(s) do not add up to the certification total %s — "
            "verified sources are not marked as undeclared", len(declared_real), declared_total,
        )
        declared_real = []
    if declared_real:
        for vi in vi_entries:
            if vi.get("verificationStatus") == "verified" and not vi.get("declaredAnnualAmount") and _vi_has_amount(vi):
                # matched declarations annotate declaredAnnualAmount or
                # selfDeclaredSource; a verified record with neither was
                # never declared.
                if not vi.get("selfDeclaredSource"):
                    vi["verificationStatus"] = "verified_not_declared"


# --- declared assets ------------------------------------------------------------

DECLARED_ASSET_PROMPT = """\
You are reading the household's OWN statements about its assets in a HUD /
Affordable Housing certification packet: the certification form's asset
table (LIHTC TIC Part IV / V, HUD 50059 Section D fields 76-80, RD 3560-8
asset lines), asset self-certifications, no-asset certifications, disposal
of assets certifications, and application or questionnaire disclosures.
These are DECLARATIONS, not statements from a bank or a verifier.

Return every asset declared, one entry per row or line:
{"declared": [{"assetOwner": ..., "accountType": ..., "sourceName": ...,
  "accountNumber": ..., "amount": ..., "incomeAmount": ..., "kind": ...,
  "page": N, "quote": "..."}]}

Rules:
- assetOwner: the household member, as the household composition on the
  form prints the name; resolve member numbers.
- accountType: Checking, Savings, Cash, Prepaid Card, Direct Express, CD,
  Investment, Retirement, Life Insurance, Real Estate, Annuity, ABLE Account,
  Cryptocurrency, Other.
- sourceName: the institution or, for real estate, the property address, as
  printed; null when the row shows only a type.
- accountNumber: as printed (last four is fine); null when absent.
- amount: the cash value / balance exactly as printed, numeric string, no $
  or commas. incomeAmount: the yearly income from the asset if printed.
- kind: "asset" for a declared holding, "no_assets" for a certification that
  the household holds no assets, "disposal" for an asset disposed of.
- page / quote: the "--- Page N ---" the line is on and at most 40
  characters of verbatim text from that line containing the amount.
- NEVER return totals, imputed-income lines, passbook rates, or thresholds
  ("$5,000", "$50,000") printed as form text.
- Do not invent a row. Return {"declared": []} when nothing is declared.

Return ONLY valid JSON."""


def _extract_declared_assets(groups: list[DocumentGroup], settings: Settings,
                             certification_type: str | None,
                             household_names: list[str] | None) -> list[dict]:
    if not groups:
        return []
    texts = _build_texts(groups)
    if not texts:
        return []
    prompt = "Read the household's declared assets from these documents:\n\n" + "\n\n---\n\n".join(texts)
    if household_names:
        prompt += _HOUSEHOLD_BLOCK.format(names="; ".join(household_names))
    prompt += _get_cert_context(certification_type)
    try:
        result = call_llm_json(DECLARED_ASSET_PROMPT, prompt, settings)
    except Exception:
        logger.exception("Declared assets: call failed — treating as no declarations")
        return []
    rows = result.get("declared") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    page_texts: dict[int, str] = {}
    doc_of_page: dict[int, str] = {}
    for g in groups:
        page_texts.update(_group_page_texts(g))
        for pn in g.pages:
            doc_of_page[pn] = g.document_type
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        amount = validation.normalize_money(str(r.get("amount"))) if r.get("amount") not in (None, "", "null") else None
        income = validation.normalize_money(str(r.get("incomeAmount"))) if r.get("incomeAmount") not in (None, "", "null") else None
        try:
            page = int(r.get("page")) if r.get("page") not in (None, "", "null") else None
        except (TypeError, ValueError):
            page = None
        pages = [page] if page in page_texts else list(page_texts)
        if amount is not None and float(amount) > 0 and not _amount_on_pages(amount, page_texts, pages):
            logger.warning("Declared asset: %s on page %s is not printed there — dropped", amount, page)
            continue
        out.append({
            "assetOwner": validation.to_title_case(r.get("assetOwner")) if r.get("assetOwner") else None,
            "accountType": r.get("accountType") or None,
            "sourceName": r.get("sourceName") or None,
            "accountNumber": str(r.get("accountNumber")) if r.get("accountNumber") not in (None, "", "null") else None,
            "amount": amount,
            "incomeAmount": income,
            "kind": (r.get("kind") or "asset").lower(),
            "page": page,
            "quote": (str(r.get("quote"))[:80] if r.get("quote") else None),
            "documentType": doc_of_page.get(page) if page else (groups[0].document_type if len(groups) == 1 else None),
            "matched": False,
        })
    return out


_ASSET_FAMILIES = {
    "cash": {"checking", "savings", "cash", "prepaid card", "direct express", "debit card", "money market", "cd", "certificate of deposit"},
    "real estate": {"real estate", "property", "home"},
    "investment": {"investment", "retirement", "annuity", "able account", "cryptocurrency", "peer-to-peer", "brokerage", "401k", "ira"},
    "life insurance": {"life insurance"},
}


# Finer than the family: the account kinds a certification lists one line
# each for. Two checking accounts are two kinds-of-the-same; a checking and
# a savings are different kinds even though both are "cash".
_ASSET_KINDS = (
    ("certificates of deposit", "cd"), ("certificate of deposit", "cd"), ("money market", "money market"),
    ("checking", "checking"), ("savings", "savings"), ("prepaid card", "prepaid card"),
    ("direct express", "direct express"), ("debit card", "debit card"), ("cash", "cash"),
    ("real estate", "real estate"), ("property", "real estate"), ("home", "real estate"),
    ("life insurance", "life insurance"), ("retirement", "retirement"), ("401k", "retirement"),
    ("ira", "retirement"), ("annuity", "annuity"), ("able account", "able account"),
    ("cryptocurrency", "cryptocurrency"), ("brokerage", "investment"), ("investment", "investment"),
)


def _asset_kind(account_type: str | None) -> str:
    t = (account_type or "").strip().lower()
    for name, kind in _ASSET_KINDS:
        if name in t:
            return kind
    if re.search(r"\bcds?\b", t):
        return "cd"
    return "other"


def _asset_family(account_type: str | None) -> str:
    t = (account_type or "").strip().lower()
    for fam, names in _ASSET_FAMILIES.items():
        if any(n in t for n in names):
            return fam
    return t or "other"


def _digits_last4(value: str | None) -> str | None:
    d = re.sub(r"\D", "", value or "")
    return d[-4:] if len(d) >= 4 else None


def _one_digit_apart(a: str, b: str) -> bool:
    """Same length, same leading digit, exactly one differing character — an
    OCR digit slip (6,294.34 against 6,294.74). The leading digit must agree:
    "20.00" and "50.00" are one character apart and are two different assets."""
    return len(a) == len(b) and a[0] == b[0] and sum(1 for x, y in zip(a, b) if x != y) == 1


def _amounts_close(a, b) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if abs(fa - fb) <= max(0.02, abs(fb) * 0.01):
        return True
    return _one_digit_apart(f"{fa:.2f}", f"{fb:.2f}")


def _amt_or_none(value) -> float | None:
    try:
        return float(str(value).replace(",", "")) if value not in (None, "", "null") else None
    except ValueError:
        return None


def _amounts_equal(a, b) -> bool:
    try:
        return abs(float(str(a).replace(",", "")) - float(str(b).replace(",", ""))) < 0.005
    except (TypeError, ValueError):
        return False


def _distinctive_amount(value) -> bool:
    """A figure unlikely to be equal by coincidence: non-zero, and either
    carrying cents or not a round multiple of fifty."""
    try:
        v = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return False
    if v <= 0:
        return False
    return round(v % 1, 2) not in (0.0, 1.0) or v % 50 != 0


def _reconcile_assets(records: list[dict], declared: list[dict]) -> None:
    """Merge each declared asset into the verified record it describes, or
    keep it as a claim when nothing backs it.

    A declaration matches a verified record of the same owner when the
    account numbers share their last four, or when the type family matches
    and the amounts are within 1% or one digit apart. Two records that each
    carry a full account number are never merged; a declaration never
    overwrites a verified balance — it lands on selfDeclaredAmount, where a
    disagreement surfaces as the existing self-declared-vs-verified finding
    instead of as a second asset that doubles the total.
    """
    for rec in records:
        if rec.get("verificationStatus") is None:
            rec["verificationStatus"] = "verified"

    kept_declared = _drop_total_rows(declared, "Declared assets")
    zero_lines = 0
    for d in kept_declared:
        if d.get("kind") in ("no_assets", "disposal"):
            d["matched"] = True
            continue
        # A self-certification prints every asset kind with a blank or $0
        # beside it: "Cash on hand $0", "Bonds $0". Those lines say the
        # household has none of that kind; they are not assets, and as
        # records they filled Cartograph with five $0 accounts for a
        # household with no assets at all. A $0 line with an account
        # number is a real account at zero and is kept.
        d_amount = _amt_or_none(d.get("amount"))
        if (d_amount is None or d_amount == 0) and not _digits_last4(d.get("accountNumber")):
            d["matched"] = True
            zero_lines += 1
            continue
        d_last4 = _digits_last4(d.get("accountNumber"))
        fam = _asset_family(d.get("accountType"))
        best = None
        for rec in records:
            if rec.get("verificationStatus") == "declared_only":
                continue
            if d.get("assetOwner") and rec.get("assetOwner") and not _same_member(rec.get("assetOwner"), d.get("assetOwner")):
                continue
            r_last4 = _digits_last4(rec.get("accountNumber"))
            if d_last4 and r_last4:
                if d_last4 == r_last4:
                    best = rec
                    break
                continue
            r_value = rec.get("currentBalance") or rec.get("averageSixMonthBalance")
            same_family = _asset_family(rec.get("accountType")) == fam
            untyped = fam in ("other", "")
            if d.get("amount") is None or not r_value:
                continue
            # Same family within tolerance, or — for a line the form does not
            # type ("Personal Property Held as an Investment", "Government
            # Benefits") — the same owner's balance to the cent: a
            # self-certification restates the accounts the statements verify.
            if (same_family and _amounts_close(d["amount"], r_value)) or (
                untyped and _amounts_close(d["amount"], r_value)
            ):
                best = rec
                break
        if best is None and d.get("amount") is not None and _distinctive_amount(d["amount"]):
            # A questionnaire lists the household's assets under whichever
            # member filled it in; the statement names the account holder.
            # A balance that matches a verified record of the same family
            # (or an untyped line) to the cent is that account, not a
            # second one — "Child Support Fund 82.05" under the daughter is
            # the head's checking account ending 2788 with $82.05 in it.
            # Same family first; failing that, any verified record. A
            # distinctive figure equal to the cent is stronger evidence than
            # the type the read attached: "Cash 6,294.74" beside the verified
            # real estate at $6,294.74 is the property, typed wrong.
            exact = [
                rec for rec in records
                if rec.get("verificationStatus") != "declared_only"
                and (rec.get("currentBalance") or rec.get("averageSixMonthBalance"))
                and not _digits_last4(d.get("accountNumber"))
                and _amounts_equal(d["amount"], rec.get("currentBalance") or rec.get("averageSixMonthBalance"))
            ]
            exact.sort(key=lambda rec: 0 if _asset_family(rec.get("accountType")) == fam or fam in ("other", "") else 1)
            if exact:
                rec = exact[0]
                logger.info(
                    "Declared assets: %s %s listed under %s equals %s's verified %s balance to the cent "
                    "— treated as the same asset", d.get("accountType") or "untyped", d["amount"],
                    d.get("assetOwner"), rec.get("assetOwner"), rec.get("accountType") or "asset",
                )
                best = rec
        if best is None and d.get("amount") is not None:
            # The certification lists the household's accounts one line per
            # kind. When the owner has exactly one verified account of that
            # kind and declares exactly one, they are the same account
            # whatever the balances say — the 50059's checking $556 and the
            # bank's verification of the only checking account are one
            # record with a discrepancy, not two accounts and a doubled
            # total. The discrepancy surfaces through selfDeclaredAmount.
            kind = _asset_kind(d.get("accountType"))
            if kind != "other":
                def _owners_ok(rec):
                    return (not d.get("assetOwner") or not rec.get("assetOwner")
                            or _same_member(rec.get("assetOwner"), d.get("assetOwner")))
                same_kind = [
                    rec for rec in records
                    if rec.get("verificationStatus") != "declared_only"
                    and _asset_kind(rec.get("accountType")) == kind and _owners_ok(rec)
                ]
                declared_same = [
                    o for o in kept_declared
                    if o is not d and o.get("kind") not in ("no_assets", "disposal")
                    and _asset_kind(o.get("accountType")) == kind and _owners_ok(o)
                    and o.get("documentType") == d.get("documentType")
                ]
                if len(same_kind) == 1 and not declared_same:
                    logger.info(
                        "Declared assets: the only %s account declared (%s) is the only %s account "
                        "verified (%s) — treated as the same account",
                        kind, d.get("amount"), kind,
                        same_kind[0].get("currentBalance") or same_kind[0].get("averageSixMonthBalance"),
                    )
                    best = same_kind[0]
        if best is not None:
            d["matched"] = True
            if not best.get("selfDeclaredAmount") and d.get("amount") is not None:
                best["selfDeclaredAmount"] = d["amount"]
                best["selfDeclaredSource"] = d.get("documentType")
            if not best.get("incomeAmount") and d.get("incomeAmount") is not None:
                best["incomeAmount"] = d["incomeAmount"]
            if not best.get("sourceName") and d.get("sourceName"):
                best["sourceName"] = d["sourceName"]
            continue
        records.append({
            "documentType": d.get("documentType"),
            "assetOwner": d.get("assetOwner"),
            "sourceName": d.get("sourceName"),
            "selfDeclaredAmount": d.get("amount"),
            "selfDeclaredSource": d.get("documentType"),
            "accountType": d.get("accountType"),
            "accountNumber": d.get("accountNumber"),
            "incomeAmount": d.get("incomeAmount"),
            "sourcePages": [d["page"]] if d.get("page") else [],
            "evidence": {"selfDeclaredAmount": d["quote"]} if d.get("quote") else {},
            "verificationStatus": "declared_only",
        })
        d["matched"] = True


def extract_income(
    groups: list[DocumentGroup],
    settings: Settings,
    certification_type: str | None = None,
    household_names: list[str] | None = None,
    declared_total=None,
) -> IncomeExtraction:
    """Extract income: one call per source document, provenance enforced,
    the household's declarations read separately and reconciled in code.

    See the section header above for why. Per document, in order: extract;
    stamp the group's pages and drop any amount the pages do not carry; if a
    record names a source without an amount, ask once more for the amount
    from this document alone; if the document prints dollar figures and
    produced no record at all, read it once more with neutral wording.
    """
    source_groups = [g for g in groups if g.category != "ignore" and not _is_income_declaration(g)]
    decl_groups = [g for g in groups if g.category != "ignore" and _is_income_declaration(g)]
    if not source_groups and not decl_groups:
        logger.info("No income documents found")
        return IncomeExtraction()
    system_prompt = INCOME_SYSTEM_PROMPT + _PROVENANCE_BLOCK + _GROUNDING_BLOCK

    def _read(g: DocumentGroup, intro: str) -> tuple[list[dict], list[dict]]:
        prompt = _single_document_prompt(g, intro, certification_type, household_names)
        result = validation.validate_income(call_llm_json(system_prompt, prompt, settings))
        si = result.get("sourceIncome") or {}
        vis = [r for r in (si.get("verificationIncome") or []) if isinstance(r, dict)]
        pss = [r for r in (si.get("payStub") or []) if isinstance(r, dict)]
        _enforce_provenance(vis, _INCOME_AMOUNT_FIELDS, g, "Income")
        _note_redactions(vis, ("rateOfPay", "ytdAmount", "overtimeRate"), g)
        _prune_payment_history(vis, g, "Income")
        _enforce_provenance(pss, _PAYSTUB_AMOUNT_FIELDS, g, "Income")
        # A program line reading $0.00 (the retirement letter that also says
        # the SSI payment is $0.00) is not income and never owes a record.
        kept = []
        for vi in vis:
            amounts = [vi.get(f) for f in _INCOME_AMOUNT_FIELDS if vi.get(f) not in (None, "", "null")]
            if amounts and all(_is_zero_money(a) for a in amounts):
                logger.info("Income: '%s' pages %s states $0.00 for %s — not a record",
                            g.document_type, g.pages, vi.get("incomeType") or vi.get("sourceName"))
                continue
            kept.append(vi)
        return kept, pss

    def _one(g: DocumentGroup) -> tuple[DocumentGroup, list[dict], list[dict]]:
        vis, pss = _read(g, "Extract income data from this document:")
        amountless_vi = [vi for vi in vis if (vi.get("sourceName") or vi.get("memberName")) and not _vi_has_amount(vi)]
        amountless_ps = [ps for ps in pss if (ps.get("sourceName") or ps.get("memberName")) and not ps.get("grossPay")]
        if amountless_vi or amountless_ps:
            logger.info("Income: '%s' pages %s has %d record(s) without an amount — asking this document once more",
                        g.document_type, g.pages, len(amountless_vi) + len(amountless_ps))
            recovered = _retry_income_amounts(_build_texts([g]), amountless_vi, amountless_ps, certification_type, settings)
            if recovered:
                _enforce_provenance(amountless_vi, _INCOME_AMOUNT_FIELDS, g, "Income (amount retry)")
                _enforce_provenance(amountless_ps, _PAYSTUB_AMOUNT_FIELDS, g, "Income (amount retry)")
        # A fixed-benefit record with no amount after the retry is a program
        # the letter mentions without paying ("payments were stopped", a
        # second program on the same letter): not income, and it would pair
        # with the paying record as a duplicate source.
        before = len(vis)
        vis = [
            vi for vi in vis
            if _vi_has_amount(vi)
            or (vi.get("incomeType") or "").strip().lower() not in _FIXED_INCOME_TYPES + ("zero income",)
        ]
        if len(vis) < before:
            logger.info("Income: '%s' pages %s: dropped %d fixed-benefit record(s) with no amount",
                        g.document_type, g.pages, before - len(vis))
        if not vis and not pss and _document_amounts(g):
            logger.info("Income: '%s' pages %s prints dollar figures but produced no record — reading it once more",
                        g.document_type, g.pages)
            vis, pss = _read(
                g,
                "This document was read once and produced no income record. Read it again and "
                "extract only the income it actually states. If it states no current income for "
                "anyone, or every figure on it is $0.00, return empty lists:",
            )
        return g, vis, pss

    vi_entries: list[dict] = []
    ps_entries: list[dict] = []
    for g, vis, pss in _run_per_group(source_groups, _one, settings, "Income"):
        vi_entries.extend(vis)
        ps_entries.extend(pss)
    _unify_paystub_sources(ps_entries)
    _repair_paystub_ytd(ps_entries)

    declared = _extract_declared_income(decl_groups, settings, certification_type, household_names)
    _reconcile_income(vi_entries, declared, certification_type, ps_entries, declared_total)

    result = scrub_extracted_dict({
        "sourceIncome": {"payStub": ps_entries, "verificationIncome": vi_entries},
        "declared": declared,
    }) or {}
    si = result.get("sourceIncome") if isinstance(result.get("sourceIncome"), dict) else {}
    for bucket in ("verificationIncome", "payStub"):
        recs = si.get(bucket) if isinstance(si, dict) else None
        if isinstance(recs, list):
            kept, dropped = drop_records_without_identity(recs, identity_fields=("memberName", "sourceName"))
            if dropped:
                logger.warning("Income: dropped %d %s record(s) with no member/source name", dropped, bucket)
            si[bucket] = kept
    if isinstance(si, dict):
        result["sourceIncome"] = si
    vi_final = si.get("verificationIncome", []) if isinstance(si, dict) else []
    logger.info(
        "Extracted %d pay stubs, %d verification income records from %d source document(s); "
        "%d declared line(s), %d declared-only, %d verified-not-declared",
        len(si.get("payStub", []) if isinstance(si, dict) else []), len(vi_final), len(source_groups),
        len(declared),
        sum(1 for v in vi_final if v.get("verificationStatus") == "declared_only"),
        sum(1 for v in vi_final if v.get("verificationStatus") == "verified_not_declared"),
    )
    return IncomeExtraction.model_validate(result)


def extract_assets(
    groups: list[DocumentGroup],
    settings: Settings,
    certification_type: str | None = None,
    household_names: list[str] | None = None,
) -> AssetExtraction:
    """Extract assets: one call per statement or verification document,
    provenance enforced; self-certifications and certification-form asset
    rows are read as declarations and merged into the records they describe.
    """
    source_groups = [g for g in groups if g.category != "ignore" and not _is_asset_declaration(g)]
    decl_groups = [g for g in groups if g.category != "ignore" and _is_asset_declaration(g)]
    if not source_groups and not decl_groups:
        logger.info("No asset documents found")
        return AssetExtraction()
    system_prompt = ASSET_SYSTEM_PROMPT + _PROVENANCE_BLOCK + _GROUNDING_BLOCK

    def _read(g: DocumentGroup, intro: str) -> list[dict]:
        prompt = _single_document_prompt(g, intro, certification_type, household_names)
        result = validation.validate_assets(call_llm_json(system_prompt, prompt, settings))
        recs = [r for r in (result.get("assetInformation") or []) if isinstance(r, dict)]
        _enforce_provenance(recs, _ASSET_AMOUNT_FIELDS, g, "Assets")
        _note_redactions(recs, ("currentBalance", "averageSixMonthBalance", "incomeAmount"), g)
        return recs

    def _one(g: DocumentGroup) -> list[dict]:
        recs = _read(g, "Extract asset data from this document:")
        if not recs and _document_amounts(g):
            logger.info("Assets: '%s' pages %s prints dollar figures but produced no record — reading it once more",
                        g.document_type, g.pages)
            recs = _read(
                g,
                "This document was read once and produced no asset record. Read it again and "
                "extract only the accounts or property it actually shows; if it shows none, "
                "return an empty list:",
            )
        return recs

    records: list[dict] = []
    for recs in _run_per_group(source_groups, _one, settings, "Assets"):
        records.extend(recs)
    records = _dedupe_asset_records(records)
    logger.info("Extracted %d asset records from %d source document(s)", len(records), len(source_groups))

    declared = _extract_declared_assets(decl_groups, settings, certification_type, household_names)
    _reconcile_assets(records, declared)

    zero_lines = sum(
        1 for d in declared
        if d.get("kind") == "asset" and (_amt_or_none(d.get("amount")) in (None, 0.0)) and not _digits_last4(d.get("accountNumber"))
    )
    if zero_lines:
        logger.info("Assets: %d declared line(s) state $0 or no amount with no account number — none of that kind, not records", zero_lines)
    result = scrub_extracted_dict({"assetInformation": records, "declared": declared}) or {}
    if isinstance(result.get("assetInformation"), list):
        kept, dropped = drop_records_without_identity(
            result["assetInformation"], identity_fields=("sourceName", "accountType", "assetOwner"),
        )
        if dropped:
            logger.warning("Assets: dropped %d asset record(s) with no identity", dropped)
        result["assetInformation"] = kept
    logger.info(
        "Assets: %d record(s) after reconciling %d declared line(s); %d declared-only",
        len(result.get("assetInformation", [])), len(declared),
        sum(1 for r in result.get("assetInformation", []) if r.get("verificationStatus") == "declared_only"),
    )
    return AssetExtraction.model_validate(result)
