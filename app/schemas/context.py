"""Pipeline context — carries request-scoped parameters through the pipeline."""

from typing import Optional

from pydantic import BaseModel


class PipelineContext(BaseModel):
    """Parameters that flow through the entire extraction pipeline."""
    funding_program: Optional[str] = None  # LIHTC, HUD, USDA, RAD, Public Housing
    certification_type: Optional[str] = None  # MI, AR, AR-SC, IR (from API or extracted)
    # What the certification document itself says it is, when that can be
    # read (move-in date equal to the effective date → MI). Set by the
    # pipeline; the document-requirement rules follow it when it contradicts
    # the caller's type, and the contradiction is a finding.
    document_certification_type: Optional[str] = None
