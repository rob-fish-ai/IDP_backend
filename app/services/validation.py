"""Post-processing validation: SSN masking, Title Case, date formatting, monetary formatting."""

import re
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSN Masking
# ---------------------------------------------------------------------------

_SSN_FULL_PATTERN = re.compile(r"\b(\d{3})-?(\d{2})-?(\d{4})\b")
_SSN_MASKED_PATTERN = re.compile(r"\*{3}-\*{2}-(\d{4})")


def mask_ssn(value: str | None) -> str | None:
    """Mask SSN to ***-**-XXXX format. Returns None if no valid SSN found."""
    if not value:
        return None

    # Already properly masked
    m = _SSN_MASKED_PATTERN.search(value)
    if m:
        return f"***-**-{m.group(1)}"

    # Full SSN visible — mask it
    m = _SSN_FULL_PATTERN.search(value)
    if m:
        return f"***-**-{m.group(3)}"

    # Partial — just last 4 digits
    digits = re.findall(r"\d", value)
    if len(digits) >= 4:
        last4 = "".join(digits[-4:])
        return f"***-**-{last4}"

    return None


def normalize_ssn(value: str | None) -> str | None:
    """Normalize an SSN as captured, PRESERVING full digits when present.

    Extraction stores the SSN exactly as the document shows it — full nine
    digits formatted NNN-NN-NNNN when printed in full, the standard masked
    form otherwise. Full SSNs stay internal to the job store for compliance
    exports; every audit-facing surface (findings, Salesforce writeback,
    API responses) masks them at egress via mask_ssns_deep()."""
    if not value:
        return None

    m = _SSN_FULL_PATTERN.search(value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    return mask_ssn(value)


# Keys that hold SSN values across extraction results and MuleSoft
# snapshots. mask_ssns_deep matches on exact key name.
_SSN_KEYS = frozenset({"socialSecurityNumber", "SSN__c"})


def mask_ssns_deep(obj):
    """Deep-copy a JSON-ish structure with every SSN field masked to last-4.

    Applied at audit-result egress (API responses, anything user-facing).
    The stored extraction keeps SSNs as captured; nothing that leaves the
    service does."""
    if isinstance(obj, dict):
        return {
            k: (mask_ssn(v) if k in _SSN_KEYS and isinstance(v, str) else mask_ssns_deep(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_ssns_deep(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Title Case
# ---------------------------------------------------------------------------

_LOWERCASE_WORDS = {"of", "the", "and", "in", "for", "to", "a", "an"}


def to_title_case(name: str | None) -> str | None:
    """Convert a name string to Title Case, preserving suffixes and hyphens."""
    if not name:
        return None

    parts = name.strip().split()
    result = []
    for i, part in enumerate(parts):
        # Handle hyphenated names
        if "-" in part:
            part = "-".join(seg.capitalize() for seg in part.split("-"))
            result.append(part)
        elif part.upper() in ("JR.", "SR.", "II", "III", "IV"):
            result.append(part.upper() if len(part) <= 3 else part.capitalize())
        else:
            result.append(part.capitalize())
    return " ".join(result)


# ---------------------------------------------------------------------------
# Date Formatting
# ---------------------------------------------------------------------------

_DATE_MDY_SLASH = re.compile(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$")
_DATE_YMD = re.compile(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$")


def normalize_date(value: str | None) -> str | None:
    """Normalize date to YYYY-MM-DD format.

    Returns None for invalid dates (month > 12, day > 31, etc.)
    to prevent garbled OCR dates from contaminating extraction.
    """
    if not value:
        return None

    value = value.strip()

    # Already YYYY-MM-DD format
    m = _DATE_YMD.match(value)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _is_valid_date(year, month, day):
            return f"{year}-{month:02d}-{day:02d}"
        return None  # Invalid date like 2026-21-01

    # MM/DD/YYYY or MM-DD-YYYY
    m = _DATE_MDY_SLASH.match(value)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _is_valid_date(year, month, day):
            return f"{year}-{month:02d}-{day:02d}"
        # Try swapping month/day (common OCR issue: DD/MM/YYYY vs MM/DD/YYYY)
        if _is_valid_date(year, day, month):
            return f"{year}-{day:02d}-{month:02d}"
        return None  # Garbled date

    return None  # Unrecognized format — return None, not raw string


def _is_valid_date(year: int, month: int, day: int) -> bool:
    """Check if a date has valid ranges."""
    if year < 1900 or year > 2100:
        return False
    if month < 1 or month > 12:
        return False
    if day < 1 or day > 31:
        return False
    return True


# ---------------------------------------------------------------------------
# Monetary Formatting
# ---------------------------------------------------------------------------

def normalize_money(value: str | None) -> str | None:
    """Normalize monetary amount to string with 2 decimal places, no symbols."""
    if not value:
        return None

    cleaned = value.strip().replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None

    try:
        amount = float(cleaned)
        return f"{amount:.2f}"
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# Phone Formatting
# ---------------------------------------------------------------------------

def normalize_phone(value: str | None) -> str | None:
    """Normalize phone to (XXX) XXX-XXXX format."""
    if not value:
        return None

    digits = re.findall(r"\d", value)

    # Strip leading country code
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]

    if len(digits) != 10:
        return None

    return f"({''.join(digits[:3])}) {''.join(digits[3:6])}-{''.join(digits[6:])}"


# ---------------------------------------------------------------------------
# Apply validation to full extraction results
# ---------------------------------------------------------------------------

def validate_household(data: dict) -> dict:
    """Apply validation rules to household demographics output."""
    for member in data.get("houseHold", []):
        member["FirstName"] = to_title_case(member.get("FirstName"))
        member["MiddleName"] = to_title_case(member.get("MiddleName"))
        member["LastName"] = to_title_case(member.get("LastName"))
        member["socialSecurityNumber"] = normalize_ssn(member.get("socialSecurityNumber"))
        member["DOB"] = normalize_date(member.get("DOB"))
        member["phone"] = normalize_phone(member.get("phone"))

        # Ensure head is only "H" or null
        head = member.get("head")
        if head and head.upper() not in ("H",):
            member["head"] = None

    # Ensure at most one head
    heads = [m for m in data.get("houseHold", []) if m.get("head") == "H"]
    if len(heads) > 1:
        for h in heads[1:]:
            h["head"] = None

    # DOB discrepancy detection: flag members with conflicting DOBs across documents
    members = data.get("houseHold", [])
    dob_by_name: dict[str, str] = {}
    discrepancies = []
    for member in members:
        name_key = f"{(member.get('FirstName') or '').lower()} {(member.get('LastName') or '').lower()}".strip()
        if not name_key:
            continue
        dob = member.get("DOB")
        if dob and name_key in dob_by_name and dob_by_name[name_key] != dob:
            discrepancies.append({
                "name": f"{member.get('FirstName', '')} {member.get('LastName', '')}".strip(),
                "dob_1": dob_by_name[name_key],
                "dob_2": dob,
            })
        elif dob:
            dob_by_name[name_key] = dob

    data["_dob_discrepancies"] = discrepancies

    return data


def validate_certification_info(data: dict) -> dict:
    """Apply validation rules to certification info output."""
    ci = data.get("certificationInfo", {})
    if not ci:
        return data

    ci["effectiveDate"] = normalize_date(ci.get("effectiveDate"))
    ci["signatureDate"] = normalize_date(ci.get("signatureDate"))
    ci["applicationSignDate"] = normalize_date(ci.get("applicationSignDate"))
    ci["grossRent"] = normalize_money(ci.get("grossRent"))
    ci["tenantRent"] = normalize_money(ci.get("tenantRent"))
    ci["utilityAllowance"] = normalize_money(ci.get("utilityAllowance"))
    ci["rentLimit"] = normalize_money(ci.get("rentLimit"))
    ci["householdIncome"] = normalize_money(ci.get("householdIncome"))

    # Normalize cert type
    cert_type = (ci.get("certificationType") or "").strip().upper()
    valid_types = {"MI", "IC", "AR", "AR-SC", "IR"}
    if cert_type == "IC":
        cert_type = "MI"  # IC is synonymous with MI per Section 12
    if cert_type not in valid_types:
        ci["certificationType"] = None
    else:
        ci["certificationType"] = cert_type

    return data


def validate_income(data: dict) -> dict:
    """Apply validation rules to income extraction output."""
    si = data.get("sourceIncome", {})

    for stub in si.get("payStub", []):
        stub["memberName"] = to_title_case(stub.get("memberName"))
        stub["sourceName"] = to_title_case(stub.get("sourceName"))
        stub["socialSecurityNumber"] = normalize_ssn(stub.get("socialSecurityNumber"))
        stub["grossPay"] = normalize_money(stub.get("grossPay"))
        stub["payDate"] = normalize_date(stub.get("payDate"))
        if stub.get("payInterval"):
            stub["payInterval"] = stub["payInterval"].lower()

    for vi in si.get("verificationIncome", []):
        vi["memberName"] = to_title_case(vi.get("memberName"))
        vi["sourceName"] = to_title_case(vi.get("sourceName"))
        vi["socialSecurityNumber"] = normalize_ssn(vi.get("socialSecurityNumber"))
        vi["rateOfPay"] = normalize_money(vi.get("rateOfPay"))
        vi["selfDeclaredAmount"] = normalize_money(vi.get("selfDeclaredAmount"))
        vi["ytdAmount"] = normalize_money(vi.get("ytdAmount"))
        vi["ytdStartDate"] = normalize_date(vi.get("ytdStartDate"))
        vi["ytdEndDate"] = normalize_date(vi.get("ytdEndDate"))
        vi["overtimeRate"] = normalize_money(vi.get("overtimeRate"))
        if vi.get("frequencyOfPay"):
            vi["frequencyOfPay"] = vi["frequencyOfPay"].lower()
        if vi.get("overtimeFrequency"):
            vi["overtimeFrequency"] = vi["overtimeFrequency"].lower()

        # Normalize employment status
        status = (vi.get("employmentStatus") or "").strip().lower()
        if status in ("active", "currently employed", "yes"):
            vi["employmentStatus"] = "Active"
        elif status in ("terminated", "no", "no longer employed", "separated"):
            vi["employmentStatus"] = "Terminated"
        elif status in ("on leave", "leave"):
            vi["employmentStatus"] = "On Leave"
        elif not status:
            vi["employmentStatus"] = None

        vi["terminationDate"] = normalize_date(vi.get("terminationDate"))
        vi["hireDate"] = normalize_date(vi.get("hireDate"))
        vi["dateReceived"] = normalize_date(vi.get("dateReceived"))

        # SSA / fixed income must NOT have YTD
        income_type = (vi.get("incomeType") or "").lower()
        if income_type in ("social security", "supplemental security income",
                           "social security disability", "pension"):
            vi["ytdAmount"] = None

    # Remove empty entries
    si["payStub"] = [
        s for s in si.get("payStub", [])
        if s.get("sourceName") or s.get("memberName") or s.get("grossPay")
    ]
    si["verificationIncome"] = [
        v for v in si.get("verificationIncome", [])
        if v.get("sourceName") or v.get("memberName") or v.get("rateOfPay")
        or v.get("selfDeclaredAmount") or v.get("ytdAmount")
    ]

    # Enforce Equifax/Work Number 6-paystub limit (Section 3)
    si["payStub"] = _enforce_source_limit(si["payStub"], _EQUIFAX_KEYWORDS, 6)

    # Enforce child support 6-payment limit (Section 3)
    si["payStub"] = _enforce_source_limit(si["payStub"], _CHILD_SUPPORT_KEYWORDS, 6)

    return data


# Keyword sets for source-specific paystub limits
_EQUIFAX_KEYWORDS = ("equifax", "work number", "screeningworks", "vault verify")
_CHILD_SUPPORT_KEYWORDS = ("child support",)


def _enforce_source_limit(
    stubs: list[dict],
    source_keywords: tuple[str, ...],
    max_count: int,
) -> list[dict]:
    """Keep only the most recent N paystubs for sources matching keywords."""
    matched = []
    other = []
    for s in stubs:
        source = (s.get("sourceName") or "").lower()
        if any(kw in source for kw in source_keywords):
            matched.append(s)
        else:
            other.append(s)

    if len(matched) <= max_count:
        return stubs

    # Sort by payDate descending, keep top N
    matched.sort(key=lambda s: s.get("payDate") or "", reverse=True)
    return other + matched[:max_count]


def validate_assets(data: dict) -> dict:
    """Apply validation rules to asset extraction output."""
    for asset in data.get("assetInformation", []):
        asset["assetOwner"] = to_title_case(asset.get("assetOwner"))
        asset["sourceName"] = to_title_case(asset.get("sourceName"))
        asset["socialSecurityNumber"] = normalize_ssn(asset.get("socialSecurityNumber"))
        asset["currentBalance"] = normalize_money(asset.get("currentBalance"))
        asset["averageSixMonthBalance"] = normalize_money(asset.get("averageSixMonthBalance"))
        asset["selfDeclaredAmount"] = normalize_money(asset.get("selfDeclaredAmount"))
        asset["incomeAmount"] = normalize_money(asset.get("incomeAmount"))
        asset["dateReceived"] = normalize_date(asset.get("dateReceived"))

        for bs in asset.get("bankStatment", []):
            bs["balance"] = normalize_money(bs.get("balance"))
            bs["statementDate"] = normalize_date(bs.get("statementDate"))
            bs["currentMortgageBalance"] = normalize_money(bs.get("currentMortgageBalance"))
            bs["income"] = normalize_money(bs.get("income"))
            bs["incomeFixedValue"] = normalize_money(bs.get("incomeFixedValue"))
            bs["incomeFromAsset"] = normalize_money(bs.get("incomeFromAsset"))
            bs["interestRate"] = normalize_money(bs.get("interestRate"))
            bs["netValueRealEstate"] = normalize_money(bs.get("netValueRealEstate"))
            bs["realEstateCurrentMarketValue"] = normalize_money(bs.get("realEstateCurrentMarketValue"))
            bs["totalClosingCosts"] = normalize_money(bs.get("totalClosingCosts"))

        voa = asset.get("verificationOfAsset")
        if voa:
            voa["currentBalance"] = normalize_money(voa.get("currentBalance"))
            voa["averageSixMonthBalance"] = normalize_money(voa.get("averageSixMonthBalance"))
            voa["incomeAmount"] = normalize_money(voa.get("incomeAmount"))
            voa["dateReceived"] = normalize_date(voa.get("dateReceived"))

    return data


def validate_document_inventory(data: dict) -> dict:
    """Apply validation rules to document inventory output."""
    for doc in data.get("documents", []):
        doc["personName"] = to_title_case(doc.get("personName"))
        doc["signedBy"] = to_title_case(doc.get("signedBy"))
        doc["signatureDate"] = normalize_date(doc.get("signatureDate"))
        doc["documentDate"] = normalize_date(doc.get("documentDate"))
    return data
