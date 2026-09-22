"""Facts about household members that several modules need and that must
not be defined twice."""


def is_unborn(member) -> bool:
    """A household composition row for a child not yet born. The row counts
    toward household size and the income limit; it has no date of birth,
    SSN, gender or student status, and it is not an adult."""
    get = member.get if isinstance(member, dict) else (lambda k: getattr(member, k, None))
    text = " ".join(str(get(k) or "") for k in ("FirstName", "LastName", "relationship")).lower()
    return "unborn" in text or "expected child" in text
