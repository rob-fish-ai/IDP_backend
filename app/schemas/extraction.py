"""Pydantic models for document classification, extraction, and MuleSoft output schemas."""

from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Document Classification
# ---------------------------------------------------------------------------

class PageClassification(BaseModel):
    page: int
    document_type: str = Field(
        ..., description="Specific document type (e.g., 'Paystub', 'VOI', 'Bank Statement')"
    )
    category: str = Field(
        ..., description="'include', 'compliance', or 'ignore'"
    )
    person_name: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: Optional[str] = None
    # How well the canonical label fits: exact | alias | nearest | none.
    fit: Optional[str] = None
    observed_title: Optional[str] = None


class ClassificationResult(BaseModel):
    pages: list[PageClassification]


# ---------------------------------------------------------------------------
# Document Grouping
# ---------------------------------------------------------------------------

class DocumentGroup(BaseModel):
    document_type: str
    category: str
    person_name: Optional[str] = None
    pages: list[int]
    page_range: str  # e.g. "4-6"
    combined_text: str
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# MuleSoft Schema — Household Demographics (Section 20)
# ---------------------------------------------------------------------------

class HouseholdMember(BaseModel):
    householdMemberNumber: Optional[str] = None
    FirstName: Optional[str] = None
    MiddleName: Optional[str] = None
    LastName: Optional[str] = None
    socialSecurityNumber: Optional[str] = None
    DOB: Optional[str] = None
    gender: Optional[str] = None
    head: Optional[str] = None
    # As the certification states it: "Head", "Spouse", "Daughter",
    # "Granddaughter". Every certification form has the column, and the
    # consumer has a column for it, and until this field existed the value
    # was read by nobody — the adapter warned "relationship not extracted"
    # on every member of every case, for a field the extractor had no place
    # to put.
    relationship: Optional[str] = None
    disabled: Optional[str] = None
    student: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class HouseholdDemographics(BaseModel):
    houseHold: list[HouseholdMember] = []


# ---------------------------------------------------------------------------
# MuleSoft Schema — Certification Info (Section 2)
# ---------------------------------------------------------------------------

class CertificationInfo(BaseModel):
    certificationType: Optional[str] = None  # MI, AR, AR-SC, IR
    effectiveDate: Optional[str] = None
    # The move-in date the form prints. Equal to the effective date, the
    # document is a move-in certification whatever the caller said it was.
    moveInDate: Optional[str] = None
    numberOfBedrooms: Optional[str] = None
    grossRent: Optional[str] = None
    tenantRent: Optional[str] = None
    utilityAllowance: Optional[str] = None
    rentLimit: Optional[str] = None
    # Rent assistance as the certification records it. These existed only as
    # underscore-prefixed names read off __dict__ that nothing ever wrote, so
    # the check comparing them against assistance documents asserted "$0 on
    # the certification" without having read anything. None means the field
    # was not found, which is different from a recorded zero.
    federalRentAssistance: Optional[str] = None
    nonFederalRentAssistance: Optional[str] = None
    householdIncome: Optional[str] = None
    householdSize: Optional[str] = None
    unitNumber: Optional[str] = None
    signatureDate: Optional[str] = None
    isSigned: Optional[str] = None
    applicationSignDate: Optional[str] = None
    # Compliance tracking fields
    formsPresent: list[str] = []
    missingForms: list[str] = []
    complianceStatus: Optional[str] = None  # Complete, Incomplete, Pending Review

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        # LLMs sometimes return floats for money/numeric fields (e.g.
        # grossRent=72.0). Schema expects strings — coerce before validation.
        # Skips list/dict so list[str] fields and nested objects work.
        if v is not None and not isinstance(v, (str, dict, list)):
            return str(v)
        return v


# ---------------------------------------------------------------------------
# MuleSoft Schema — Income Extraction V4.3 (Section 21)
# ---------------------------------------------------------------------------

class PayStubEntry(BaseModel):
    sourceName: Optional[str] = None
    memberName: Optional[str] = None
    socialSecurityNumber: Optional[str] = None
    # The employee / ID number the stub prints. Two stubs for one member
    # that share it come from one employer whatever their headers read.
    employeeId: Optional[str] = None
    grossPay: Optional[str] = None
    payDate: Optional[str] = None
    payInterval: Optional[str] = None
    ytdGross: Optional[str] = None
    # Provenance: the packet pages this record was read from (set by the
    # extractor from the document group, never by the model) and, per amount
    # field, the verbatim text on those pages that carries the figure.
    sourcePages: list[int] = []
    evidence: dict[str, str] = {}


class Address(BaseModel):
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None


class PaymentHistoryRow(BaseModel):
    """One line of a payment record: what was paid on a date. The engine
    annualises the ledger; the model never sums or picks a row."""
    date: Optional[str] = None      # YYYY-MM-DD; a month-only line is its first day
    amount: Optional[str] = None    # numeric string, the amount actually paid

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        if v is not None and not isinstance(v, str):
            return str(v)
        return v


class VerificationIncomeEntry(BaseModel):
    sourceName: Optional[str] = None
    memberName: Optional[str] = None
    socialSecurityNumber: Optional[str] = None
    programName: Optional[str] = None
    selfDeclaredAmount: Optional[str] = None
    selfDeclaredSource: Optional[str] = None  # Questionnaire, Application, Self-Certification TIC, Resident Affidavit/Certification, Asset Under 5,000 or 50,000 Form, Other
    rateOfPay: Optional[str] = None
    # What rateOfPay is per: hourly | daily | weekly | bi-weekly | semi-monthly |
    # monthly | quarterly | annually | per_period. Hours multiply only an
    # hourly rate; a salary is already annual.
    rateUnit: Optional[str] = None
    frequencyOfPay: Optional[str] = None
    hoursPerPayPeriod: Optional[str] = None
    overtimeRate: Optional[str] = None
    overtimeFrequency: Optional[str] = None
    ytdAmount: Optional[str] = None
    ytdStartDate: Optional[str] = None
    ytdEndDate: Optional[str] = None
    # A payment record's rows (child support ledger, agency payment history),
    # annualised by the calculator from what was actually paid.
    paymentHistory: list[PaymentHistoryRow] = []
    incomeType: Optional[str] = None
    type_of_VOI: Optional[str] = None
    address: Optional[Address] = None
    employmentStatus: Optional[str] = None  # Active, Terminated, On Leave
    terminationDate: Optional[str] = None
    hireDate: Optional[str] = None
    dateReceived: Optional[str] = None  # Date VOI was received/signed by employer
    # Provenance (see PayStubEntry) and the reconciliation verdict:
    #   verified              read from a third-party document
    #   declared_only         the household declared it (certification /
    #                         questionnaire) and no source document carries it
    #   verified_not_declared a source document carries it and the
    #                         certification's own income table does not
    sourcePages: list[int] = []
    evidence: dict[str, str] = {}
    verificationStatus: Optional[str] = None
    # The figure the certification's own income table declares for this
    # source, as printed, annualised only when the table's column is annual.
    declaredAnnualAmount: Optional[str] = None
    declaredSource: Optional[str] = None

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        if v is not None and not isinstance(v, (str, dict, list)):
            return str(v)
        return v


class SourceIncome(BaseModel):
    payStub: list[PayStubEntry] = []
    verificationIncome: list[VerificationIncomeEntry] = []


class DeclaredIncome(BaseModel):
    """One line of the household's own account of its income: a row of the
    certification's income table or a questionnaire disclosure. A
    declaration, never a verification; the extractor reconciles it against
    the records read from source documents."""
    memberName: Optional[str] = None
    memberNumber: Optional[str] = None
    sourceName: Optional[str] = None
    incomeType: Optional[str] = None
    amount: Optional[str] = None
    amountPeriod: Optional[str] = None   # annual | monthly | weekly | bi-weekly | per_period | unknown
    page: Optional[int] = None
    quote: Optional[str] = None
    documentType: Optional[str] = None
    matched: bool = False

    @field_validator("memberNumber", "amount", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        if v is not None and not isinstance(v, str):
            return str(v)
        return v


class IncomeExtraction(BaseModel):
    sourceIncome: SourceIncome = Field(default_factory=SourceIncome)
    declared: list[DeclaredIncome] = []


# ---------------------------------------------------------------------------
# MuleSoft Schema — Asset Extraction V4.0 (Section 22)
# ---------------------------------------------------------------------------

class BankStatementEntry(BaseModel):
    statementDate: Optional[str] = None
    balance: Optional[str] = None
    accountNumber: Optional[str] = None
    currentMortgageBalance: Optional[str] = None
    income: Optional[str] = None
    incomeFixedValue: Optional[str] = None
    incomeFromAsset: Optional[str] = None
    interestRate: Optional[str] = None
    netValueRealEstate: Optional[str] = None
    percentageOfOwnership: Optional[str] = None
    realEstateCurrentMarketValue: Optional[str] = None
    totalClosingCosts: Optional[str] = None

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        # A number where a string is expected is the value, not an error.
        if v is not None and not isinstance(v, (str, dict, list)):
            return str(v)
        return v


class MonthlyBalance(BaseModel):
    """One month's balance as a verification of assets lists it.

    Some banks (Chase among them) answer a VOA with the balance at the end
    of each of the last six months instead of a six-month average. The
    average is then computed at delivery, oldest month first, from these.
    """
    month: Optional[str] = None    # as printed: "2026-03", "03/2026", "March 2026", or "1".."6"
    balance: Optional[str] = None  # numeric string, 2 decimals

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        # The model returns a bare 1..6 for a VOA that numbers its months;
        # a number where a string is expected is the value, not an error.
        if v is not None and not isinstance(v, (str, dict, list)):
            return str(v)
        return v


class VerificationOfAsset(BaseModel):
    accountNumber: Optional[str] = None
    currentBalance: Optional[str] = None
    averageSixMonthBalance: Optional[str] = None
    monthlyBalances: list[MonthlyBalance] = []
    dateReceived: Optional[str] = None
    incomeAmount: Optional[str] = None
    interestType: Optional[str] = None
    interestRate: Optional[str] = None
    percentageOfOwnership: Optional[str] = None

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        # A number where a string is expected is the value, not an error.
        if v is not None and not isinstance(v, (str, dict, list)):
            return str(v)
        return v


class AssetEntry(BaseModel):
    documentType: Optional[str] = None
    assetOwner: Optional[str] = None
    socialSecurityNumber: Optional[str] = None
    sourceName: Optional[str] = None
    selfDeclaredAmount: Optional[str] = None
    selfDeclaredSource: Optional[str] = None  # Same picklist as VerificationIncomeEntry
    accountType: Optional[str] = None
    accountNumber: Optional[str] = None
    currentBalance: Optional[str] = None
    averageSixMonthBalance: Optional[str] = None
    dateReceived: Optional[str] = None
    incomeAmount: Optional[str] = None
    interestType: Optional[str] = None
    percentageOfOwnership: Optional[str] = None
    address: Optional[Address] = None
    bankStatment: list[BankStatementEntry] = []
    verificationOfAsset: Optional[VerificationOfAsset] = None
    # Provenance and reconciliation verdict (see VerificationIncomeEntry):
    #   verified       read from a statement / verification document
    #   declared_only  a self-certification or certification-form claim that
    #                  no statement in the packet backs
    sourcePages: list[int] = []
    evidence: dict[str, str] = {}
    verificationStatus: Optional[str] = None


class DeclaredAsset(BaseModel):
    """One asset the household itself declares: a certification-form asset
    row, a self-certification line, a questionnaire disclosure. A claim
    about an asset, not an asset; it is merged into the verified record it
    describes or kept as a claim when nothing backs it."""
    assetOwner: Optional[str] = None
    accountType: Optional[str] = None
    sourceName: Optional[str] = None
    accountNumber: Optional[str] = None
    amount: Optional[str] = None
    incomeAmount: Optional[str] = None
    kind: Optional[str] = None           # asset | no_assets | disposal
    page: Optional[int] = None
    quote: Optional[str] = None
    documentType: Optional[str] = None
    matched: bool = False

    @field_validator("accountNumber", "amount", "incomeAmount", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        if v is not None and not isinstance(v, str):
            return str(v)
        return v


class AssetExtraction(BaseModel):
    assetInformation: list[AssetEntry] = []
    declared: list[DeclaredAsset] = []


# ---------------------------------------------------------------------------
# MuleSoft Schema — Document Inventory (Sections 23 & 24)
# ---------------------------------------------------------------------------

class DocumentInventoryEntry(BaseModel):
    documentType: Optional[str] = None
    documentTitle: Optional[str] = None
    sourceOrganization: Optional[str] = None
    personName: Optional[str] = None
    pageRange: Optional[str] = None
    pageCount: int = 0
    isSigned: Optional[str] = None
    signedBy: Optional[str] = None
    signatureDate: Optional[str] = None
    documentDate: Optional[str] = None
    notes: Optional[str] = None


class DocumentInventory(BaseModel):
    documents: list[DocumentInventoryEntry] = []


# ---------------------------------------------------------------------------
# Income Calculation Results (Section 9)
# ---------------------------------------------------------------------------

class IncomeCalculationResult(BaseModel):
    """Result of one income calculation method for one source."""
    memberName: Optional[str] = None
    sourceName: Optional[str] = None
    incomeType: Optional[str] = None  # benefit program / income category from the VI record
    method: Optional[str] = None  # self-declared, voi-based, ytd-based, paystub-based, history-based
    annualIncome: Optional[str] = None  # numeric string, 2 decimals; None on a "[rejected]" row
    details: Optional[str] = None  # explanation; "[audit]" / "[historical]" / "[rejected]" prefixes


# ---------------------------------------------------------------------------
# Questionnaire Disclosures (Section 11 — Affirmative Response)
# ---------------------------------------------------------------------------

class QuestionnaireEmployment(BaseModel):
    """One employment block on an application: who, since when."""
    employer: Optional[str] = None
    start_date: Optional[str] = None   # YYYY-MM-DD as the applicant wrote it

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        if v is not None and not isinstance(v, str):
            return str(v)
        return v


class QuestionnaireDisclosures(BaseModel):
    """Yes/no disclosures extracted from application/questionnaire."""
    has_employment: Optional[bool] = None
    employers: list[str] = []
    # The application's employment section, block by block: the start date
    # is the basis for a year-to-date projection when the job began this year.
    employment: list[QuestionnaireEmployment] = []
    has_student_status: Optional[bool] = None
    has_ssa_benefits: Optional[bool] = None
    has_checking_account: Optional[bool] = None
    has_savings_account: Optional[bool] = None
    has_child_support: Optional[bool] = None
    has_pension: Optional[bool] = None
    has_self_employment: Optional[bool] = None
    has_other_income: Optional[bool] = None
    has_real_estate: Optional[bool] = None
    has_life_insurance: Optional[bool] = None


# ---------------------------------------------------------------------------
# Combined Pipeline Output
# ---------------------------------------------------------------------------

class PreviousCertIncomeSource(BaseModel):
    """One income source from the previous certification."""
    incomeType: Optional[str] = None
    sourceName: Optional[str] = None
    memberName: Optional[str] = None
    annualAmount: Optional[str] = None

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        if v is not None and not isinstance(v, (str, dict, list)):
            return str(v)
        return v


class PreviousCertification(BaseModel):
    """Previous certification data for IR delta comparison."""
    effectiveDate: Optional[str] = None
    certificationType: Optional[str] = None
    householdIncome: Optional[str] = None
    tenantRent: Optional[str] = None
    grossRent: Optional[str] = None
    utilityAllowance: Optional[str] = None
    householdSize: Optional[str] = None
    income_by_source: list[PreviousCertIncomeSource] = []
    source_pages: list[int] = []

    @field_validator(
        "effectiveDate", "certificationType", "householdIncome",
        "tenantRent", "grossRent", "utilityAllowance", "householdSize",
        mode="before",
    )
    @classmethod
    def coerce_str_fields(cls, v):
        # source_pages is list[int] and must NOT be coerced — only the
        # string-shaped fields here.
        if v is not None and not isinstance(v, (str, dict, list)):
            return str(v)
        return v


class Finding(BaseModel):
    """One audit finding, carrying the context needed to place it downstream.

    Findings were plain strings until the Cartograph integration, which needs
    them attached to a subject (a member, an income record, an asset) with a
    stable identity so a re-audit updates a finding instead of duplicating it
    and so reviewer resolutions survive.

    `text` is the full human-readable string and remains the canonical wording
    — the string list on ExtractionResult is rendered from it, so existing
    consumers see exactly what they saw before.
    """
    code: str                                   # stable type, e.g. "SSA_AS_PAYSTUB_AND_VOI"
    text: str                                   # full finding wording (backward-compatible)
    category: str = "file_review"               # unit_rent | household_member | income |
                                                # asset | expense | file_review
    label: Optional[str] = None                 # short title, when one is worth separating
    subject_type: Optional[str] = None          # household_member | income_record |
                                                # asset_record | expense_record | None = case
    subject_ref: dict = Field(default_factory=dict)   # {member_name, source_name, ...}
    result: str = "non_compliant"               # compliant | non_compliant | na
    assignment: Optional[str] = None            # internal | client | procedural_issue
    correction_required: Optional[str] = None
    resolution_type: Optional[str] = None       # presence_only | recalculation
    # True when the finding reports the extraction contradicting the document's
    # own account of itself — a declared total that the extracted sources do
    # not sum to, a figure on the certification that matches no record, methods
    # that disagree. Such a finding is evidence about the RELIABILITY of the
    # values involved, not only about the household, so the scorer reads it.
    #
    # A missing-document finding is deliberately not one of these: the file is
    # incomplete, but nothing says the extraction misread what is there.
    disputes_extraction: bool = False
    confidence: Optional[float] = None
    pages: list[int] = []
    finding_key: Optional[str] = None           # derived; see build_finding_key

    @field_validator("pages", mode="before")
    @classmethod
    def coerce_pages(cls, v):
        if v is None:
            return []
        return [int(p) for p in v if str(p).isdigit() or isinstance(p, int)]


class PageOcrRecord(BaseModel):
    """Per-page OCR provenance persisted for post-hoc diagnosis.

    Without this, a review of "why did extraction miss field X" cannot
    distinguish an OCR failure (value never reached the LLM) from an
    extraction failure (value was in the text and the LLM skipped it) —
    OCR is nondeterministic, so re-running it later proves nothing about
    what the original run saw.
    """
    page: int
    flag: Optional[str] = None       # green | yellow | red
    score: Optional[float] = None    # composite quality score
    chars: int = 0
    flags: list[str] = []            # blank_page, vision_fallback, suspected_content_loss, ...
    text: Optional[str] = None       # the sanitized text extraction actually consumed


class ExtractionResult(BaseModel):
    """Complete pipeline output combining all MuleSoft schemas."""
    classification: ClassificationResult
    document_groups: list[DocumentGroup]
    household_demographics: HouseholdDemographics
    certification_info: Optional[CertificationInfo] = None
    previous_certification: Optional[PreviousCertification] = None
    income: IncomeExtraction
    assets: AssetExtraction
    document_inventory_financial: DocumentInventory
    document_inventory_hud: DocumentInventory
    income_calculations: list[IncomeCalculationResult] = []
    questionnaire_disclosures: Optional[QuestionnaireDisclosures] = None
    findings: list[str] = []
    # Structured form of `findings`. Populated for emitters that have been
    # migrated; `findings` stays the rendered string list for every consumer
    # that predates the Cartograph integration.
    finding_records: list[Finding] = []
    field_scores: Optional["ExtractionScoreSummary"] = None
    page_ocr: list[PageOcrRecord] = []


# Deferred import to avoid circular dependency
from app.schemas.scoring import ExtractionScoreSummary  # noqa: E402

ExtractionResult.model_rebuild()
