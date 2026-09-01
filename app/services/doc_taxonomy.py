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

Recognition is by exact label, not by substring. Substring matching failed in
both directions on real packets:

  - "Tenant Income Certification Questionnaire" contains the certification's
    entire name and is not the certification, so a questionnaire's signature
    stood in for a certification's.
  - "Annual Self Certification", "NY AR Self Certification Form" and
    "Owner's Eligibility Determination" are AR-SC certifications and contain
    none of the markers, so they would not have been recognised at all.

Both failures are the same mistake: guessing a document's kind from how its
name reads. The classifier is told which labels exist; this module trusts
that list and reports anything outside it rather than guessing.
"""

import logging

logger = logging.getLogger(__name__)

# Certification forms, exactly as the classifier is instructed to emit them.
# Each program has its own: LIHTC's TIC, HUD's 50059, Rural Development's
# 3560. AR-SC self-certifications are classified as TIC by design — the
# prompt says so — which is why no self-certification label appears here.
_CERTIFICATION_TYPES = frozenset({
    "hud 50059",
    "tenant income certification (tic)",
    "hud 3560 form",
})

# Markers specific enough that an unrecognized label carrying one is an
# anomaly worth reporting. Used only to decide whether to warn, never to
# classify.
#
# Deliberately narrow. "certification" appears in Asset Self-Certification,
# Zero Income Certification and Student Status Certification, all legitimate
# types of their own; "tic" appears inside "Notice". Warning on those would
# bury the case this exists for — a label carrying a certification form's
# actual name that is not a known type, which is what
# "Tenant Income Certification Questionnaire" was.
_RESEMBLES_CERTIFICATION = (
    "tenant income certification",
    "50059",
    "3560",
)

_PREVIOUS_MARKER = "(previous)"

# Warn once per distinct label. An unknown type recurs on every page of the
# document that carries it, and a warning per page would bury the signal it
# exists to raise.
_reported: set[str] = set()


def _base_label(document_type: str) -> str:
    return document_type.lower().replace(_PREVIOUS_MARKER, "").strip()


def is_certification_form(document_type: str | None) -> bool:
    """Whether a classified document type is a certification form.

    An unrecognized label that reads like a certification is reported rather
    than guessed at. That is the case worth surfacing: the classifier
    returning something outside its own taxonomy means the document reaches
    no extractor and contributes nothing, and it does so silently.
    """
    if not document_type:
        return False

    base = _base_label(document_type)
    if base in _CERTIFICATION_TYPES:
        return True

    if any(word in base for word in _RESEMBLES_CERTIFICATION):
        if base not in _reported:
            _reported.add(base)
            logger.warning(
                "Document type %r is not a known type but reads like a "
                "certification — it will be treated as an ordinary document. "
                "If it is a certification form, the classifier should be "
                "returning one of: %s",
                document_type, sorted(_CERTIFICATION_TYPES),
            )
    return False


def is_previous_certification(document_type: str | None) -> bool:
    """Whether a label marks a prior-year copy rather than the current one.

    A previous certification restates last year's figures. Nothing in it
    should match this year's records, so any check comparing an extraction
    against the certification must exclude it or every packet carrying one
    looks full of discrepancies.
    """
    return _PREVIOUS_MARKER in (document_type or "").lower()


def is_current_certification_form(document_type: str | None) -> bool:
    """The certification being audited: a certification form, not a prior copy."""
    return (
        is_certification_form(document_type)
        and not is_previous_certification(document_type)
    )
