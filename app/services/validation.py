"""Post-processing validation: SSN masking, Title Case, date formatting, monetary formatting."""

import re
import logging
from datetime import date

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSN Masking
# ---------------------------------------------------------------------------

_SSN_FULL_PATTERN = re.compile(r"\b(\d{3})-?(\d{2})-?(\d{4})\b")
_SSN_MASKED_PATTERN = re.compile(r"\*{3}-\*{2}-(\d{4})")

# An SSN as a document prints it: three-two-four digits, delimited from any
# other digits, with OCR's stray spaces around the dashes tolerated
# ("441- 66- 8882"). A phone number (3-3-4) or a ten-digit run never fits.
_SSN_SHAPED_RE = re.compile(r"(?<![\d*xX#])(\d{3})[\s-]*(\d{2})[\s-]*(\d{4})(?![\d-])")
# The masked forms: ***-**-1234, XXX-XX-1234, #####1234, *****1234.
_SSN_MASKED_SHAPED_RE = re.compile(
    r"(?:[*xX#•]{3}[\s-]*[*xX#•]{2}[\s-]*|[*xX#•]{5,}[\s-]*)(\d{4})(?!\d)"
)
# Only the last four, alone or after a label ("last 4: 1234", "SSN 1234").
_SSN_LAST4_RE = re.compile(
    r"^(?:(?:last\s*(?:four|4)(?:\s*digits)?|ssn|ss\s*#|ss\s*no\.?|social)\s*[:#\-]?\s*)?(\d{4})$",
    re.IGNORECASE,
)
_DATE_SHAPED_RE = re.compile(r"\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|\d{4}-\d{2}-\d{2}")
_NOT_A_VALUE = frozenset({"n/a", "na", "none", "null", "unknown", "not provided",
                          "not applicable", "-", "--", "—", "not shown", "not listed"})


def mask_ssn(value: str | None) -> str | None:
    """Mask SSN to ***-**-XXXX format. Returns None if no valid SSN found.

    Egress masking: this errs toward masking, so anything with four digits
    in an SSN-named field leaves as last-four. The strict reading of what
    is an SSN lives in normalize_ssn."""
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


def _ssn_parts_plausible(area: str, group: str, serial: str) -> bool:
    """The Social Security Administration never issues these."""
    if area in ("000", "666") or area.startswith("9"):
        return False
    if group == "00" or serial == "0000":
        return False
    return True


def normalize_ssn(value: str | None) -> str | None:
    """Normalize an SSN as captured, PRESERVING full digits when present.

    Accepts only SSN-shaped input: nine digits in three-two-four groups
    (dashes, spaces or nothing between them), the masked forms, or the
    last four alone. A date, a phone number, a case number or an account
    number in this field is not an SSN and returns None — the old reading
    turned "02/20/1959" into ***-**-1959.

    Extraction stores the SSN exactly as the document shows it — full nine
    digits formatted NNN-NN-NNNN when printed in full, the standard masked
    form otherwise. Full SSNs stay internal to the job store for compliance
    exports; every audit-facing surface (findings, Salesforce writeback,
    API responses) masks them at egress via mask_ssns_deep()."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _NOT_A_VALUE:
        return None
    if _DATE_SHAPED_RE.search(text):
        return None

    m = _SSN_SHAPED_RE.search(text)
    if m:
        area, group, serial = m.groups()
        if _ssn_parts_plausible(area, group, serial):
            return f"{area}-{group}-{serial}"
        logger.warning("normalize_ssn: %r is not an issuable SSN — dropped", text)
        return None

    m = _SSN_MASKED_SHAPED_RE.search(text)
    if m:
        return f"***-**-{m.group(1)}"

    m = _SSN_LAST4_RE.match(text)
    if m:
        return f"***-**-{m.group(1)}"

    logger.warning("normalize_ssn: %r is not SSN-shaped — dropped", text)
    return None


# Keys that hold SSN values across extraction results and MuleSoft
# snapshots. Matched by exact key name; see _SSN_IN_TEXT_PATTERN for the
# second ground, which catches SSNs that no key name marks.
_SSN_KEYS = frozenset({"socialSecurityNumber", "SSN__c"})

# An SSN written the way a document prints it. Used to mask SSNs that appear
# in free text rather than in a field named for them — the raw OCR of a
# certification form carries them in prose ("TENANT: ... SSN: 441-60-8888"),
# where no key name marks them.
#
# Deliberately only the dashed nine-digit form. A bare run of nine digits is
# just as likely to be an account or case number, and rewriting those would
# corrupt the very evidence a reviewer is reading.
_SSN_IN_TEXT_PATTERN = re.compile(r"\b\d{3}-\d{2}-(\d{4})\b")


def mask_ssns_deep(obj):
    """Deep-copy a JSON-ish structure with every SSN masked to last-4.

    Applied at audit-result egress (API responses, anything user-facing).
    The stored extraction keeps SSNs as captured; nothing that leaves the
    service does.

    Masks on two independent grounds, because either alone leaks:

      - the key names an SSN field, which covers structured extraction; and
      - the value looks like an SSN, which covers everything else. Raw OCR
        page text is the case that matters — it reproduces the certification
        verbatim, SSNs included, under a key called "text".

    Pydantic models are dumped rather than returned untouched. A model is
    neither a dict nor a list, so a recursive walk that only knows those two
    hands the whole object back unmasked; `process_pdf_full` returns its
    extraction as a model, so the entire structured result — every member
    and every asset — passed through this function unchanged. The response
    body is the same JSON either way, since the model is serialized on the
    way out regardless.
    """
    if hasattr(obj, "model_dump"):
        return mask_ssns_deep(obj.model_dump())
    if isinstance(obj, dict):
        return {
            k: (mask_ssn(v) if k in _SSN_KEYS and isinstance(v, str)
                else mask_ssns_deep(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_ssns_deep(v) for v in obj]
    if isinstance(obj, str):
        return _SSN_IN_TEXT_PATTERN.sub(r"***-**-\1", obj)
    return obj


# ---------------------------------------------------------------------------
# Title Case
# ---------------------------------------------------------------------------

# Tokens that keep their letter case whatever the rest of the name does:
# generational suffixes, entity types, credentials, and short codes that
# read as words when capitalised ("Ok" for OK, "Tbk" for TBK).
_CASED_TOKENS = {
    "jr": "Jr.", "jr.": "Jr.", "sr": "Sr.", "sr.": "Sr.", "ii": "II", "iii": "III", "iv": "IV",
    "llc": "LLC", "l.l.c.": "L.L.C.", "inc": "Inc.", "inc.": "Inc.", "llp": "LLP", "lp": "LP",
    "pllc": "PLLC", "pc": "PC", "p.c.": "P.C.", "ltd": "Ltd.", "ltd.": "Ltd.", "co": "Co.", "co.": "Co.",
    "corp": "Corp.", "corp.": "Corp.", "dba": "DBA", "d/b/a": "d/b/a", "usa": "USA", "us": "US",
    "u.s.": "U.S.", "md": "MD", "dds": "DDS", "cpa": "CPA", "phd": "PhD", "rn": "RN", "ssa": "SSA",
    "hud": "HUD", "dhs": "DHS", "va": "VA", "ymca": "YMCA", "ywca": "YWCA", "ups": "UPS",
    "usps": "USPS", "atm": "ATM", "irs": "IRS", "ssi": "SSI", "ssdi": "SSDI", "eiv": "EIV",
    "tanf": "TANF", "snap": "SNAP", "wic": "WIC", "lihtc": "LIHTC", "pha": "PHA", "dss": "DSS",
    "dcf": "DCF", "dhhs": "DHHS", "ocse": "OCSE", "ira": "IRA", "ach": "ACH", "eft": "EFT",
    "ibm": "IBM", "att": "ATT", "at&t": "AT&T", "ups": "UPS", "cvs": "CVS", "kfc": "KFC",
    "and": "and", "of": "of", "the": "the", "de": "de",
    "la": "la", "del": "del", "van": "van", "von": "von", "da": "da", "y": "y",
}
_ACRONYM_MAX = 4


def _cap_token(token: str) -> str:
    """Capitalise one token, keeping hyphen and apostrophe structure and the
    Mc/Mac/O' name patterns: "O'BRIEN" → "O'Brien", "MCDONALD" → "McDonald",
    "MARY-ANN" → "Mary-Ann"."""
    def _cap(seg: str) -> str:
        if not seg:
            return seg
        low = seg.lower()
        if low.startswith("mc") and len(low) > 3:
            return "Mc" + low[2:].capitalize()
        return low.capitalize()

    def _cap_apostrophes(seg: str) -> str:
        # "O'BRIEN" → "O'Brien", "D'ANGELO" → "D'Angelo"; a possessive or
        # contraction tail ("MCDONALD'S") stays lower.
        pieces = seg.split("'")
        return "'".join(
            _cap(part) if i == 0 or len(part) > 1 else part.lower()
            for i, part in enumerate(pieces)
        )

    return "-".join(_cap_apostrophes(seg) for seg in token.split("-"))


def to_title_case(name: str | None) -> str | None:
    """Convert a name to Title Case without destroying what was deliberate.

    A name printed entirely in capitals (the way forms print them) is
    title-cased token by token. A name that already mixes case is left as
    the writer had it except that all-lower tokens are capitalised, so
    "McDonald's LLC" and "ABC Trucking" survive, and short all-caps tokens
    (≤ 4 letters: LLC, TBK, OK) keep their case even inside an otherwise
    capitalised name. Suffixes and entity types take their conventional
    form; particles ("de", "la", "van") stay lower after the first word.
    """
    if name is None:
        return None
    text = str(name).strip()
    if not text:
        return None

    parts = text.split()
    alpha_parts = [p for p in parts if any(c.isalpha() for c in p)]
    all_caps = bool(alpha_parts) and all(p == p.upper() for p in alpha_parts)
    out: list[str] = []
    for i, part in enumerate(parts):
        key = part.lower()
        bare = key.strip(".,;:()")
        if bare in _CASED_TOKENS or key in _CASED_TOKENS:
            fixed = _CASED_TOKENS.get(key, _CASED_TOKENS.get(bare))
            if i == 0 and fixed in ("and", "of", "the", "de", "la", "del", "van", "von", "da", "y"):
                fixed = fixed.capitalize()
            # keep any trailing punctuation the token had ("Inc.," → "Inc.,")
            trail = part[len(part.rstrip(".,;:)")):]
            if trail and not fixed.endswith(trail):
                fixed = fixed + trail
            out.append(fixed)
            continue
        letters = "".join(c for c in part if c.isalpha())
        if not letters:
            out.append(part)
            continue
        if part == part.upper():
            # A short all-caps token is an acronym when the rest of the name
            # is cased text ("ABC Trucking"), or when it has no vowel and so
            # cannot be a word ("TBK BANK", "OKDHS CSS") — in an all-caps
            # name a short token with a vowel is a word (ANNA, LEE, RAY).
            if len(letters) <= _ACRONYM_MAX and (
                not all_caps or not any(c in "AEIOUY" for c in letters)
            ):
                out.append(part)
            else:
                out.append(_cap_token(part))
        elif part == part.lower():
            out.append(_cap_token(part))
        else:
            out.append(part)   # already mixed case — the writer's choice
    return " ".join(out)


# ---------------------------------------------------------------------------
# Date Formatting
# ---------------------------------------------------------------------------

_DATE_YMD = re.compile(r"^(\d{4})[/.\-](\d{1,3})[/.\-](\d{1,3})(?:[T ].*)?$")
_DATE_MDY = re.compile(r"^(\d{1,3})[/.\-](\d{1,3})[/.\-](\d{2}|\d{4})$")
_DATE_TEXT_MDY = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")
_DATE_TEXT_DMY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?[\s\-,]+(\d{4})$")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTHS.update({"sept": 9})
_YEAR_MIN, _YEAR_MAX = 1900, 2100


def _pivot_year(yy: int) -> int:
    """Two-digit year → four. Dates on these documents are birth dates and
    signature dates: nothing later than next year, so "26" is 2026 and "49"
    is 1949 (pivot at the current year + 1)."""
    pivot = (date.today().year + 1) % 100
    return 2000 + yy if yy <= pivot else 1900 + yy


def _part_candidates(part: str) -> list[int]:
    """Readings of a day or month part, tolerating one OCR-inserted digit
    ("071" for 07, "115" for 15): each way of dropping one digit, the
    reading most of them agree on first."""
    if len(part) <= 2:
        return [int(part)]
    if len(part) != 3:
        return [int(part)]
    readings = [int(part[:2]), int(part[1:]), int(part[0] + part[2:])]
    ordered = sorted(set(readings), key=lambda r: (-readings.count(r), readings.index(r)))
    return ordered


def _calendar_date(year: int, month: int, day: int) -> str | None:
    if not (_YEAR_MIN <= year <= _YEAR_MAX):
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _resolve_md(year: int, first: str, second: str, month_first: bool) -> str | None:
    """Resolve two numeric parts as month/day (or day/month), trying the
    stated order first and the swapped order only when the stated order
    is impossible (a month over 12)."""
    firsts, seconds = _part_candidates(first), _part_candidates(second)
    orders = [(True, False)] if month_first else [(False, True)]
    orders.append((not orders[0][0], not orders[0][1]))
    for as_month_first, _ in orders:
        for a in firsts:
            for b in seconds:
                month, day = (a, b) if as_month_first else (b, a)
                iso = _calendar_date(year, month, day)
                if iso:
                    return iso
    return None


def normalize_date(value) -> str | None:
    """Normalize a date to YYYY-MM-DD, or None when the text is not a date.

    Accepts ISO (with or without a time), M/D/Y with a two- or four-digit
    year, dotted and dashed variants, and written months ("July 15, 1949",
    "15 Jul 1949"). Two-digit years pivot at next year; an OCR-doubled digit
    in a part is tolerated ("071/15/1949"); a day that does not exist in its
    month (2020-02-30) is rejected rather than accepted; month/day are
    swapped only when the printed order is impossible.
    """
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).strip())
    if not text or text.lower() in _NOT_A_VALUE:
        return None

    m = _DATE_YMD.match(text)
    if m:
        return _resolve_md(int(m.group(1)), m.group(2), m.group(3), month_first=True)

    m = _DATE_MDY.match(text)
    if m:
        year_text = m.group(3)
        year = _pivot_year(int(year_text)) if len(year_text) == 2 else int(year_text)
        return _resolve_md(year, m.group(1), m.group(2), month_first=True)

    m = _DATE_TEXT_MDY.match(text)
    if m:
        month = _MONTHS.get(m.group(1).lower()[:4]) or _MONTHS.get(m.group(1).lower()[:3])
        if month:
            return _calendar_date(int(m.group(3)), month, int(m.group(2)))
        return None

    m = _DATE_TEXT_DMY.match(text)
    if m:
        month = _MONTHS.get(m.group(2).lower()[:4]) or _MONTHS.get(m.group(2).lower()[:3])
        if month:
            return _calendar_date(int(m.group(3)), month, int(m.group(1)))
        return None

    logger.debug("normalize_date: %r is not a date — dropped", text)
    return None


def _is_valid_date(year: int, month: int, day: int) -> bool:
    """Whether the calendar has this date (kept for callers of the old name)."""
    return _calendar_date(year, month, day) is not None


# ---------------------------------------------------------------------------
# Monetary Formatting
# ---------------------------------------------------------------------------

_MONEY_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def normalize_money(value, field: str | None = None) -> str | None:
    """Normalize a monetary amount to a string with two decimals, no symbols.

    Returns None — never the input — when the text is not one amount:
    "N/A", a date, a range ("1,200 - 1,500"), a percentage, or prose with
    several numbers. Accepts what documents print: "$1,489.50", "1 489.50"
    (OCR spaces), "(50.00)" and "-50" (negative), "1489.50/mo" (a unit
    after the figure). The rejection is logged with the field name so the
    gap is visible in the run log.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    text = str(value).strip()
    if not text or text.lower() in _NOT_A_VALUE:
        return None
    if "%" in text or _DATE_SHAPED_RE.search(text):
        logger.warning("normalize_money%s: %r is not an amount — dropped", f"[{field}]" if field else "", text)
        return None

    negative = bool(re.match(r"^\(.*\)$", text)) or bool(re.match(r"^[-−]\s*\$?|^\$\s*[-−]", text))
    # Remove currency marks, grouping commas and the spaces OCR drops into
    # a figure ("1, 489.50"), then read the single number that is left.
    cleaned = re.sub(r"[()$€£\s]|USD|usd", "", text).replace(",", "").lstrip("-−")
    numbers = _MONEY_NUMBER_RE.findall(cleaned)
    if len(numbers) != 1:
        logger.warning("normalize_money%s: %r is not one amount — dropped", f"[{field}]" if field else "", text)
        return None
    remainder = cleaned.replace(numbers[0], "", 1)
    if len(remainder) > 12 or re.search(r"\d", remainder):
        logger.warning("normalize_money%s: %r is not an amount — dropped", f"[{field}]" if field else "", text)
        return None
    amount = float(numbers[0])
    if negative:
        amount = -amount
    return f"{amount:.2f}"


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

    # Conflicting identity values across documents are resolved and
    # reported by app.services.identity, which sees every page.
    return data


def validate_certification_info(data: dict) -> dict:
    """Apply validation rules to certification info output."""
    ci = data.get("certificationInfo", {})
    if not ci:
        return data

    ci["effectiveDate"] = normalize_date(ci.get("effectiveDate"))
    ci["moveInDate"] = normalize_date(ci.get("moveInDate"))
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


def _canonical_frequency(value) -> str | None:
    """The canonical spelling of a pay frequency; unknown text is kept
    lowercased so the scorer can still show what the document said."""
    if not value:
        return None
    from app.services.income_calculator import FREQUENCY_MULTIPLIERS, normalize_frequency
    canon = normalize_frequency(value)
    if canon in FREQUENCY_MULTIPLIERS:
        return canon
    return str(value).strip().lower() or None


def validate_income(data: dict) -> dict:
    """Apply validation rules to income extraction output."""
    si = data.get("sourceIncome", {})

    for stub in si.get("payStub", []):
        stub["memberName"] = to_title_case(stub.get("memberName"))
        stub["sourceName"] = to_title_case(stub.get("sourceName"))
        stub["socialSecurityNumber"] = normalize_ssn(stub.get("socialSecurityNumber"))
        stub["grossPay"] = normalize_money(stub.get("grossPay"))
        stub["payDate"] = normalize_date(stub.get("payDate"))
        stub["payInterval"] = _canonical_frequency(stub.get("payInterval"))

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
        # Frequencies in one vocabulary. "Hourly" is a rate unit, not a pay
        # frequency: when it arrives as the frequency it moves to rateUnit.
        from app.services.income_calculator import normalize_frequency, normalize_rate_unit
        for key in ("frequencyOfPay", "overtimeFrequency"):
            canon = normalize_frequency(vi.get(key))
            if canon in ("hourly", "daily"):
                if not vi.get("rateUnit"):
                    vi["rateUnit"] = canon
                vi[key] = None
            else:
                vi[key] = _canonical_frequency(vi.get(key))
        vi["rateUnit"] = normalize_rate_unit(vi.get("rateUnit"))
        rows = vi.get("paymentHistory")
        clean_rows = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            amount = normalize_money(row.get("amount"))
            if amount is None:
                continue
            raw_date = row.get("date")
            clean_rows.append({
                "date": normalize_date(raw_date) or (str(raw_date).strip() if raw_date else None),
                "amount": amount,
            })
        vi["paymentHistory"] = clean_rows

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
                           "social security disability", "pension", "veterans benefits"):
            vi["ytdAmount"] = None

    # Remove empty entries
    si["payStub"] = [
        s for s in si.get("payStub", [])
        if s.get("sourceName") or s.get("memberName") or s.get("grossPay")
    ]
    si["verificationIncome"] = [
        v for v in si.get("verificationIncome", [])
        if v.get("sourceName") or v.get("memberName") or v.get("rateOfPay")
        or v.get("selfDeclaredAmount") or v.get("ytdAmount") or v.get("paymentHistory")
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
            for mb in voa.get("monthlyBalances") or []:
                if isinstance(mb, dict):
                    mb["balance"] = normalize_money(mb.get("balance"))

    return data
