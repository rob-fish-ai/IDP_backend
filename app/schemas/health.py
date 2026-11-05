from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str
    # The Cartograph environments this host serves (names and hosts, no
    # secrets) and the one whose rows are keyed by bare case_ref.
    environments: dict = {}
    default_environment: str | None = None
