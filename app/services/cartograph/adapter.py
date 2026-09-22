"""Turn an ExtractionResult into a Cartograph ingest payload.

This is the piece between "the engine finished an audit" and "Cartograph
received it". Signing and delivery already exist; nothing built the body.

Scope: the envelope, the target, `cert_review`, `household_members`,
`income_records` (with paystubs nested), and `asset_records` (with bank
statements and the verification of assets nested).

Not yet carried:

  - `expense_records`. The engine does not extract expenses at all, so the
    section would be empty rather than incomplete. It stays absent until
    extraction exists, because an empty array asserts "no expenses" while
    absence asserts nothing.

  - `findings`. Each one has to arrive attached to a subject with a stable
    key, and only two of the seven emitting modules produce that shape so
    far. Sending the migrated minority would look like a short finding list
    rather than a partial migration.

Two rules the mapping follows, both of which matter more than they look:

  - **Unknown is not false.** The engine records `disabled` and `student` as
    "Y", "N", or None, where None means the packet never said. Sending false
    for None would assert something the audit does not know, and "disability
    field null when a cert doc exists" is one of the pipeline's own findings.
    Unknown is transmitted as null.

  - **SSNs leave as last four digits only.** CertHouseholdMember#ssn is a
    plaintext column on Cartograph with no `encrypts` declaration, so writing
    full digits there would be a downgrade in handling. Nothing in this module
    can emit more than four digits.

Anything the mapping cannot carry is reported in `AdapterResult.warnings`
rather than dropped silently — a payload that quietly loses a date is
indistinguishable from a packet that never had one.
"""

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.config import Settings
from app.schemas.extraction import ExtractionResult, HouseholdMember
from app.services.income_calculator import match_paystubs_to_sources, normalize_rate_unit
from app.services.members import is_unborn
from app.services.name_reconciler import _name_similarity

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.3"

# Below this, two names are different people rather than two spellings of
# one. Deliberately strict: attaching an income record to the wrong member
# is worse than leaving it unattached, because an unattached record is
# reported as a warning while a misattached one looks correct.
_NAME_MATCH_THRESHOLD = 0.72

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _normalize_term(value: str) -> str:
    """Reduce a vocabulary term to a comparable form.

    'Non-Federal Wage', 'non federal wages' and 'NON_FEDERAL_WAGE' are the
    same term written three ways. Matching on the normalized form means the
    engine does not need a lookup entry per spelling, which is what makes the
    mapping survive documents it has not seen.
    """
    slug = _SLUG_RE.sub("_", str(value).strip().lower()).strip("_")
    # Trailing plural: 'wages' and 'wage' are one term.
    return slug[:-1] if slug.endswith("s") and not slug.endswith("ss") else slug

# The engine's four certification types and Cartograph's names for them.
# AR-SC is a distinct value on both sides: it applies its own rule set here
# (the certification form is the source of truth, so no third-party wage
# verification is expected) and Cartograph carries ar_self_cert to match.
_CERT_TYPE_OUT = {
    "MI": "initial",
    "AR": "annual",
    "AR-SC": "ar_self_cert",
    "IR": "interim",
}

# The same mapping read backwards, for the cert type arriving on a
# notification. Without it the consumer's vocabulary reaches the extraction
# pipeline unchanged, where "annual" matches none of the engine's types and
# every cert-type rule silently stops applying.
_CERT_TYPE_IN = {value: key for key, value in _CERT_TYPE_OUT.items()}

# Values the extractor uses for these fields. "H" marks head of household;
# disabled and student are Y/N. See field_scorer's business rules.
_HEAD_OF_HOUSEHOLD = "H"
_YES = "Y"
_NO = "N"

# Date formats seen in extracted certification packets, most specific first.
# OCR returns whatever the form printed, so a single format is not enough.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m/%d/%y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %B %Y",
)


@dataclass
class AdapterResult:
    """A payload plus everything the mapping could not carry.

    Warnings are the engine's own view of what it lost, distinct from the
    warnings Cartograph returns on the import callback about what *it* could
    not store. Both are needed: they describe different ends of the trip.
    """
    payload: dict
    warnings: list[str] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        return len(self.payload.get("household_members", []))


def _tri_state(value: str | None) -> bool | None:
    """Map the extractor's Y/N/None onto true/false/null.

    None is preserved rather than collapsed to false: the difference between
    "not disabled" and "the packet never said" is exactly what a reviewer
    needs to see.
    """
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized in (_YES, "YES", "TRUE"):
        return True
    if normalized in (_NO, "NO", "FALSE"):
        return False
    return None


def _ssn_last4(value: str | None) -> str | None:
    """Last four digits of an SSN, whether it arrived full or masked.

    Works on '123-45-6789' and on '***-**-6789' alike, and cannot return more
    than four characters regardless of input.
    """
    if not value:
        return None
    digits = re.findall(r"\d", str(value))
    if len(digits) < 4:
        return None
    return "".join(digits[-4:])


def _iso_date(value: str | None) -> str | None:
    """Normalize a date to YYYY-MM-DD, or None if no format matches."""
    if not value:
        return None
    raw = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _full_name(member: HouseholdMember) -> str:
    return " ".join(
        p for p in (member.FirstName, member.LastName) if p
    ).strip()


def _member_ref(index: int) -> str:
    """Payload-scoped handle for a member, e.g. 'm01'.

    Income, asset, and expense records reference members by this handle, so it
    only has to be unique within one request. It carries no meaning outside
    the payload and is not persisted.
    """
    return f"m{index + 1:02d}"


# Cartograph's relationship picklist, and how the forms' own words map onto
# it. The HUD 50059 prints codes (D for dependent, K for co-head) and the
# TIC prints whatever the manager wrote (Son, Daughter, Child, Grandchild);
# Cartograph has one word for every child under eighteen. Age decides the
# child/adult split when the form's word does not.
_RELATIONSHIP_HEAD = "Head of Household"
_RELATIONSHIP_OUT = {
    "head": _RELATIONSHIP_HEAD, "head of household": _RELATIONSHIP_HEAD, "hoh": _RELATIONSHIP_HEAD,
    "spouse": "Spouse", "wife": "Spouse", "husband": "Spouse",
    "co-head": "Co-Head", "cohead": "Co-Head", "co head": "Co-Head", "k": "Co-Head",
    "live-in aide": "Live-in Aide", "live in aide": "Live-in Aide", "aide": "Live-in Aide", "l": "Live-in Aide",
    "foster child": "Foster Child", "foster": "Foster Child", "f": "Foster Child",
    "foster adult": "Other Adult",
    "unborn child": "Unborn", "unborn": "Unborn", "expected child": "Unborn",
    "other adult": "Other Adult", "adult": "Other Adult", "o": "Other Adult",
}
_CHILD_WORDS = ("dependent", "child", "son", "daughter", "grandson", "granddaughter", "grandchild",
                "minor", "stepson", "stepdaughter", "stepchild", "niece", "nephew", "d", "c")
_ADULT_RELATIVE_WORDS = ("mother", "father", "parent", "brother", "sister", "sibling", "grandmother",
                         "grandfather", "grandparent", "aunt", "uncle", "cousin", "partner", "friend",
                         "roommate", "in-law", "relative", "other")


def _age_at(dob: str | None, on: str | None) -> float | None:
    from datetime import date
    try:
        y, m, d = (int(x) for x in (dob or "").split("-"))
        born = date(y, m, d)
    except (TypeError, ValueError):
        return None
    try:
        y, m, d = (int(x) for x in (on or "").split("-"))
        ref = date(y, m, d)
    except (TypeError, ValueError):
        ref = date.today()
    return (ref - born).days / 365.25


def relationship_out(printed: str | None, dob: str | None, effective_date: str | None) -> str | None:
    """The Cartograph relationship for a member, from the form's word and age.

    "Dependent", "Son", "Daughter" and "Child" are "Minor Child" under
    eighteen and "Other Adult" from eighteen; a child relationship with no
    date of birth is taken as a minor, which is what the word means on the
    forms. Words Cartograph has no value for ("Mother", "Friend") are
    adults. The form's own word travels beside it as relationship_as_printed.
    """
    key = (printed or "").strip().lower().rstrip(".")
    if not key:
        return None
    if key in _RELATIONSHIP_OUT:
        return _RELATIONSHIP_OUT[key]
    age = _age_at(dob, effective_date)
    if any(key == w or key.startswith(w + " ") or key.endswith(" " + w) for w in _CHILD_WORDS):
        if age is not None and age >= 18:
            return "Other Adult"
        return "Minor Child"
    if any(w in key for w in _ADULT_RELATIVE_WORDS):
        return "Minor Child" if age is not None and age < 18 else "Other Adult"
    return None


def build_household_members(
    extraction: ExtractionResult,
    warnings: list[str],
) -> list[dict]:
    """Map the household roster onto the ingest contract's member records."""
    members: list[dict] = []

    for index, member in enumerate(extraction.household_demographics.houseHold):
        name = _full_name(member)
        if not name:
            # Nothing to import and nothing to reference it by. Reported
            # rather than dropped, because a nameless roster entry usually
            # means a misread page, not an empty row.
            warnings.append(
                f"household_members[{index}] has no name; omitted from payload"
            )
            continue

        dob = _iso_date(member.DOB)
        if member.DOB and not dob:
            warnings.append(
                f"household_members[{index}].date_of_birth could not be parsed "
                f"(value: {member.DOB!r}); sent as null"
            )

        is_hoh = (member.head or "").strip().upper() == _HEAD_OF_HOUSEHOLD

        record = {
            "ref": _member_ref(index),
            "first_name": member.FirstName,
            "last_name": member.LastName,
            "middle_name": member.MiddleName,
            "date_of_birth": dob,
            "is_hoh": is_hoh,
            "is_disabled": _tri_state(member.disabled),
            "full_time_student": _tri_state(member.student),
            "ssn_last4": _ssn_last4(member.socialSecurityNumber),
            "email": member.email,
            "cell_phone": member.phone,
            "sort_order": index + 1,
        }

        # The certification states each member's relationship to the head
        # and the extractor now reads it. Head of household is still asserted
        # from the roster position, which is authoritative for that one
        # value; everyone else carries what the form says, and the warning
        # is reserved for a member the form genuinely left blank.
        effective = extraction.certification_info.effectiveDate if extraction.certification_info else None
        unborn = is_unborn(member)
        record["member_status"] = "Unborn" if unborn else "Active"
        record["relationship_as_printed"] = member.relationship
        if is_hoh:
            record["relationship"] = _RELATIONSHIP_HEAD
        elif unborn:
            record["relationship"] = "Unborn"
        elif member.relationship:
            mapped = relationship_out(member.relationship, member.DOB, effective)
            if mapped:
                record["relationship"] = mapped
            else:
                record["relationship"] = member.relationship
                warnings.append(
                    f"household_members[{index}].relationship {member.relationship!r} has no "
                    f"Cartograph equivalent; sent as printed"
                )
        else:
            warnings.append(
                f"household_members[{index}].relationship not extracted; left unset"
            )

        members.append(record)

    if not members:
        warnings.append("no household members extracted from this packet")

    return members


def cert_type_from_cartograph(value: str | None) -> str | None:
    """Translate an arriving certification type into the engine's vocabulary.

    Cartograph names these initial, annual, interim and ar_self_cert; the
    engine calls them MI, AR, AR-SC and IR, and every cert-type rule keys on
    its own names. Passing the consumer's value straight through means those
    rules stop applying — an AR-SC packet labelled "annual" is audited as an
    ordinary recertification, so the exemptions that expect no third-party
    wage verification never fire and the file collects false findings about
    documents it is not supposed to have.

    An unrecognized value returns None rather than a guess. None lets the
    engine determine the type from the documents; a wrong override silently
    replaces what the forms say.
    """
    if not value:
        return None
    return _CERT_TYPE_IN.get(str(value).strip().lower())


def _cert_type_out(value: str | None, warnings: list[str]) -> str | None:
    """Translate the certification type for delivery, or omit it.

    Recognized in either vocabulary: the engine's own names, and Cartograph's
    if a value reaches here already translated. Recognizing the consumer's
    term is not a guess — it is the same table read the other way — and it
    keeps a correct value correct instead of reporting it as unknown.

    Anything else is omitted rather than defaulted. This field used to fall
    back to 'annual', which is the most common type and therefore the most
    convincing wrong answer: cert_type selects the checklist template through
    `cert_type_scope`, so a move-in delivered as an annual is audited against
    the wrong rule set and reads as a clean result. A missing value is
    visible and recoverable; a plausible one is neither.

    The same reasoning as the classifier's unknown labels and the employment
    status that held prose — report, never invent.
    """
    raw = (value or "").strip()
    if not raw:
        warnings.append(
            "cert_review.cert_type omitted: no certification type was "
            "extracted, so the checklist template cannot be selected"
        )
        return None

    mapped = _CERT_TYPE_OUT.get(raw.upper())
    if mapped:
        return mapped

    if raw.lower() in _CERT_TYPE_IN:
        # Already in Cartograph's vocabulary — recognized, not guessed.
        return raw.lower()

    warnings.append(
        f"cert_review.cert_type omitted: '{value}' is not one of "
        f"MI, AR, AR-SC or IR, and the wrong type selects the wrong "
        f"checklist template"
    )
    return None


def _money(value: str | None) -> str | None:
    """Normalize a money string to plain digits, or None if unparseable.

    Extraction returns whatever the form printed — '$2,095.43', '2095.43',
    sometimes a bare float coerced to str. Cartograph's decimal columns
    reject the formatted forms.
    """
    if value is None or str(value).strip() == "":
        return None
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return f"{float(cleaned):.2f}"
    except ValueError:
        return None


def _resolve_member_ref(
    name: str | None,
    members: list[dict],
    warnings: list[str],
    context: str,
) -> str | None:
    """Match a name on an income or asset record to a household member.

    Records name their owner in prose; the payload references members by the
    handle assigned in `household_members`. An unmatched record is still sent
    with a null ref and a warning — the money is real even when the engine
    cannot say whose it is, and dropping it would understate the household.
    """
    if not name or not members:
        return None

    best_ref, best_score = None, 0.0
    for member in members:
        candidate = " ".join(
            p for p in (member.get("first_name"), member.get("last_name")) if p
        )
        if not candidate:
            continue
        score = _name_similarity(name, candidate)
        if score > best_score:
            best_ref, best_score = member["ref"], score

    if best_score < _NAME_MATCH_THRESHOLD:
        warnings.append(
            f"{context}: '{name}' did not match any household member "
            f"(closest {best_score:.2f}); sent without a member reference"
        )
        return None
    return best_ref


# The only values this field is defined to hold. Anything else is prose that
# reached a status column.
_EMPLOYMENT_STATUSES = ("active", "terminated", "on leave")


def _employment_status(
    value: str | None,
    warnings: list[str],
    context: str,
) -> str | None:
    """Pass through a real employment status; drop anything else.

    The extractor occasionally writes an explanation into this field rather
    than a status — "Flagged — disclosed on questionnaire but no VOI/paystub
    found" appeared on a real case. Forwarded, that prose lands in a status
    column and every later reader treats it as a value, including the checks
    that ask whether employment was terminated.

    The note is real information, but it belongs in a finding, not in a field
    whose vocabulary is three words. Dropping it with a warning keeps the
    column meaningful and keeps the observation visible.
    """
    if not value:
        return None
    if any(status in value.strip().lower() for status in _EMPLOYMENT_STATUSES):
        return value
    warnings.append(
        f"{context}: employment_status held prose rather than a status "
        f"({value[:60]!r}); omitted"
    )
    return None


_ENGINE_SYNONYMS = {
    "supplemental_security_income": "ssi",
    "social_security_disability": "social_security",
    "direct_express": "prepaid_debit_card",
    "prepaid_card": "prepaid_debit_card",
    "debit_card": "prepaid_debit_card",
    "certificate_of_deposit": "cd",
    "certificates_of_deposit": "cd",
    "cash": "cash_on_hand",
    "life_insurance": "whole_life_insurance",
    "temporary_assistance": "public_assistance",
    "tanf": "public_assistance",
}


def _map_vocabulary(
    value: str | None,
    allowed: list[str],
    warnings: list[str],
    context: str,
    aliases: dict[str, str] | None = None,
) -> str:
    """Map an extracted type onto the consumer's picklist, or to 'other'.

    `allowed` is the consumer's own list of valid values, supplied as
    configuration rather than compiled in. The engine does not decide what
    Cartograph accepts, and a table of terms observed in whatever documents
    happened to be tested is a table that silently stops working on the next
    property, the next state, the next funding program.

    Matching is on the normalized form, so a term the consumer spells
    'non_federal_wages' matches an extraction reading 'Non-Federal Wage'
    without an entry per spelling.

    Anything unmatched collapses to 'other' and is reported. Reporting is the
    point: their validators reject unknown values, and assets are created
    with create!, so a guess surfaces as a 500 with nothing to debug. A
    warning naming the original converts their exception into our diagnostic,
    and the accumulated warnings are the list of terms to agree with them.
    """
    if not value:
        warnings.append(f"{context}: no type extracted; sent as 'other'")
        return "other"
    # The engine's own long names for terms the picklists abbreviate. These
    # are facts about our vocabulary, not the consumer's decisions, so they
    # live here; the configured aliases still override them.
    value = _ENGINE_SYNONYMS.get(_normalize_term(value), value)

    if not allowed:
        warnings.append(
            f"{context}: type '{value}' sent as 'other' — no picklist is "
            f"configured, so no value can be confirmed valid"
        )
        return "other"

    target = _normalize_term(value)

    # An alias wins over a spelling match. A term can normalize onto a value
    # the consumer still accepts but has moved away from, and matching on
    # spelling would keep sending the one they are retiring.
    aliased = (aliases or {}).get(target)
    if aliased:
        return aliased

    for candidate in allowed:
        if _normalize_term(candidate) == target:
            return candidate

    warnings.append(
        f"{context}: type '{value}' is not in the configured picklist; "
        f"collapsed to 'other'"
    )
    return "other"


def build_cert_review(
    extraction: ExtractionResult,
    members: list[dict],
    asset_records: list[dict],
    warnings: list[str],
) -> dict:
    """Map the certification form onto the cert_review record."""
    info = extraction.certification_info
    if info is None:
        warnings.append("no certification form extracted; cert_review omitted")
        return {}

    cert_type = _cert_type_out(info.certificationType, warnings)

    hoh = next(
        (m for m in members if m.get("is_hoh")),
        members[0] if members else None,
    )
    hoh_name = (
        " ".join(p for p in (hoh.get("first_name"), hoh.get("last_name")) if p)
        if hoh else None
    )

    # Summed from the records actually sent, not from the certification form,
    # which carries no asset total. Cartograph recomputes from its children
    # anyway; this is here so the two can be compared.
    asset_total = 0.0
    for record in asset_records:
        value = record.get("current_value") or record.get("manual_balance")
        if value:
            asset_total += float(value)

    return {
        "cert_type": cert_type,
        "effective_date": _iso_date(info.effectiveDate),
        "unit_number": info.unitNumber,
        "hh_size": len(members),
        "annual_income": _money(info.householdIncome),
        "annual_assets": f"{asset_total:.2f}" if asset_records else None,
        "head_of_household_name": hoh_name,
        "tenant_rent": _money(info.tenantRent),
        "gross_rent": _money(info.grossRent),
        "utility_allowance": _money(info.utilityAllowance),
        "max_program_rent": _money(info.rentLimit),
    }


def _calc_key(member: str | None, source: str | None) -> tuple[str, str]:
    return ((member or "").strip().lower(), (source or "").strip().lower())


_PAY_FREQUENCIES = ("weekly", "bi-weekly", "semi-monthly", "monthly", "quarterly")


def _frequency_out(entry, stubs: list | None = None) -> str | None:
    """How often the person is paid.

    The stubs say it best when there are any; otherwise the record's stated
    frequency, or the rate's own unit ($1,489.50 monthly is paid monthly).
    "Annually" is the period of a declared figure, not a pay frequency, and
    an hourly rate implies none.
    """
    from app.services.income_calculator import normalize_frequency
    for stub in stubs or []:
        freq = normalize_frequency(getattr(stub, "payInterval", None))
        if freq in _PAY_FREQUENCIES:
            return freq
    freq = normalize_frequency(entry.frequencyOfPay)
    if freq in _PAY_FREQUENCIES:
        return freq
    unit = normalize_rate_unit(getattr(entry, "rateUnit", None))
    if unit in _PAY_FREQUENCIES:
        return unit
    return None


def _calculations_by_source(extraction: ExtractionResult) -> dict[tuple[str, str], dict]:
    """The engine's annual figure per source, with how it was reached.

    One primary row per (member, source): the method that produced the
    figure and its arithmetic in words. The audit rows (a year-to-date
    projection beside a stub average) and rejected attempts travel as
    alternatives, each stating why it did not stand, so a reviewer sees the
    same comparison the findings were made from.
    """
    out: dict[tuple[str, str], dict] = {}
    for calc in extraction.income_calculations or []:
        key = _calc_key(calc.memberName, calc.sourceName)
        details = calc.details or ""
        status = ("audit" if details.startswith("[audit]") else
                  "rejected" if details.startswith("[rejected]") else
                  "historical" if details.startswith("[historical]") else "primary")
        row = {"method": calc.method, "annual_income": _money(calc.annualIncome),
               "status": status, "details": details}
        slot = out.setdefault(key, {"method": None, "annual_income": None, "details": None, "alternatives": []})
        if status == "primary" and slot["annual_income"] is None and row["annual_income"] is not None:
            slot.update(method=calc.method, annual_income=row["annual_income"], details=details)
        else:
            slot["alternatives"].append(row)
    return out


def build_income_records(
    extraction: ExtractionResult,
    members: list[dict],
    settings: Settings,
    warnings: list[str],
) -> list[dict]:
    """Map income sources, with their paystubs nested underneath.

    The engine extracts one entry per physical paystub; grouping them by
    member and employer happens here so the importer receives them already
    attached to the source they belong to.
    """
    source_income = extraction.income.sourceIncome
    entries = source_income.verificationIncome
    if not entries:
        if source_income.payStub:
            warnings.append(
                f"{len(source_income.payStub)} paystub(s) extracted with no "
                f"income source to attach them to; not sent"
            )
        return []

    grouped = match_paystubs_to_sources(source_income.payStub, entries)
    calculations = _calculations_by_source(extraction)

    records: list[dict] = []
    for index, entry in enumerate(entries):
        context = f"income_records[{index}]"
        member_ref = _resolve_member_ref(
            entry.memberName, members, warnings, context,
        )
        calc = calculations.get(_calc_key(entry.memberName, entry.sourceName))

        paystubs = [
            {
                "pay_date": _iso_date(stub.payDate),
                "gross_pay": _money(stub.grossPay),
                "ytd_amount": _money(stub.ytdGross),
                "pay_frequency": stub.payInterval,
                "pages": _pages_of(stub),
            }
            for stub in grouped.get(index, [])
        ]

        vois: list[dict] = []
        if entry.rateOfPay or entry.ytdAmount or entry.type_of_VOI:
            vois.append({
                "voi_type": entry.type_of_VOI,
                "date_received": _iso_date(entry.dateReceived),
                "rate_of_pay": _money(entry.rateOfPay),
                "rate_unit": normalize_rate_unit(entry.rateUnit),
                "hours_per_pay_period": entry.hoursPerPayPeriod,
                "frequency_of_pay": entry.frequencyOfPay,
                "ytd_amount": _money(entry.ytdAmount),
                "ytd_start_date": _iso_date(entry.ytdStartDate),
                "ytd_end_date": _iso_date(entry.ytdEndDate),
            })

        records.append({
            "member_ref": member_ref,
            "income_type": _map_vocabulary(
                entry.incomeType, settings.cartograph_income_types, warnings, context,
                aliases=settings.cartograph_type_aliases,
            ),
            "source_name": entry.sourceName,
            "frequency_of_pay": _frequency_out(entry, grouped.get(index, [])),
            "rate_unit": normalize_rate_unit(entry.rateUnit),
            "date_received": _iso_date(entry.dateReceived),
            "employment_start_date": _iso_date(entry.hireDate),
            "employment_status": _employment_status(
                entry.employmentStatus, warnings, context,
            ),
            "termination_date": _iso_date(entry.terminationDate),
            "self_declared_amount": _money(entry.selfDeclaredAmount),
            "source_of_declaration": entry.selfDeclaredSource,
            "verification_status": entry.verificationStatus,
            "pages": _pages_of(entry),
            "annual_income": calc["annual_income"] if calc else None,
            "calculation": calc,
            "payment_history": [
                {"date": _iso_date(row.date) if row.date else None, "amount": _money(row.amount)}
                for row in (entry.paymentHistory or [])
                if row.amount
            ],
            "paystubs": paystubs,
            "vois": vois,
            "zero_income": None,
        })

    # Paystubs left over after matching describe an employer the extractor
    # never raised a verification entry for. That happens: on this packet the
    # employer verification came back blank and the manager substituted
    # paystubs, so the source exists only as a stack of stubs.
    #
    # Building records solely from verificationIncome would drop the largest
    # income in the household while sending the small self-declared ones,
    # and Cartograph recomputes the total from what it receives. A source the
    # engine demonstrably knows about must not vanish because it is recorded
    # in one of the three income structures rather than another.
    matched = {id(stub) for group in grouped.values() for stub in group}
    orphans = [s for s in source_income.payStub if id(s) not in matched]
    if orphans:
        by_source: dict[tuple, list] = {}
        for stub in orphans:
            key = ((stub.memberName or "").strip(), (stub.sourceName or "").strip())
            by_source.setdefault(key, []).append(stub)

        for (member_name, source_name), stubs in by_source.items():
            context = f"income_records[{len(records)}]"
            warnings.append(
                f"{context}: '{source_name or 'unnamed employer'}' has "
                f"{len(stubs)} paystub(s) but no verification entry; record "
                f"reconstructed from the paystubs"
            )
            calc = calculations.get(_calc_key(member_name, source_name))
            records.append({
                "member_ref": _resolve_member_ref(
                    member_name, members, warnings, context,
                ),
                # No type is inferred from the fact that stubs exist. Which
                # kind of employment income this is depends on the employer
                # and the program, and a stub says neither — so it goes
                # through the same mapping as anything else and collapses to
                # 'other' with a warning, rather than arriving as a
                # determination nobody made.
                "income_type": _map_vocabulary(
                    None, settings.cartograph_income_types, warnings, context,
                    aliases=settings.cartograph_type_aliases,
                ),
                "source_name": source_name or None,
                "frequency_of_pay": next(
                    (s.payInterval for s in stubs if s.payInterval), None,
                ),
                "date_received": None,
                "employment_start_date": None,
                "employment_status": None,
                "termination_date": None,
                "self_declared_amount": None,
                "source_of_declaration": None,
                "verification_status": "verified",
                "pages": sorted({p for s in stubs for p in _pages_of(s)}),
                "rate_unit": None,
                "annual_income": calc["annual_income"] if calc else None,
                "calculation": calc,
                "payment_history": [],
                "paystubs": [
                    {
                        "pay_date": _iso_date(s.payDate),
                        "gross_pay": _money(s.grossPay),
                        "ytd_amount": _money(s.ytdGross),
                        "pay_frequency": s.payInterval,
                        "pages": _pages_of(s),
                    }
                    for s in stubs
                ],
                "vois": [],
                "zero_income": None,
            })

    return records


def build_asset_records(
    extraction: ExtractionResult,
    members: list[dict],
    settings: Settings,
    warnings: list[str],
) -> list[dict]:
    """Map assets, with statements and the verification of assets nested."""
    records: list[dict] = []

    for index, asset in enumerate(extraction.assets.assetInformation):
        context = f"asset_records[{index}]"
        member_ref = _resolve_member_ref(
            asset.assetOwner, members, warnings, context,
        )

        # A statement with neither a date nor a balance carries nothing and
        # would arrive as an empty row asserting that a statement exists.
        # That is worse than sending none: the checklist item asking whether
        # a statement was obtained would read as satisfied.
        statements = [
            entry for entry in (
                {
                    "statement_date": _iso_date(s.statementDate),
                    "balance": _money(s.balance),
                }
                for s in asset.bankStatment
            )
            if any(entry.values())
        ]

        voa = None
        if asset.verificationOfAsset is not None:
            v = asset.verificationOfAsset
            voa = {
                "voa_date": _iso_date(v.dateReceived),
                "reported_value": _money(v.currentBalance),
                "source": asset.sourceName,
            }

        # current_value asserts a third-party verified balance; manual_balance
        # asserts the resident's own figure. The extractor sometimes copies a
        # self-certified amount into currentBalance as well, which would send
        # a resident's declaration to Cartograph dressed as verification and
        # let it satisfy a checklist item that requires third-party proof.
        #
        # A balance counts as verified only when verification evidence came
        # with it: a bank statement or a verification of assets. Tested on the
        # presence of the evidence rather than on the name of the document
        # that carried it — document titles vary by state, by management
        # company and by form revision, so a title test passes or fails on
        # spelling rather than on whether anyone actually verified anything.
        verified = bool(asset.bankStatment or asset.verificationOfAsset)
        current_value = _money(asset.currentBalance) if verified else None
        if asset.currentBalance and not verified:
            warnings.append(
                f"{context}: balance arrived with no statement or "
                f"verification of assets behind it; sent as manual_balance "
                f"rather than as a verified value"
            )

        records.append({
            "member_ref": member_ref,
            "asset_type": _map_vocabulary(
                asset.accountType, settings.cartograph_asset_types, warnings, context,
                aliases=settings.cartograph_type_aliases,
            ),
            "institution_name": asset.sourceName,
            "current_value": current_value,
            "manual_balance": _money(
                asset.selfDeclaredAmount or asset.currentBalance
            ),
            "source_of_declaration": asset.selfDeclaredSource,
            "bank_stmt_avg_balance": _money(asset.averageSixMonthBalance),
            "annual_income_from_assets": _money(asset.incomeAmount),
            "interest_type": asset.interestType,
            "percentage_of_ownership": asset.percentageOfOwnership,
            "verification_status": asset.verificationStatus,
            "pages": _pages_of(asset),
            "bank_statements": statements,
            "voa": voa,
        })

    return records


def _pages_of(record) -> list[int]:
    """The packet pages a record was read from, as the extractor set them."""
    pages = getattr(record, "sourcePages", None) or []
    return sorted({int(p) for p in pages if p is not None})


# Per-field review notes ("[YELLOW] Randy Buck — Savings → incomeAmount:
# Review recommended") are the scorer's commentary, not findings: they
# have no subject a reviewer acts on and would fill the thread twenty rows
# deep. They stay out of the findings section.
_FIELD_NOTE_RE = re.compile(r"^\[(GREEN|YELLOW|RED)\]")
_DIGITS_RE = re.compile(r"[\d$,.]+")


def _note_key(text: str) -> str:
    """A stable key for a finding that is still plain text.

    Digits and amounts are stripped before hashing so a re-scan that reads
    a figure slightly differently updates the same row instead of opening
    a new one; two notes of the same shape about different people still
    differ, since the names stay in.
    """
    shape = _DIGITS_RE.sub("", text.lower())
    shape = re.sub(r"\s+", " ", shape).strip()
    return f"NOTE:{hashlib.sha1(shape.encode()).hexdigest()[:12]}"


def _subject_label(ref: dict) -> str | None:
    parts = [
        ref.get(k) for k in ("member_name", "source_name", "account_type", "document_type", "table", "field")
        if ref.get(k) and str(ref.get(k)).lower() not in ("unknown", "none")
    ]
    return " — ".join(str(p) for p in parts) or None


def build_findings(extraction: ExtractionResult, members: list[dict]) -> list[dict]:
    """Every finding of the audit, one object each, in the shape Cartograph's
    Scan Findings thread stores.

    Structured findings carry their derived key (code + subject), so a
    re-scan reporting the same finding updates the same row. Findings that
    are still plain strings get a hash key with code NOTE. Field-level
    review notes are not findings and are left out.
    """
    structured = list(getattr(extraction, "finding_records", None) or [])
    covered = {f.text for f in structured}
    out: list[dict] = []
    for f in structured:
        ref = dict(f.subject_ref or {})
        member_ref = _resolve_member_ref(ref.get("member_name"), members, [], "findings") if ref.get("member_name") else None
        out.append({
            "finding_key": f.finding_key,
            "code": f.code,
            "category": f.category,
            "result": f.result,
            "subject_type": f.subject_type,
            "subject_label": _subject_label(ref),
            "member_ref": member_ref,
            "pages": sorted({int(p) for p in (f.pages or [])}),
            "label": f.label,
            "description": f.text,
            "correction_required": f.correction_required,
            "assignment": f.assignment,
            "resolution_type": f.resolution_type,
            "disputes_extraction": bool(f.disputes_extraction),
        })
    for text in getattr(extraction, "findings", None) or []:
        if not text or text in covered or _FIELD_NOTE_RE.match(text):
            continue
        out.append({
            "finding_key": _note_key(text),
            "code": "NOTE",
            "category": "file_review",
            "result": "non_compliant",
            "subject_type": None,
            "subject_label": None,
            "member_ref": None,
            "pages": [],
            "label": None,
            "description": text,
            "correction_required": None,
            "assignment": None,
            "resolution_type": None,
            "disputes_extraction": False,
        })
    # One row per key: the dedupe upstream is by key too, but a NOTE whose
    # shape repeats (the same wording about two different pages) would
    # otherwise arrive twice and update one row twice.
    seen: set = set(); unique: list[dict] = []
    for row in out:
        if row["finding_key"] in seen:
            continue
        seen.add(row["finding_key"]); unique.append(row)
    return unique


def _confidence_of(card) -> dict:
    """The engine's confidence in one record: its score, its flag, and the
    fields a reviewer should look at with the reason for each."""
    review = [
        {"field": f.field_name, "flag": f.flag.value, "reason": f.flag_message}
        for f in card.fields if f.flag.value in ("yellow", "red")
    ]
    return {"score": round(card.composite, 3), "flag": card.flag.value, "review": review}


def attach_confidence(payload: dict, extraction: ExtractionResult) -> None:
    """Put each score card's confidence on the payload record it scored.

    Cards are built from the same lists the records are, in the same
    order: one per household member, one per income entry, one per asset,
    one for the certification. Income records reconstructed from orphan
    pay stubs have no card and get none. When the counts do not line up
    the cards are matched on their label instead, and a record no card
    matches is left without confidence rather than given another's.
    """
    scores = getattr(extraction, "field_scores", None)
    if not scores:
        return
    by_type: dict[str, list] = {}
    for card in scores.records:
        by_type.setdefault(card.record_type, []).append(card)

    def _assign(records: list[dict], cards: list, label_of) -> None:
        if not records or not cards:
            return
        if len(records) == len(cards):
            for rec, card in zip(records, cards):
                rec["confidence"] = _confidence_of(card)
            return
        by_label = {(c.record_label or "").lower(): c for c in cards}
        for rec in records:
            card = by_label.get(label_of(rec).lower())
            if card:
                rec["confidence"] = _confidence_of(card)

    members = payload.get("household_members") or []
    _assign(members, by_type.get("household_member", []),
            lambda m: " ".join(p for p in (m.get("first_name"), m.get("last_name")) if p))
    ref_name = {m.get("ref"): " ".join(p for p in (m.get("first_name"), m.get("last_name")) if p) for m in members}
    income = [r for r in (payload.get("income_records") or []) if r.get("vois") or r.get("self_declared_amount") is not None or not r.get("paystubs")]
    _assign(income, by_type.get("income", []),
            lambda r: f"{ref_name.get(r.get('member_ref'), '')} — {r.get('source_name') or ''}".strip(" —"))
    _assign(payload.get("asset_records") or [], by_type.get("asset", []),
            lambda r: f"{ref_name.get(r.get('member_ref'), '')} — {r.get('asset_type') or ''}".strip(" —"))
    cert_cards = by_type.get("certification", [])
    if cert_cards and isinstance(payload.get("cert_review"), dict):
        payload["cert_review"]["confidence"] = _confidence_of(cert_cards[0])
    payload["confidence"] = {"score": round(scores.overall_composite, 3), "flag": scores.overall_flag.value,
                             "fields": {"green": scores.green_fields, "yellow": scores.yellow_fields,
                                        "red": scores.red_fields, "na": scores.na_fields}}


def build_payload(
    extraction: ExtractionResult,
    settings: Settings,
    *,
    case_ref: str,
    job_id: int | None = None,
    community_id: int | None = None,
    unit_number: str | None = None,
    extraction_id: str | None = None,
    extracted_at: str | None = None,
) -> AdapterResult:
    """Build the ingest body for one audited case.

    `case_ref` is Cartograph's `Job#ref_number` and is the field their
    importer resolves against, so it is required even when the numeric ids
    are not yet known.
    """
    warnings: list[str] = []

    # Members first: income and asset records reference them by the handle
    # assigned here, so the roster has to exist before anything can point at
    # it. Assets before cert_review for the same reason — the asset total is
    # summed from the records actually sent.
    members = build_household_members(extraction, warnings)
    assets = build_asset_records(extraction, members, settings, warnings)
    income = build_income_records(extraction, members, settings, warnings)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "extraction_id": extraction_id or f"ext_{uuid.uuid4().hex}",
        "engine_version": f"idp-{settings.app_version}",
        "extracted_at": extracted_at or datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "case_ref": case_ref,
        "target": {
            "job_id": job_id,
            "community_id": community_id,
            "unit_number": unit_number,
        },
        "cert_review": build_cert_review(extraction, members, assets, warnings),
        "household_members": members,
        "income_records": income,
        "asset_records": assets,
        "findings": build_findings(extraction, members),
    }
    attach_confidence(payload, extraction)

    if warnings:
        logger.info(
            "Cartograph payload for case_ref=%s built with %d warning(s): %s",
            case_ref, len(warnings), warnings,
        )

    return AdapterResult(payload=payload, warnings=warnings)
