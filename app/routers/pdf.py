import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.core.exceptions import InvalidFileError, JobNotFoundError
from app.schemas.pdf import FullPipelineResponse, ProcessPdfResponse
from app.services import pdf_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pdf", tags=["PDF"])


async def _validate_upload(file: UploadFile, settings: Settings) -> bytes:
    """Common upload validation for PDF endpoints."""
    if file.content_type not in settings.allowed_content_types:
        raise InvalidFileError(
            f"Unsupported content type '{file.content_type}'. Only PDF files are accepted."
        )
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise InvalidFileError("Uploaded file is empty.")
    return pdf_bytes


@router.post("/process", response_model=ProcessPdfResponse)
async def upload_and_process_pdf(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
):
    """Upload a PDF, split into pages, pre-process, OCR, and save text per page."""
    pdf_bytes = await _validate_upload(file, settings)
    logger.info("Received PDF upload size=%d content_type=%s", len(pdf_bytes), file.content_type)

    result = await run_in_threadpool(pdf_service.process_pdf, pdf_bytes, settings)
    return result


@router.post("/extract", response_model=FullPipelineResponse)
async def upload_and_extract(
    file: UploadFile = File(...),
    funding_program: Optional[str] = Form(None, description="Funding program: LIHTC, HUD, USDA, RAD, Public Housing"),
    certification_type: Optional[str] = Form(None, description="Certification type override: MI, AR, AR-SC, IR"),
    settings: Settings = Depends(get_settings),
):
    """Full pipeline: OCR + classify + extract structured data per MuleSoft schemas.

    Returns OCR results and structured extraction including:
    - Household demographics
    - Income data (pay stubs + verification income)
    - Asset data (bank statements + VOA)
    - Document inventory (financial + HUD compliance)
    - Compliance findings
    """
    pdf_bytes = await _validate_upload(file, settings)
    logger.info(
        "Received PDF for full extraction size=%d content_type=%s",
        len(pdf_bytes), file.content_type,
    )

    result = await run_in_threadpool(
        pdf_service.process_pdf_full,
        pdf_bytes,
        settings,
        funding_program=funding_program,
        certification_type=certification_type,
    )
    # SSNs are stored as captured; every audit-facing response masks them.
    from app.services.validation import mask_ssns_deep
    return mask_ssns_deep(result)


@router.get("/images/{filename}")
async def get_image(
    filename: str,
    settings: Settings = Depends(get_settings),
):
    """Serve a processed page image."""
    file_path = settings.output_dir / "processed" / filename
    if not file_path.exists():
        raise JobNotFoundError(f"Image not found: {filename}")

    return FileResponse(str(file_path), media_type="image/png")


@router.get("/texts/{filename}")
async def get_text(
    filename: str,
    settings: Settings = Depends(get_settings),
):
    """Serve an extracted text file."""
    file_path = settings.output_dir / "texts" / filename
    if not file_path.exists():
        raise JobNotFoundError(f"Text file not found: {filename}")

    return FileResponse(str(file_path), media_type="text/plain; charset=utf-8")
