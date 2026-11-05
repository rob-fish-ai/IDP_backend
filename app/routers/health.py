from fastapi import APIRouter, Depends

from app.core.config import Settings
from app.core.dependencies import get_settings
from app.schemas.health import HealthResponse
from app.services.cartograph.environments import describe

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(settings: Settings = Depends(get_settings)):
    env = describe(settings)
    return HealthResponse(status="ok", version=settings.app_version,
                          environments=env["environments"], default_environment=env["default"])
