from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

class IDPBaseError(Exception):
    """Base exception for all IDP domain errors."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class InvalidFileError(IDPBaseError):
    """Raised when the uploaded file is invalid or unsupported."""


class JobNotFoundError(IDPBaseError):
    """Raised when a job_id does not exist."""


class ProcessingError(IDPBaseError):
    """Raised when PDF/image processing fails."""


class ClassificationUnavailableError(ProcessingError):
    """Raised when the classification LLM call fails outright.

    Without classification NOTHING downstream can work — every page would
    be 'Unknown', extraction would see zero documents, and the pipeline
    would emit a confident-looking garbage audit (observed: 7% RED with
    48 per-page noise findings written to Salesforce as a real verdict).
    This error is classified retryable, so the case stays unprocessed and
    the next poll cycle retries — a transient API blip costs a minute,
    not a false audit.
    """


# ---------- HTTP error mapping ----------

_STATUS_MAP: dict[type[IDPBaseError], int] = {
    InvalidFileError: 400,
    JobNotFoundError: 404,
    ProcessingError: 500,
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(IDPBaseError)
    async def idp_error_handler(_request: Request, exc: IDPBaseError) -> JSONResponse:
        status_code = _STATUS_MAP.get(type(exc), 500)
        return JSONResponse(
            status_code=status_code,
            content={"error": type(exc).__name__, "detail": exc.detail},
        )
