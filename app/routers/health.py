import shutil
from pathlib import Path

from fastapi import APIRouter, Depends

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.schemas.health import HealthResponse
from app.services.cartograph.environments import describe

router = APIRouter(tags=["Health"])


def storage(settings: Settings) -> dict:
    """Free megabytes under output_dir and under the job store's folder
    (the same number when both sit on one volume), and the job store's
    size. A path that does not exist yet reports nothing rather than
    failing the health call."""
    out: dict = {}
    for key, path in (("output_free_mb", Path(settings.output_dir)),
                      ("job_db_free_mb", Path(settings.audit_job_db).parent)):
        try:
            out[key] = shutil.disk_usage(path).free // (1024 * 1024)
        except OSError:
            pass
    try:
        out["job_db_mb"] = round(Path(settings.audit_job_db).stat().st_size / (1024 * 1024), 1)
    except OSError:
        pass
    return out


@router.get("/health", response_model=HealthResponse)
async def health_check(settings: Settings = Depends(get_settings)):
    env = describe(settings)
    return HealthResponse(status="ok", version=settings.app_version,
                          environments=env["environments"], default_environment=env["default"],
                          storage=storage(settings))
