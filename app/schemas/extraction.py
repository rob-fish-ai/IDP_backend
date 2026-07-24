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
    head: Optional[str] = None
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
    numberOfBedrooms: Optional[str] = None
    grossRent: Optional[str] = None
    tenantRent: Optional[str] = None
    utilityAllowance: Optional[str] = None
    rentLimit: Optional[str] = None
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
    grossPay: Optional[str] = None
    payDate: Optional[str] = None
    payInterval: Optional[str] = None
    ytdGross: Optional[str] = None


class Address(BaseModel):
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None


class VerificationIncomeEntry(BaseModel):
    sourceName: Optional[str] = None
    memberName: Optional[str] = None
    socialSecurityNumber: Optional[str] = None
    programName: Optional[str] = None
    selfDeclaredAmount: Optional[str] = None
    selfDeclaredSource: Optional[str] = None  # Questionnaire, Application, Self-Certification TIC, Resident Affidavit/Certification, Asset Under 5,000 or 50,000 Form, Other
    rateOfPay: Optional[str] = None
    frequencyOfPay: Optional[str] = None
    hoursPerPayPeriod: Optional[str] = None
    overtimeRate: Optional[str] = None
    overtimeFrequency: Optional[str] = None
    ytdAmount: Optional[str] = None
    ytdStartDate: Optional[str] = None
    ytdEndDate: Optional[str] = None
    incomeType: Optional[str] = None
    type_of_VOI: Optional[str] = None
    address: Optional[Address] = None
    employmentStatus: Optional[str] = None  # Active, Terminated, On Leave
    terminationDate: Optional[str] = None
    hireDate: Optional[str] = None
    dateReceived: Optional[str] = None  # Date VOI was received/signed by employer

    @field_validator("*", mode="before")
    @classmethod
    def coerce_to_str(cls, v):
        if v is not None and not isinstance(v, (str, dict, list)):
            return str(v)
        return v


class SourceIncome(BaseModel):
    payStub: list[PayStubEntry] = []
    verificationIncome: list[VerificationIncomeEntry] = []


class IncomeExtraction(BaseModel):
    sourceIncome: SourceIncome = Field(default_factory=SourceIncome)


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


class VerificationOfAsset(BaseModel):
    accountNumber: Optional[str] = None
    currentBalance: Optional[str] = None
    averageSixMonthBalance: Optional[str] = None
    dateReceived: Optional[str] = None
    incomeAmount: Optional[str] = None
    interestType: Optional[str] = None
    interestRate: Optional[str] = None
    percentageOfOwnership: Optional[str] = None


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


class AssetExtraction(BaseModel):
    assetInformation: list[AssetEntry] = []


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
    method: Optional[str] = None  # self-declared, voi-based, ytd-based, paystub-based
    annualIncome: Optional[str] = None  # numeric string, 2 decimals
    details: Optional[str] = None  # explanation of calculation


# ---------------------------------------------------------------------------
# Questionnaire Disclosures (Section 11 — Affirmative Response)
# ---------------------------------------------------------------------------

class QuestionnaireDisclosures(BaseModel):
    """Yes/no disclosures extracted from application/questionnaire."""
    has_employment: Optional[bool] = None
    employers: list[str] = []
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
    field_scores: Optional["ExtractionScoreSummary"] = None
    page_ocr: list[PageOcrRecord] = []


# Deferred import to avoid circular dependency
from app.schemas.scoring import ExtractionScoreSummary  # noqa: E402

ExtractionResult.model_rebuild()
