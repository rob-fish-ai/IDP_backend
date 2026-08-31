"""Turn an ExtractionResult into a Cartograph ingest payload.

This is the piece between "the engine finished an audit" and "Cartograph
received it". Signing and delivery already exist; nothing built the body.

Scope: the first slice agreed with Cartograph is files in, household members
back. `build_payload` therefore emits the envelope, the target, and
`household_members`, and leaves the income, asset, expense, and finding
sections for the passes that follow. The envelope is the full v1.2 shape, so
adding a section later is additive rather than a rewrite.

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

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.2"

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
        "household_members": build_household_members(extraction, warnings),
    }

    if warnings:
        logger.info(
            "Cartograph payload for case_ref=%s built with %d warning(s): %s",
            case_ref, len(warnings), warnings,
        )

    return AdapterResult(payload=payload, warnings=warnings)
