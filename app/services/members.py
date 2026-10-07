"""Facts about household members that several modules need and that must
not be defined twice."""


def is_unborn(member) -> bool:
    """A household composition row for a child not yet born. The row counts
    toward household size and the income limit; it has no date of birth,
    SSN, gender or student status, and it is not an adult."""
    get = member.get if isinstance(member, dict) else (lambda k: getattr(member, k, None))
    text = " ".join(str(get(k) or "") for k in ("FirstName", "LastName", "relationship")).lower()
    return "unborn" in text or "expected child" in text


# Roles a household composition form gives only to adults. Head is left
# out on purpose: an emancipated minor can head a household.
_ADULT_ONLY_RELATIONSHIPS = frozenset({
    "co-head", "cohead", "co head", "spouse", "wife", "husband", "other adult", "adult",
    "live-in aide", "live in aide", "aide", "partner", "co-tenant", "adult co-tenant",
})


def age_on(dob: str | None, reference: str | None) -> float | None:
    """Age in years on the reference date (ISO strings), or None."""
    from datetime import date
    try:
        y, m, d = (int(x) for x in (dob or "")[:10].split("-"))
        born = date(y, m, d)
        y, m, d = (int(x) for x in (reference or "")[:10].split("-"))
        ref = date(y, m, d)
    except (TypeError, ValueError):
        return None
    return (ref - born).days / 365.25


def relationship_for_age(relationship: str | None, dob: str | None, reference: str | None) -> str | None:
    """The relationship, corrected when it names an adult-only role for a
    member the dates make a minor. A one-letter code ("C" on a tenant
    income certification is Child) expanded to "Co-Head" for a four-year-
    old is a misread of the code, and the date of birth says so."""
    age = age_on(dob, reference)
    if age is None or age >= 18:
        return relationship
    if (relationship or "").strip().lower().rstrip(".") in _ADULT_ONLY_RELATIONSHIPS:
        return "Child"
    return relationship
