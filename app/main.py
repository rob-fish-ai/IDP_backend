from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.dependencies import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.routers import health, integration, pdf, webhook


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    setup_logging(settings)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    # Start the audit poller as a background thread when audit_mode is
    # "poll" or "both". In "webhook" mode (default) this is a no-op —
    # the FastAPI server only listens for incoming webhooks.
    #
    # The maintenance thread (retention cleanup + watchdog) runs in every
    # mode so pure-webhook deployments still prune old terminal rows from
    # the JobStore and recover wedged in-flight cases.
    from app.services.audit.poller import (
        start_maintenance_thread, start_poller_thread,
        stop_maintenance_thread, stop_poller_thread,
    )
    start_poller_thread()
    start_maintenance_thread()
    try:
        yield
    finally:
        stop_poller_thread()
        stop_maintenance_thread()


def create_app() -> FastAPI:
    settings = get_settings()

    # Interactive docs are served only when IDP_DEBUG=true. They cannot be
    # put behind the bearer token — a browser navigating to /docs sends no
    # Authorization header — and on a publicly reachable pod they hand a
    # visitor the full endpoint list and every request schema.
    docs_enabled = settings.debug

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://idp-frontend-drab.vercel.app/",
            "http://localhost:3000",
            "http://localhost:5173",
        ],
        allow_origin_regex=r"https://idp-frontend.*\.vercel\.app",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(pdf.router)
    app.include_router(webhook.router)
    app.include_router(integration.router)

    return app


app = create_app()
