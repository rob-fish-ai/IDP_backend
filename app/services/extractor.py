"""Field extraction service — uses LLM to extract structured data per MuleSoft schema."""

import logging

from app.core.config import Settings
from app.schemas.extraction import (
    AssetExtraction,
    CertificationInfo,
    DocumentGroup,
    DocumentInventory,
    HouseholdDemographics,
    IncomeExtraction,
)
from app.services.llm_service import call_llm_json
from app.services import validation
from app.services.text_sanitizer import (
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
- SSN MASKING: NEVER output a full SSN. ALWAYS mask as ***-**-XXXX (last 4 digits only).
- NAME FORMATTING: ALWAYS Title Case for ALL names.
- If the document is a Calculation Worksheet (keywords: "Calculation", "Calculator", "Worksheet", "Calc Sheet", "PCAP", "CF-51", "LIHTC Calc"), return {"houseHold": []} immediately.

EXTRACTION RULES:
- householdMemberNumber: 2-digit string with leading zero ("01", "02"). From TIC → "HH Mbr#"; HUD 50059 → Field 33 "No."; RD 3560 → derive from row order. null for pay stubs, bank statements, etc.
- FirstName: Title Case. null if only single name field.
- MiddleName: Title Case, or null.
- LastName: Title Case. Include suffixes (Jr., Sr., III). Preserve hyphens and multi-word names.
- socialSecurityNumber: ALWAYS ***-**-XXXX. null if not found.
- DOB: YYYY-MM-DD format. "01/15/1990" → "1990-01-15". If no day, default DD to 01.
- SOURCE PRIORITY for DOB and SSN: printed identity documents (driver
  license, state ID, Social Security card pages) are AUTHORITATIVE — when
  an Identity Document page shows a member's DOB or SSN, use that value
  over any handwritten application/questionnaire entry (handwriting is
  routinely misread). Same priority for name spellings.
- head: "H" for head of household/primary applicant. null for all others. Maximum ONE "H".
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

Extract income data from the provided document text into the MuleSoft Income schema.

CRITICAL RULES:
- SSN MASKING: ALWAYS ***-**-XXXX. No exceptions.
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
current rate IS the new rate. Extract every dollar figure you see.

CHILD SUPPORT STATEMENT — ALWAYS EXTRACT:
Any "Child Support Statement", "Child Support Order", "Child Support Verification",
court order with ordered amounts, or DOR/State Disbursement Unit statement → create
a verificationIncome entry:
  - sourceName: payer name, or "Child Support" / state agency name if payer unknown
  - memberName: the custodial parent/head of household receiving support
  - incomeType: "Child Support"
  - type_of_VOI: "Child Support Order"
  - selfDeclaredAmount: monthly or annual support amount as shown
  - rateOfPay: support amount per payment period, if shown
  - frequencyOfPay: payment frequency (weekly, bi-weekly, monthly), if shown
Do NOT skip child support just because the form is brief or lacks typical wage fields.
If a TIC or cert form lists household income that exceeds the sum of wage sources, and
a Child Support Statement is present, the gap is almost always the child support amount.

PAYSTUB FIELDS:
- sourceName: employer name, Title Case
- memberName: employee name, Title Case
- socialSecurityNumber: ***-**-XXXX
- grossPay: exact dollar amount with cents, numeric string ("1250.00"), no $ or commas
- payDate: YYYY-MM-DD
- payInterval: lowercase (weekly / bi-weekly / semi-monthly / monthly)
- ytdGross: year-to-date gross on the stub ("YTD Gross", "YTD Earnings"),
  numeric string, no $ or commas. null if the document shows no YTD figure
  (EIV/Work Number quarterly rows have none).

SPECIAL PAYSTUB RULES:
- Work Number/Equifax: take the 6 most current entries only
- Child Support: last 6 most recent payments
- Do NOT create pay stubs for SSA, pension, or TANF (these go to verificationIncome)

VERIFICATION INCOME FIELDS:
- sourceName, memberName, socialSecurityNumber (same rules as payStub)
- programName: official program name for benefit income
- selfDeclaredAmount: from self-cert forms, applications, or questionnaires, numeric string. Match to the corresponding employer/source by name when possible.
- dateReceived: date the VOI form was received or date signed by employer. YYYY-MM-DD. null if not shown.
- rateOfPay: numeric string (hourly or periodic rate)
- frequencyOfPay: lowercase. This is how often the person is PAID (weekly / bi-weekly / semi-monthly / monthly), NOT the rate unit. If rate is "hourly" but pay dates are 14 days apart, frequencyOfPay is "bi-weekly". Determine from pay period structure, not from rate label.
- hoursPerPayPeriod: hours per week
- overtimeRate: only if person actually receives overtime
- overtimeFrequency: same frequency as regular pay
- ytdAmount: only if document explicitly states "year to date". MUST BE null for SSA/fixed income.
- ytdStartDate, ytdEndDate: YYYY-MM-DD
- incomeType: one of: Non-Federal Wage, Federal Wage, Social Security, Supplemental Security Income, Social Security Disability, Pension, Temporary Assistance, Child Support, Self-Employment, Zero Income, Other Income
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

SELF-DECLARED AMOUNTS FROM QUESTIONNAIRE:
- When an Application or Housing Questionnaire mentions income amounts (e.g., "income has changed", "currently earning", "expected income"), extract as selfDeclaredAmount on the matching verificationIncome entry.
- Match self-declared amounts to the corresponding employer/source by name.
- If a questionnaire states a specific dollar amount for an employer, place it in selfDeclaredAmount of that employer's verificationIncome entry.

FORMATTING:
- Monetary: numeric string with 2 decimal places, no $ or commas
- Dates: YYYY-MM-DD
- Names: Title Case
- payInterval/frequencyOfPay: lowercase

Return ONLY valid JSON: {"sourceIncome": {"payStub": [...], "verificationIncome": [...]}}"""

ASSET_SYSTEM_PROMPT = """\
You are an expert data extractor for HUD/Affordable Housing asset documents.

Extract asset data from the provided document text into the MuleSoft Asset schema.

CRITICAL RULES:
- SSN MASKING: ALWAYS ***-**-XXXX.
- NAME FORMATTING: ALWAYS Title Case.
- If document is a Calculation Worksheet, return {"assetInformation": []} immediately.
- PROCESS EVERY DOCUMENT separated by "---". The input may contain 5+ asset
  documents (multiple bank VOAs, a real estate worksheet, life insurance,
  etc.). Each distinct account or property = ONE separate entry in the
  output array. Do NOT stop after the first few. Do NOT skip pages.
- Before returning, count the asset documents in the input — your output
  array length should be at least equal to the number of distinct accounts/
  properties shown across all documents.

DOCUMENT ROUTING:
- bankStatment route: actual bank statements with transactions
- verificationOfAsset route: VOA/VOD forms, life insurance, investment statements
- Self-declared amounts: from asset self-certification, applications, questionnaires

FIELDS:
- documentType: Bank Statement — Checking, Bank Statement — Savings, Verification of Assets, Life Insurance, Investment Account, Asset Self-Certification, ABLE Account, Real Estate, Certificate of Deposit, Cryptocurrency, Prepaid Card, Annuity, Direct Express Card
- assetOwner: account holder name, Title Case
- socialSecurityNumber: ***-**-XXXX
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

HUD 50059 / TIC ASSET EXTRACTION:
- HUD 50059 Section D contains asset information (fields 76-80): Description, Status, Cash Value, Actual Yearly Income, Date Divested
- Extract each asset row as a separate entry. Map "Cash Value" → currentBalance, "Actual Yearly Income" → incomeAmount
- Common descriptions: "Checking account", "Savings account", "Other asset", "Life insurance", etc.
- The member number column tells you which household member owns the asset — match to person name from Section C
- TIC Part V contains similar asset data — extract the same way

Return ONLY valid JSON: {"assetInformation": [...]}"""

INVENTORY_FINANCIAL_SYSTEM_PROMPT = """\
You are an expert document cataloger for HUD/Affordable Housing certification files.

Catalog all FINANCIAL and COMPLIANCE documents (NOT HUD-specific forms) found in the provided text.

DOCUMENT TYPES TO CATALOG (23 types):
Pay Stub, Bank Statement, SSA Benefit Letter, SSI Benefit Letter, SSDI Benefit Letter, Pension Statement / Letter, TANF / Public Assistance Verification, Child Support Order / Statement, Verification of Income (VOI / VOE), Verification of Deposit (VOD), Verification of Assets (VOA), Life Insurance Policy / Statement, Asset Self-Certification, Divestiture of Assets Certification, Student Status Affidavit / Certification, Annual Student Certification, Unemployed / Zero Income Affidavit, Child Support / Alimony Affidavit, Household Demographics Form, Application / Household Roster, Self-Declaration / Self-Certification, Work Number / Equifax Income Report, Income Calculation Worksheet (flag only)

SKIP THESE (handled by HUD inventory):
HUD 9887, HUD 9887-A, HUD 92006, EIV Summary Report, EIV Income Report, Acknowledgement of Receipt of HUD Forms, Race and Ethnic Data Reporting Form, Authorization to Release Information, Tenant Release and Consent Form

EXCLUDE ENTIRELY:
Fax cover sheets, blank/unfilled forms, credit screening reports, IRS Form 4506-T, duplicate pages, internal manager correspondence

FIELDS:
- documentType: from the 23 types above
- documentTitle: specific title/heading on the document
- sourceOrganization: issuing agency/employer/bank, Title Case
- personName: person named on document, Title Case
- pageRange: e.g. "1", "4-5", "7-9"
- pageCount: number of pages
- isSigned: "Yes", "No", or "N/A"
- signedBy: signer name in Title Case, or null
- signatureDate: YYYY-MM-DD or null
- documentDate: YYYY-MM-DD or null
- notes: notable observations or null

RULES:
- NO PII extraction (no SSNs, account numbers, financial amounts)
- Consecutive pages of same doc + same person = single entry
- Same doc type for different people = separate entries
- Income Calculation Worksheets: include with note "Internal calculation document — excluded from IDP data extraction per Document Exclusion rules"
- Always catalog: Asset Self-Cert, Student Status, Zero Income, Application, etc.
- If poor scan quality, note it

Return ONLY valid JSON: {"documents": [...]}"""

INVENTORY_HUD_SYSTEM_PROMPT = """\
You are an expert document cataloger for HUD compliance forms in affordable housing certification files.

Catalog ONLY the following 9 HUD compliance form types. Ignore ALL other documents.

HUD FORMS TO CATALOG:
1. Authorization to Release Information — property-specific, authorizes income/asset verification
2. Tenant Release and Consent — authorizes release of tenant info to third parties
3. HUD-9887 — "Notice and Consent for Release of Information", ONE per household, signed by ALL adults
4. HUD-9887-A — "Applicant's/Tenant's Consent", ONE per adult member, signed by tenant AND owner
5. HUD-92006 — "Supplement to Application for Federally Assisted Housing", emergency contact info
6. EIV Summary Report — system-generated, household-level summary, NOT signed
7. EIV Income Report — system-generated, wage/benefit data, NOT signed
8. Acknowledgement of Receipt of HUD Forms — checklist of received documents, signed by resident
9. Race and Ethnic Data Reporting Form — OMB No. 2502-0204, ONE per household member, race/ethnicity checkboxes

FIELDS:
- documentType: one of the 9 types above
- documentTitle: specific title on the form
- sourceOrganization: property management company or HUD, Title Case
- personName: person named on form, Title Case
- pageRange: e.g. "1", "4-5"
- pageCount: number of pages
- isSigned: "Yes", "No", or "N/A" (N/A for system-generated EIV reports)
- signedBy: signer name in Title Case, or null
- signatureDate: YYYY-MM-DD or null
- documentDate: YYYY-MM-DD or null
- notes: notable observations or null

RULES:
- NO PII extraction
- Same form for different people = separate entries
- Multi-page forms = single entry with full page range
- If zero HUD forms found, return {"documents": []}
- Note poor scan quality if applicable

Return ONLY valid JSON: {"documents": [...]}"""


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
            f"Person: {g.person_name or 'Unknown'}]\n{g.combined_text}"
        )
    return texts


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
        return [f for f in _CRITICAL_CERT_FIELDS if not cert_info_dict.get(f)]

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

    # Strip HTML/markup leakage from field values before schema validation.
    cert_info_dict = scrub_extracted_dict(cert_info_dict) or {}

    logger.info(
        "Extracted certification info: type=%s",
        cert_info_dict.get("certificationType"),
    )
    return CertificationInfo.model_validate(cert_info_dict)


# Fields considered critical for cert_info. If any of these come back null
# from the first extraction, a targeted retry runs to try to recover them.
# These are the fields that downstream scoring, compliance checks, and the
# frontend display all depend on.
_CRITICAL_CERT_FIELDS = [
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


def extract_income(
    groups: list[DocumentGroup],
    settings: Settings,
    certification_type: str | None = None,
) -> IncomeExtraction:
    """Extract income data from pre-routed income document groups.

    After the first pass, any income record that was extracted WITH a source
    but WITHOUT a dollar amount triggers one targeted retry for just those
    records. LLM extraction is non-deterministic — the same prompt can name a
    source on one run and drop its amount on the next. A focused retry ("find
    the amount for these specific sources") usually recovers it. An income
    record with a name but no amount is unusable downstream (no income
    calculation, no MuleSoft comparison value), so it's worth one more look.

    A second retry recovers income SOURCES dropped entirely: if a classified
    benefit document (SSA/SSI/SSDI/pension/child support) yielded no record of
    its type, that document is re-extracted on its own. Both retries go through
    the shared _retry_if_incomplete driver; each is max 1 and fail-safe.
    """
    relevant_texts = _build_texts(groups)
    if not relevant_texts:
        logger.info("No income documents found")
        return IncomeExtraction()

    user_prompt = "Extract income data from these documents:\n\n" + "\n\n---\n\n".join(relevant_texts)
    user_prompt += _get_cert_context(certification_type)

    result = call_llm_json(INCOME_SYSTEM_PROMPT, user_prompt, settings)
    result = validation.validate_income(result)

    # Re-bind to the actual lists inside `result` so a retry that APPENDS
    # (source recovery) propagates — `or []` on an empty list would otherwise
    # hand back a fresh, disconnected list.
    si = result.get("sourceIncome") or {}
    result["sourceIncome"] = si
    vi_entries = si.get("verificationIncome") or []
    si["verificationIncome"] = vi_entries
    ps_entries = si.get("payStub") or []
    si["payStub"] = ps_entries

    # --- Self-healing retry #1: recover income SOURCES dropped entirely.
    # A classified benefit document (SSA/SSI/SSDI/pension/child support) whose
    # income type produced no record means the source was dropped on the first
    # pass — re-extract that document on its own. Runs BEFORE the amount retry
    # so a recovered source can still get its amount filled below.
    def _cov_gate():
        return _income_coverage_gaps(groups, vi_entries) or None

    def _cov_fill(gaps) -> None:
        added = _retry_income_coverage(gaps, vi_entries, certification_type, settings)
        if added:
            logger.info("Income coverage retry recovered %d dropped source(s)", added)

    _retry_if_incomplete("Income coverage", gate=_cov_gate, fill=_cov_fill)

    # --- Self-healing retry #2: recover amounts for records that have a source
    # but no dollar figure. Records with no name AND no amount are noise
    # (validate_income already drops them), so they don't trigger a retry.
    def _amt_gate():
        amountless_vi = [
            vi for vi in vi_entries
            if (vi.get("sourceName") or vi.get("memberName")) and not _vi_has_amount(vi)
        ]
        amountless_ps = [
            ps for ps in ps_entries
            if (ps.get("sourceName") or ps.get("memberName")) and not ps.get("grossPay")
        ]
        return (amountless_vi, amountless_ps) if (amountless_vi or amountless_ps) else None

    def _amt_fill(spec) -> None:
        amountless_vi, amountless_ps = spec
        recovered = _retry_income_amounts(
            relevant_texts, amountless_vi, amountless_ps, certification_type, settings,
        )
        if recovered:
            logger.info("Income retry recovered amounts for %d record(s)", recovered)

    _retry_if_incomplete("Income amounts", gate=_amt_gate, fill=_amt_fill)

    # Scrub HTML/markup from extracted strings, then drop records lacking
    # any usable identity (no member AND no source name). Such records
    # would otherwise propagate as junk findings against tag fragments.
    result = scrub_extracted_dict(result) or {}
    si = result.get("sourceIncome") if isinstance(result.get("sourceIncome"), dict) else {}
    for bucket in ("verificationIncome", "payStub"):
        recs = si.get(bucket) if isinstance(si, dict) else None
        if isinstance(recs, list):
            kept, dropped = drop_records_without_identity(
                recs, identity_fields=("memberName", "sourceName"),
            )
            if dropped:
                logger.warning(
                    "Income: dropped %d %s record(s) with no member/source name "
                    "(HTML/markup-only or empty after sanitization)",
                    dropped, bucket,
                )
            si[bucket] = kept
    if isinstance(si, dict):
        result["sourceIncome"] = si

    logger.info(
        "Extracted %d pay stubs, %d verification income records",
        len(ps_entries), len(vi_entries),
    )
    return IncomeExtraction.model_validate(result)


# Amount fields that make a verificationIncome record "usable". At least one
# must be populated, or we know the source exists but not how much it pays.
_VI_AMOUNT_FIELDS = ("rateOfPay", "selfDeclaredAmount", "ytdAmount", "overtimeRate")

# Income types whose amount is a fixed benefit, never a YTD figure (mirrors the
# rule in validation.validate_income). YTD recovered for these is discarded.
_FIXED_INCOME_TYPES = (
    "social security", "supplemental security income",
    "social security disability", "pension",
)


def _vi_has_amount(vi: dict) -> bool:
    """True if a verificationIncome record carries any usable dollar amount."""
    return any(vi.get(f) for f in _VI_AMOUNT_FIELDS)


_INCOME_AMOUNT_RETRY_PROMPT = """\
You are re-examining HUD/Affordable Housing income documents to recover the
DOLLAR AMOUNT for income sources that were extracted WITHOUT one on the first pass.

Each target below is an income source already identified in the documents but
missing its amount. For each id, find the income figure in the document text.
Look hard — the amount is almost always present near the source name.

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
_INCOME_DOC_TYPE_EXPECTATIONS: dict[str, tuple[str, set[str], set[str]]] = {
    "SSA Benefit Letter": ("Social Security", {"social security"}, {"social security administration"}),
    "SSI Benefit Letter": ("Supplemental Security Income", {"supplemental security income"}, {"supplemental security"}),
    "SSDI Benefit Letter": ("Social Security Disability", {"social security disability"}, {"social security disability"}),
    "Pension Statement": ("Pension", {"pension"}, {"pension"}),
    "Child Support Statement": ("Child Support", {"child support"}, {"child support"}),
    "Child Support / Alimony Affidavit": ("Child Support", {"child support"}, {"child support"}),
    "TANF Verification": ("Temporary Assistance", {"temporary assistance"}, {"tanf"}),
    "TANF / Public Assistance Verification": ("Temporary Assistance", {"temporary assistance"}, {"tanf", "public assistance"}),
}


def _income_type_present(types: set[str], keywords: set[str], vi_entries: list[dict]) -> bool:
    """True if any extracted record represents this income type — by exact
    normalized incomeType, or a distinctive sourceName keyword."""
    for vi in vi_entries:
        it = (vi.get("incomeType") or "").strip().lower()
        sn = (vi.get("sourceName") or "").strip().lower()
        if it in types:
            return True
        if any(kw in sn for kw in keywords):
            return True
    return False


def _income_coverage_gaps(
    groups: list[DocumentGroup], vi_entries: list[dict],
) -> list[tuple[DocumentGroup, str, set[str], set[str]]]:
    """Classified income documents whose expected income type has no record."""
    gaps: list[tuple[DocumentGroup, str, set[str], set[str]]] = []
    for g in groups:
        spec = _INCOME_DOC_TYPE_EXPECTATIONS.get(g.document_type)
        if not spec:
            continue
        label, types, kws = spec
        if not _income_type_present(types, kws, vi_entries):
            gaps.append((g, label, types, kws))
    return gaps


def _retry_income_coverage(
    gaps: list[tuple[DocumentGroup, str, set[str], set[str]]],
    vi_entries: list[dict],
    certification_type: str | None,
    settings: Settings,
) -> int:
    """Re-extract income from documents whose income type was dropped entirely.

    Appends only records of the expected type that aren't already present, and
    only ones the model finds in the document (the prompt forbids inventing).
    Mutates vi_entries in place; returns the count added.
    """
    added = 0
    for g, label, types, kws in gaps:
        # An earlier gap's retry may already have supplied this type.
        if _income_type_present(types, kws, vi_entries):
            continue
        doc_text = (
            f"[Document: {g.document_type}, Pages: {g.page_range}, "
            f"Person: {g.person_name or 'Unknown'}]\n{g.combined_text}"
        )
        prompt = (
            f"This document is a '{g.document_type}' and documents {label} income "
            f"for the household, but the first extraction pass produced no {label} "
            f"income record. Re-read it and extract the income record(s) it contains.\n"
            f"Extract ONLY income actually present in this document — do NOT invent "
            f"an amount or a source.\n\n" + doc_text
        )
        prompt += _get_cert_context(certification_type)
        try:
            result = call_llm_json(INCOME_SYSTEM_PROMPT, prompt, settings)
            result = validation.validate_income(result)
        except Exception:
            logger.exception("Income coverage retry failed for %s", g.document_type)
            continue
        new_vis = ((result.get("sourceIncome") or {}).get("verificationIncome")) or []
        for vi in new_vis:
            it = (vi.get("incomeType") or "").strip().lower()
            sn = (vi.get("sourceName") or "").strip().lower()
            if not (it in types or any(kw in sn for kw in kws)):
                continue   # not the type we're recovering
            if _income_type_present(types, kws, vi_entries):
                break      # already recovered
            vi_entries.append(vi)
            added += 1
    return added


_ASSET_DOC_TYPES_PER_RECORD = {
    # Document types that typically yield ONE asset record per document.
    # Used for gap detection — if extraction count is far below the count
    # of these doc types in the input, retry with the missed groups.
    "Bank Statement",
    "Verification of Assets (VOA)",
    "Verification of Deposit (VOD)",
    "Life Insurance Policy",
    "Asset Self-Certification",
    "Real Estate",
    "Investment Account Statement",
    "Direct Express Card Verification",
    "Debit Card Asset Self-Certification",
}


def extract_assets(
    groups: list[DocumentGroup],
    settings: Settings,
    certification_type: str | None = None,
) -> AssetExtraction:
    """Extract asset data from pre-routed asset document groups.

    After the first extraction, if the number of records is suspiciously
    low compared to the number of asset documents in the input, retry the
    missed groups with a targeted prompt. This catches the common LLM
    failure mode of dropping records when 5+ asset documents are sent at once.
    """
    relevant_texts = _build_texts(groups)
    if not relevant_texts:
        logger.info("No asset documents found")
        return AssetExtraction()

    user_prompt = "Extract asset data from these documents:\n\n" + "\n\n---\n\n".join(relevant_texts)
    user_prompt += _get_cert_context(certification_type)

    result = call_llm_json(ASSET_SYSTEM_PROMPT, user_prompt, settings)
    result = validation.validate_assets(result)

    asset_records = result.get("assetInformation", []) or []
    logger.info("Extracted %d asset records (initial pass)", len(asset_records))

    # --- Self-healing retry: gap detection. Each Bank Statement / VOA / Real
    # Estate / etc. group should yield at least one record; if we're short,
    # re-extract just the missed groups.
    def _gate():
        expected = [g for g in groups if g.document_type in _ASSET_DOC_TYPES_PER_RECORD]
        current = result.get("assetInformation", []) or []
        return expected if (expected and len(current) < len(expected)) else None

    def _fill(expected_groups) -> None:
        records = result.get("assetInformation", []) or []
        retry_records = _retry_missed_asset_groups(
            records, expected_groups, certification_type, settings,
        )
        if not retry_records:
            return
        merged = _dedupe_asset_records(records + retry_records)
        added = len(merged) - len(records)
        if added > 0:
            result["assetInformation"] = merged
            logger.info(
                "Asset retry added %d new records after dedup (total now %d)",
                added, len(merged),
            )
        else:
            logger.info(
                "Asset retry returned %d records but all were duplicates — kept initial set",
                len(retry_records),
            )

    _retry_if_incomplete("Assets", gate=_gate, fill=_fill)

    # Scrub HTML/markup, then drop assets with no usable source identity
    # (matches the gate used on members and income records).
    result = scrub_extracted_dict(result) or {}
    if isinstance(result.get("assetInformation"), list):
        kept, dropped = drop_records_without_identity(
            result["assetInformation"],
            identity_fields=("sourceName", "accountType"),
        )
        if dropped:
            logger.warning(
                "Assets: dropped %d asset record(s) with no source/account-type "
                "identity (HTML/markup-only or empty after sanitization)", dropped,
            )
        result["assetInformation"] = kept

    return AssetExtraction.model_validate(result)


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


def _retry_missed_asset_groups(
    extracted: list[dict],
    expected_groups: list[DocumentGroup],
    certification_type: str | None,
    settings: Settings,
) -> list[dict]:
    """Identify asset groups whose pages aren't represented in extracted
    records, then send a targeted retry for just those groups.

    Matching uses sourceName + accountNumber only (not assetOwner — owner
    name appears on every page in single-person households and would mark
    all groups as covered).
    """
    extracted_signatures: set[str] = set()
    for rec in extracted:
        # sourceName: institution-specific, good signal
        src = rec.get("sourceName")
        if src and len(str(src).strip()) >= 4:
            extracted_signatures.add(str(src).lower().strip())
        # accountNumber: very specific
        acct = rec.get("accountNumber")
        if acct and len(str(acct).strip()) >= 4:
            extracted_signatures.add(str(acct).lower().strip())

    missed_groups: list[DocumentGroup] = []
    for g in expected_groups:
        text_lower = (g.combined_text or "").lower()
        # Skip if any extracted record's signature appears in this group text
        if any(sig in text_lower for sig in extracted_signatures):
            continue
        missed_groups.append(g)

    if not missed_groups:
        return []

    logger.info(
        "Asset retry: %d missed groups (types: %s)",
        len(missed_groups),
        [g.document_type for g in missed_groups],
    )

    missed_texts = _build_texts(missed_groups)
    if not missed_texts:
        return []

    retry_prompt = (
        "These asset documents were missed in the first extraction pass. "
        "Extract a separate asset record for EACH document below — do NOT "
        "skip any. Return only the new records, not the previously extracted "
        "ones.\n\n"
        + "\n\n---\n\n".join(missed_texts)
    )
    retry_prompt += _get_cert_context(certification_type)

    try:
        retry_result = call_llm_json(ASSET_SYSTEM_PROMPT, retry_prompt, settings)
        retry_result = validation.validate_assets(retry_result)
        return retry_result.get("assetInformation", []) or []
    except Exception:
        logger.exception("Asset retry call failed — keeping initial records")
        return []


def extract_document_inventory_financial(
    groups: list[DocumentGroup],
    settings: Settings,
) -> DocumentInventory:
    """Catalog all financial/compliance documents (Section 23)."""
    # Send all non-HUD groups
    hud_types = {
        "HUD 9887", "HUD 9887-A", "HUD 92006",
        "HUD Race and Ethnic Data Form",
        "Acknowledgement of Receipt of HUD Forms",
        "Authorization to Release Information",
        "Tenant Release and Consent Form",
        "EIV Summary Report", "EIV Income Report",
    }

    relevant_texts = []
    for g in groups:
        if g.document_type in hud_types:
            continue
        relevant_texts.append(
            f"[Pages: {g.page_range}, Classified as: {g.document_type}, "
            f"Category: {g.category}, Person: {g.person_name or 'Unknown'}]\n"
            f"{g.combined_text}"
        )

    if not relevant_texts:
        return DocumentInventory()

    user_prompt = (
        "Catalog these financial and compliance documents:\n\n"
        + "\n\n---\n\n".join(relevant_texts)
    )

    result = call_llm_json(INVENTORY_FINANCIAL_SYSTEM_PROMPT, user_prompt, settings)
    result = validation.validate_document_inventory(result)

    logger.info(
        "Financial inventory: %d documents cataloged",
        len(result.get("documents", [])),
    )
    return DocumentInventory.model_validate(result)


def extract_document_inventory_hud(
    groups: list[DocumentGroup],
    settings: Settings,
) -> DocumentInventory:
    """Catalog HUD compliance forms only (Section 24)."""
    hud_types = {
        "HUD 9887", "HUD 9887-A", "HUD 92006",
        "HUD Race and Ethnic Data Form",
        "Acknowledgement of Receipt of HUD Forms",
        "Authorization to Release Information",
        "Tenant Release and Consent Form",
        "EIV Summary Report", "EIV Income Report",
    }

    relevant_texts = []
    for g in groups:
        if g.document_type in hud_types or g.category == "compliance":
            relevant_texts.append(
                f"[Pages: {g.page_range}, Classified as: {g.document_type}, "
                f"Person: {g.person_name or 'Unknown'}]\n{g.combined_text}"
            )

    if not relevant_texts:
        return DocumentInventory()

    user_prompt = (
        "Catalog HUD compliance forms from these documents:\n\n"
        + "\n\n---\n\n".join(relevant_texts)
    )

    result = call_llm_json(INVENTORY_HUD_SYSTEM_PROMPT, user_prompt, settings)
    result = validation.validate_document_inventory(result)

    logger.info(
        "HUD inventory: %d documents cataloged",
        len(result.get("documents", [])),
    )
    return DocumentInventory.model_validate(result)
