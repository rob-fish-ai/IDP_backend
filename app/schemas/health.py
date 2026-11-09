from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str
    # The Cartograph environments this host serves (names and hosts, no
    # secrets) and the one whose rows are keyed by bare case_ref.
    environments: dict = {}
    default_environment: str | None = None
    # Free space where the engine writes (output_dir and the job store) and
    # the job store's size, in megabytes, so a host filling up is seen
    # from the same call that says the engine is up.
    storage: dict = {}
