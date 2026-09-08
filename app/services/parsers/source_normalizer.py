"""Employer/source name normalization."""

import re

from app.services.validation import to_title_case

# Known employer name corrections (lowercase key without special chars -> proper name)
_SOURCE_NAME_MAP: dict[str, str] = {
    "7eleven": "7-Eleven",
    "7 eleven": "7-Eleven",
    "7eleven inc": "7-Eleven",
    "711": "7-Eleven",
    "7ll": "7-Eleven",
    "mcdonalds": "McDonald's",
    "walmart": "Walmart",
    "amazon": "Amazon",
    "starbucks": "Starbucks",
    "target": "Target",
    "costco": "Costco",
    "ssa": "Social Security Administration",
    "social security administration": "Social Security Administration",
}


def normalize_source_name(name: str | None) -> str | None:
    """Normalize an employer or source name.

    Checks against known name map first, then falls back to Title Case.
    """
    if not name:
        return None

    stripped = name.strip()
    if not stripped:
        return None

    # Try exact match on lowercased, stripped of special chars
    key = re.sub(r"[^a-z0-9\s]", "", stripped.lower()).strip()
    key_no_space = re.sub(r"\s+", "", key)

    # Check both with and without spaces
    if key in _SOURCE_NAME_MAP:
        return _SOURCE_NAME_MAP[key]
    if key_no_space in _SOURCE_NAME_MAP:
        return _SOURCE_NAME_MAP[key_no_space]

    # Deliberately no substring fallback. Canonicalising the spelling of a
    # name is not the same as guessing which entity it is, and matching a
    # map key anywhere inside the input did the second: any employer whose
    # name happens to contain "ssa" became the Social Security
    # Administration — Massachusetts General Hospital, Nassau County Health,
    # Vassar Brothers Medical — and "Amazonia Foods" became Amazon.
    #
    # The renamed source then satisfied the 'ssa' tests in bug_detector and
    # the income routing, so a wage earner's paystubs were audited as
    # benefit income and the reviewer was told to delete them. Word
    # boundaries would fix the accidents but not the principle: a fragment
    # of a name is not evidence of the entity.
    #
    # A genuine spelling variant belongs in the map as its own key.
    return to_title_case(stripped)
