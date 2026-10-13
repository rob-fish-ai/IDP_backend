import logging
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
    from app.services.audit.job_store import get_job_store
    from app.services.cartograph.resume import interrupted_cases, start_resume_thread

    # Cases that were in flight when the last process stopped are claimed
    # before the maintenance thread starts, or its first sweep would fail
    # them as wedged instead of letting them run again.
    start_resume_thread(settings, get_job_store(settings.audit_job_db))
    start_poller_thread()
    start_maintenance_thread()
    try:
        yield
    finally:
        stop_poller_thread()
        stop_maintenance_thread()
        try:
            in_flight = [r["case_id"] for r in interrupted_cases(get_job_store(settings.audit_job_db))]
        except Exception:
            in_flight = []
        if in_flight:
            logging.getLogger(__name__).warning(
                "Stopping with %d case(s) in flight; they are audited again at the next start: %s",
                len(in_flight), ", ".join(in_flight),
            )


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
