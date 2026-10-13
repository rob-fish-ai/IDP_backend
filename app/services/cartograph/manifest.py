"""Link what the packet says to the records Cartograph already holds.

Cartograph sends, with each case, a manifest of the household members,
income records and asset records it already has for the case. A payload
row that corresponds to one of them carries that row's id back
(`existing_member_id` and `person_id` on a member, `existing_record_id` on
an income or asset record), and Cartograph updates the row in place,
filling blanks only, instead of clearing its scan rows and recreating
them. A row sent without an id is created as before, so a missed link
costs a duplicate for staff to merge; a wrong link writes one person's
figures onto another's record. The matching is therefore strict: a link
needs identity evidence (the date of birth, or a name close enough on
its own), and a candidate that two payload rows both claim goes to the
better claim only.

A member whose name on the packet is not one Cartograph has for the
person (its first/last name or any of its `aka_names`) is sent with the
packet's spelling in `aka_names`; Cartograph queues it as a suggestion
for staff to accept or dismiss, never adds it directly.
"""

from __future__ import annotations

import logging
import re

from app.services.name_reconciler import _name_similarity

logger = logging.getLogger(__name__)

# A name alone links a member only when it is this close to one of the
# names Cartograph has for the person: the same threshold the payload uses
# to attach an income record to a member, chosen so that two spellings of
# one name pass and two people do not.
_NAME_ALONE_THRESHOLD = 0.72
# With the date of birth agreeing, the name has only to be recognisably
# the same person — a surname changed by marriage or a hyphen dropped.
_NAME_WITH_DOB_THRESHOLD = 0.45
# With the dates of birth disagreeing, only a name this close links — and
# only when nothing else claims either side. One of the two dates was
# misread or mistyped (1956-02-13 against 1958-02-13 on the same Joyce
# Perkins, the household's one member), and that is reported; it is not
# evidence of a second person of the same name in the household.
_NAME_OVER_DOB_THRESHOLD = 0.9
# A source name links a record to one of several of the same type only
# when it is this close; below it, the type alone decides when the type
# is unique and nothing decides when it is not.
_SOURCE_THRESHOLD = 0.6

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value) -> str:
    return _SLUG_RE.sub("_", str(value or "").strip().lower()).strip("_")


def normalise_manifest(raw) -> dict[str, list[dict]]:
    """The manifest as three lists, whatever shape it arrived in."""
    out = {"household_members": [], "income_records": [], "asset_records": []}
    if not isinstance(raw, dict):
        return out
    for key in out:
        rows = raw.get(key)
        if isinstance(rows, list):
            out[key] = [r for r in rows if isinstance(r, dict) and r.get("id") is not None]
    return out


def _int_or_none(value):
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _manifest_names(row: dict) -> list[str]:
    """Every name Cartograph has for a manifest member."""
    names = []
    full = " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p).strip()
    if full:
        names.append(full)
    for aka in row.get("aka_names") or []:
        if isinstance(aka, str) and aka.strip():
            names.append(aka.strip())
        elif isinstance(aka, dict):
            text = aka.get("name") or " ".join(p for p in (aka.get("first_name"), aka.get("last_name")) if p)
            if text.strip():
                names.append(text.strip())
    return names


def _payload_name(member: dict) -> str:
    return " ".join(p for p in (member.get("first_name"), member.get("last_name")) if p).strip()


def _best_name_score(name: str, row: dict) -> float:
    if not name:
        return 0.0
    return max((_name_similarity(name, other) for other in _manifest_names(row)), default=0.0)


def link_members(members: list[dict], manifest: dict, warnings: list[str]) -> dict[str, int]:
    """Set existing_member_id, person_id and aka_names on each payload member.

    Returns {ref: existing_member_id} for the records to key on. Every
    member leaves with the three keys present (null / null / []) so the
    contract is the same whether or not a manifest came.
    """
    rows = manifest.get("household_members") or []
    for m in members:
        m.setdefault("existing_member_id", None)
        m.setdefault("person_id", None)
        m.setdefault("aka_names", [])
    if not rows or not members:
        return {}

    # Score every pair, then award each manifest row to its best claimant.
    # Tier 2: the date of birth agrees and the name is recognisable.
    # Tier 1: no date to compare and the name is close. Tier 0: the dates
    # disagree and the name is near-identical.
    claims: list[tuple[int, float, int, int]] = []
    for i, m in enumerate(members):
        name = _payload_name(m)
        for j, row in enumerate(rows):
            score = _best_name_score(name, row)
            dob_ours, dob_theirs = m.get("date_of_birth"), row.get("date_of_birth")
            same_dob = bool(dob_ours and dob_theirs and str(dob_ours)[:10] == str(dob_theirs)[:10])
            dob_conflict = bool(dob_ours and dob_theirs and not same_dob)
            if same_dob and score >= _NAME_WITH_DOB_THRESHOLD:
                claims.append((2, score, i, j))
            elif not dob_conflict and score >= _NAME_ALONE_THRESHOLD:
                claims.append((1, score, i, j))
            elif dob_conflict and score >= _NAME_OVER_DOB_THRESHOLD:
                claims.append((0, score, i, j))
    claims.sort(reverse=True)

    linked: dict[str, int] = {}
    taken_members: set[int] = set()
    taken_rows: set[int] = set()
    for tier, _score, i, j in claims:
        if i in taken_members or j in taken_rows:
            continue
        m, row = members[i], rows[j]
        if tier == 0:
            # A name-over-date link: refused when either side has any
            # other claim at all, since the date is the one thing that
            # tells two people of one name apart.
            if any((ii == i or jj == j) and (ii, jj) != (i, j) for _t, _s, ii, jj in claims):
                continue
            warnings.append(
                f"household_members[{i}] '{_payload_name(m)}' date of birth read as "
                f"{m.get('date_of_birth')}; Cartograph holds {row.get('date_of_birth')} for the "
                f"same name — linked on the name, date to be confirmed"
            )
        taken_members.add(i)
        taken_rows.add(j)
        m["existing_member_id"] = _int_or_none(row.get("id"))
        m["person_id"] = _int_or_none(row.get("person_id"))
        linked[m["ref"]] = m["existing_member_id"]
        ours = _payload_name(m)
        known = {_slug(n) for n in _manifest_names(row)}
        if ours and _slug(ours) not in known:
            m["aka_names"] = [ours]
            logger.info("Member %s read as %r, known to Cartograph as %s; sent as an aka suggestion",
                        m["ref"], ours, _manifest_names(row))

    for i, m in enumerate(members):
        if i not in taken_members:
            warnings.append(
                f"household_members[{i}] '{_payload_name(m)}' matches none of the {len(rows)} "
                f"member(s) Cartograph holds for this case; sent without existing_member_id"
            )
    return linked


def _canonical_type(value, aliases: dict[str, str]) -> str:
    """A type slug with the consumer's own aliases applied, so a manifest
    row still typed by a value they have moved away from (non_federal_wages)
    is the same type as the payload's (wages_and_salaries)."""
    slug = _slug(value)
    # Trailing plural: the alias table is keyed on the singular, as the
    # vocabulary mapping normalises ("non_federal_wages" -> non_federal_wage).
    if slug.endswith("s") and not slug.endswith("ss"):
        slug = slug[:-1]
    return _slug(aliases.get(slug, slug))


def link_records(
    records: list[dict],
    manifest_rows: list[dict],
    linked_members: dict[str, int],
    *,
    type_key: str,
    source_key: str,
    manifest_source_key: str,
    context: str,
    warnings: list[str],
    aliases: dict[str, str] | None = None,
) -> None:
    """Set existing_record_id on each payload record that corresponds to a
    manifest row: the same member, the same type, and — when the member has
    several rows of that type — the closest source name."""
    for r in records:
        r.setdefault("existing_record_id", None)
    if not manifest_rows or not records:
        return
    aliases = {_canonical_type(k, {}): _canonical_type(v, {}) for k, v in (aliases or {}).items()}

    claims: list[tuple[float, int, int]] = []
    for i, rec in enumerate(records):
        member_id = linked_members.get(rec.get("member_ref") or "")
        if member_id is None:
            continue
        rtype = _canonical_type(rec.get(type_key), aliases)
        same_member_type = [
            (j, row) for j, row in enumerate(manifest_rows)
            if _int_or_none(row.get("cert_household_member_id")) == member_id
            and _canonical_type(row.get(type_key), aliases) == rtype
        ]
        if not same_member_type or not rtype:
            continue
        source = str(rec.get(source_key) or "").strip()
        for j, row in same_member_type:
            theirs = str(row.get(manifest_source_key) or "").strip()
            sim = _name_similarity(source, theirs) if source and theirs else 0.0
            if len(same_member_type) == 1:
                # One row of this type for this member: the type decides;
                # the source name only improves the score.
                claims.append((1.0 + sim, i, j))
            elif sim >= _SOURCE_THRESHOLD:
                claims.append((sim, i, j))
    claims.sort(reverse=True)

    taken_records: set[int] = set()
    taken_rows: set[int] = set()
    for _score, i, j in claims:
        if i in taken_records or j in taken_rows:
            continue
        taken_records.add(i)
        taken_rows.add(j)
        records[i]["existing_record_id"] = _int_or_none(manifest_rows[j].get("id"))

    # A record with no counterpart is created on their side as before: a
    # source new since the last certification is the ordinary case, not a
    # warning.
    for i, rec in enumerate(records):
        if i not in taken_records and linked_members.get(rec.get("member_ref") or "") is not None:
            logger.info("%s[%s] (%s, %s) has no counterpart among Cartograph's rows; sent without existing_record_id",
                        context, i, rec.get(type_key), rec.get(source_key) or "no source")


def link_payload(payload: dict, raw_manifest, warnings: list[str], aliases: dict[str, str] | None = None) -> None:
    """Attach Cartograph's own ids to the members, income and asset records."""
    manifest = normalise_manifest(raw_manifest)
    members = payload.get("household_members") or []
    linked = link_members(members, manifest, warnings)
    link_records(
        payload.get("income_records") or [], manifest["income_records"], linked,
        type_key="income_type", source_key="source_name", manifest_source_key="source_name",
        context="income_records", warnings=warnings, aliases=aliases,
    )
    link_records(
        payload.get("asset_records") or [], manifest["asset_records"], linked,
        type_key="asset_type", source_key="institution_name", manifest_source_key="institution_name",
        context="asset_records", warnings=warnings, aliases=aliases,
    )
