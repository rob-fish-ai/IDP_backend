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

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.config import Settings
from app.schemas.extraction import ExtractionResult, HouseholdMember
from app.services.income_calculator import match_paystubs_to_sources
from app.services.name_reconciler import _name_similarity

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.2"

# Below this, two names are different people rather than two spellings of
# one. Deliberately strict: attaching an income record to the wrong member
# is worse than leaving it unattached, because an unattached record is
# reported as a warning while a misattached one looks correct.
_NAME_MATCH_THRESHOLD = 0.72

# Cartograph validates these against its own picklists and creates asset
# records with create!, so an unrecognized value raises RecordInvalid rather
# than being stored. Only values confirmed against their schema are mapped;
# everything else collapses to "other" and is reported, which turns a 500
# on their side into a line in the warnings list on ours.
_INCOME_TYPES = {
    "non-federal wage": "non_federal_wages",
    "non federal wage": "non_federal_wages",
    "non-federal wages": "non_federal_wages",
}
_ASSET_TYPES = {
    "checking": "checking",
    "checking account": "checking",
    "savings": "savings",
    "savings account": "savings",
}

# CertReview::CERT_TYPES allows initial, annual and interim. The engine
# audits four types; AR-SC has no target, which is an open item on the
# schema change request. Until it is resolved AR-SC maps to annual and says
# so, rather than being silently indistinguishable from a plain AR.
_CERT_TYPES = {
    "MI": "initial",
    "AR": "annual",
    "AR-SC": "annual",
    "IR": "interim",
}

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

        # The engine does not extract a relationship other than head of
        # household — the roster gives a position, not a relation. Sending
        # the one value we do know beats sending nothing; the rest stays
        # absent rather than guessed.
        if is_hoh:
            record["relationship"] = "Head of Household"
        else:
            warnings.append(
                f"household_members[{index}].relationship not extracted; left unset"
            )

        members.append(record)

    if not members:
        warnings.append("no household members extracted from this packet")

    return members


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


def _map_vocabulary(
    value: str | None,
    table: dict[str, str],
    warnings: list[str],
    context: str,
) -> str:
    """Map an extracted type onto Cartograph's picklist, or to 'other'.

    Collapsing is reported every time. These warnings are the only record of
    where the engine's vocabulary is lossy, and the alternative — guessing at
    a value their validator rejects — surfaces as a 500 with no explanation.
    """
    if not value:
        warnings.append(f"{context}: no type extracted; sent as 'other'")
        return "other"
    mapped = table.get(str(value).strip().lower())
    if mapped:
        return mapped
    warnings.append(
        f"{context}: type '{value}' has no Cartograph equivalent; "
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

    raw_type = (info.certificationType or "").strip().upper()
    cert_type = _CERT_TYPES.get(raw_type)
    if cert_type is None:
        warnings.append(
            f"cert_review.cert_type '{info.certificationType}' is not one of "
            f"MI, AR, AR-SC or IR; sent as 'annual'"
        )
        cert_type = "annual"
    elif raw_type == "AR-SC":
        warnings.append(
            "cert_review.cert_type AR-SC sent as 'annual' — CERT_TYPES has no "
            "AR-SC value, so the self-certification distinction is lost"
        )

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


def build_income_records(
    extraction: ExtractionResult,
    members: list[dict],
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

    records: list[dict] = []
    for index, entry in enumerate(entries):
        context = f"income_records[{index}]"
        member_ref = _resolve_member_ref(
            entry.memberName, members, warnings, context,
        )

        paystubs = [
            {
                "pay_date": _iso_date(stub.payDate),
                "gross_pay": _money(stub.grossPay),
                "ytd_amount": _money(stub.ytdGross),
                "pay_frequency": stub.payInterval,
            }
            for stub in grouped.get(index, [])
        ]

        vois: list[dict] = []
        if entry.rateOfPay or entry.ytdAmount or entry.type_of_VOI:
            vois.append({
                "voi_type": entry.type_of_VOI,
                "date_received": _iso_date(entry.dateReceived),
                "rate_of_pay": _money(entry.rateOfPay),
                "hours_per_pay_period": entry.hoursPerPayPeriod,
                "frequency_of_pay": entry.frequencyOfPay,
                "ytd_amount": _money(entry.ytdAmount),
                "ytd_start_date": _iso_date(entry.ytdStartDate),
                "ytd_end_date": _iso_date(entry.ytdEndDate),
            })

        records.append({
            "member_ref": member_ref,
            "income_type": _map_vocabulary(
                entry.incomeType, _INCOME_TYPES, warnings, context,
            ),
            "source_name": entry.sourceName,
            "frequency_of_pay": entry.frequencyOfPay,
            "date_received": _iso_date(entry.dateReceived),
            "employment_start_date": _iso_date(entry.hireDate),
            "employment_status": entry.employmentStatus,
            "termination_date": _iso_date(entry.terminationDate),
            "self_declared_amount": _money(entry.selfDeclaredAmount),
            "source_of_declaration": entry.selfDeclaredSource,
            "paystubs": paystubs,
            "vois": vois,
            "zero_income": None,
        })

    attached = sum(len(r["paystubs"]) for r in records)
    if attached < len(source_income.payStub):
        warnings.append(
            f"{len(source_income.payStub) - attached} of "
            f"{len(source_income.payStub)} paystub(s) matched no income "
            f"source and were not sent"
        )

    return records


def build_asset_records(
    extraction: ExtractionResult,
    members: list[dict],
    warnings: list[str],
) -> list[dict]:
    """Map assets, with statements and the verification of assets nested."""
    records: list[dict] = []

    for index, asset in enumerate(extraction.assets.assetInformation):
        context = f"asset_records[{index}]"
        member_ref = _resolve_member_ref(
            asset.assetOwner, members, warnings, context,
        )

        statements = [
            {
                "statement_date": _iso_date(s.statementDate),
                "balance": _money(s.balance),
            }
            for s in asset.bankStatment
        ]

        voa = None
        if asset.verificationOfAsset is not None:
            v = asset.verificationOfAsset
            voa = {
                "voa_date": _iso_date(v.dateReceived),
                "reported_value": _money(v.currentBalance),
                "source": asset.sourceName,
            }

        # A self-certified asset carries its value in selfDeclaredAmount and
        # a verified one in currentBalance. Both are sent under the field
        # that describes what they are, so the importer can tell a verified
        # balance from a resident's own figure.
        records.append({
            "member_ref": member_ref,
            "asset_type": _map_vocabulary(
                asset.accountType, _ASSET_TYPES, warnings, context,
            ),
            "institution_name": asset.sourceName,
            "current_value": _money(asset.currentBalance),
            "manual_balance": _money(asset.selfDeclaredAmount),
            "source_of_declaration": asset.selfDeclaredSource,
            "bank_stmt_avg_balance": _money(asset.averageSixMonthBalance),
            "annual_income_from_assets": _money(asset.incomeAmount),
            "interest_type": asset.interestType,
            "percentage_of_ownership": asset.percentageOfOwnership,
            "bank_statements": statements,
            "voa": voa,
        })

    return records


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
    assets = build_asset_records(extraction, members, warnings)
    income = build_income_records(extraction, members, warnings)

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
    }

    if warnings:
        logger.info(
            "Cartograph payload for case_ref=%s built with %d warning(s): %s",
            case_ref, len(warnings), warnings,
        )

    return AdapterResult(payload=payload, warnings=warnings)
