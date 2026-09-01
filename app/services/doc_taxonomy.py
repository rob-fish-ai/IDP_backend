"""One definition of what a document type means.

The classifier assigns labels from a taxonomy described in its prompt.
Consumers then need to ask questions of those labels — "is this the
certification form?" — and each had grown its own substring list to answer
it. Four copies existed before this module: the extractor routing sets, the
signature vision verifier, the completeness checks, and the previous-cert
demotion.

Copies drift. A label added to the classifier reaches whichever consumers
someone remembered, and the ones missed fail silently — a certification form
that no longer matches is not an error, it is a check that quietly stops
running. That failure is invisible precisely because nothing breaks.
"""

# Forms that certify the household's eligibility: the document whose figures
# the audit treats as the property manager's own account, and against which
# an extraction is checked. Program-specific because each program has its
# own form — LIHTC's TIC, HUD's 50059, Rural Development's 3560.
_CERTIFICATION_MARKERS = (
    "tenant income certification",
    "(tic)",
    "50059",
    "3560",
)

# Words that name a different kind of document even when a certification
# marker is also present. A questionnaire titled "Tenant Income
# Certification Questionnaire" contains the certification's whole name and
# is not the certification — it is the resident's declaration that feeds
# one. Marker matching alone would treat it as the form it is named after,
# which is how a questionnaire's signature comes to stand for a
# certification's.
#
# The qualifier wins because it is the more specific claim: "certification"
# describes the subject, "questionnaire" describes the document.
_NOT_CERTIFICATION_MARKERS = (
    "questionnaire",
    "worksheet",
    "checklist",
    "instructions",
)


def is_certification_form(document_type: str | None) -> bool:
    """Whether a classified document type is a certification form.

    Matched on markers rather than exact labels because the classifier
    qualifies them — "Tenant Income Certification (TIC)", "HUD 50059
    (Previous)", "HUD 3560 Form" are all the same kind of document with
    different decoration.
    """
    if not document_type:
        return False
    label = document_type.lower()
    if any(marker in label for marker in _NOT_CERTIFICATION_MARKERS):
        return False
    return any(marker in label for marker in _CERTIFICATION_MARKERS)


def is_previous_certification(document_type: str | None) -> bool:
    """Whether a label marks a prior-year copy rather than the current one.

    A previous certification restates last year's figures. Nothing in it
    should match this year's records, so any check comparing an extraction
    against the certification must exclude it or every packet carrying one
    looks full of discrepancies.
    """
    return "(previous)" in (document_type or "").lower()


def is_current_certification_form(document_type: str | None) -> bool:
    """The certification being audited: a certification form, not a prior copy."""
    return (
        is_certification_form(document_type)
        and not is_previous_certification(document_type)
    )
