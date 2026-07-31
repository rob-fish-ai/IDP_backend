from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="IDP_")

    app_name: str = "IDP - Intelligent Document Processing"
    app_version: str = "1.0.0"
    debug: bool = False

    # Storage
    output_dir: Path = Path("output")

    # PDF rendering
    # 200 DPI matches the native resolution of most scanned HUD/affordable
    # housing PDFs and provides ~1.3× supersample headroom for a clean
    # downsample to the 1280×1920 OCR target. Rendering higher just upsamples
    # the embedded scan — no real detail is recovered, and preprocessing
    # runtime scales with pixel count (~2.25× faster than 300 DPI).
    render_dpi: int = 200

    # Vision fallback for low-quality OCR pages.
    # When a page's OCR composite score falls below this threshold, the page
    # image is sent to Claude Vision for text extraction before classification.
    # This replaces the bad OCR text at the source so every downstream step
    # (classification, extraction, scoring) benefits from clean text.
    ocr_vision_threshold: float = 0.5

    # Image pre-processing
    # DeepSeek-OCR is optimized for 1280×1920 input. Smaller images force
    # the model to upscale internally which degrades accuracy on small text,
    # signatures, and handwritten fields. 1280×1920 matches the model's
    # native vision encoder resolution for best OCR quality.
    image_max_width: int = 1280
    image_max_height: int = 1920

    # Logging
    log_level: str = "INFO"
    log_json: bool = False

    # OCR service
    ocr_service_url: str = ""
    ocr_fallback_url: str = ""
    ocr_prompt: str = "document"
    ocr_dpi: int = 200
    ocr_timeout: int = 600
    ocr_raw: bool = False
    ocr_retry: bool = True
    # OCR service can handle 4 concurrent requests. Parallelizing OCR
    # reduces whole-pipeline latency dramatically (~5x on 50+ page files).
    ocr_concurrency: int = 4
    # Image preprocessing runs in parallel worker threads because OpenCV
    # releases the Python GIL. Rendering stays sequential (PyMuPDF is not
    # thread-safe) but render(n+1) overlaps with preprocess(n).
    preprocess_concurrency: int = 4

    # LLM extraction (Claude API)
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5"
    # Classification runs on Haiku by default — classification is a
    # constrained labeling task that Haiku handles well, and Haiku uses a
    # separate capacity pool so classification stays up when Sonnet 529s.
    llm_classify_model: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 8192
    llm_temperature: float = 0.0

    # Pipeline context (optional overrides)
    funding_program: str = ""  # LIHTC, HUD, USDA, RAD, Public Housing
    certification_type_override: str = ""  # MI, AR, AR-SC, IR

    # Upload validation
    allowed_content_types: list[str] = [
        "application/pdf",
        "application/octet-stream",
    ]
    # PDFs below this page count are rejected as non-source documents.
    # Real recertification packets always contain at minimum a HUD 50059
    # or TIC plus supporting verifications — they don't exist in 1-3 pages.
    # Anything smaller is almost always a coversheet, stray attachment,
    # partial upload, or a non-source doc the title denylist missed.
    min_pdf_pages: int = 4

    # AR-SC self-certification packets are legitimately tiny — a complete
    # OHCS/NY self-cert is 2-3 pages (resident page + owner determination).
    # The global gate would reject them all; 2 still blocks 1-page strays.
    min_pdf_pages_self_cert: int = 2

    # Cap on the total page count when merging all of a case's source
    # PDFs into one packet. Files are merged newest-first; candidates
    # that would push past the cap are skipped (and logged), so the
    # newest packet always gets in whole.
    max_merged_pages: int = 150

    # ------------------------------------------------------------------
    # Salesforce integration
    # ------------------------------------------------------------------
    sf_username: str = ""
    sf_password: str = ""
    sf_token: str = ""
    sf_domain: str = "us-hc.my"

    # Webhook authentication — Salesforce includes this token in the
    # Authorization header. Reject any webhook without a matching token.
    webhook_auth_token: str = ""
    # Set IDP_DEV_MODE=true ONLY for local development to bypass webhook
    # auth. In production this MUST be false (the default) so missing
    # tokens fail closed instead of accepting unauthenticated webhooks.
    dev_mode: bool = False

    # Salesforce HTTP timeout (seconds) for both SOQL queries and binary
    # downloads. A hung Salesforce response would otherwise block the
    # worker indefinitely.
    sf_request_timeout: int = 30

    # Integration mode:
    #   "webhook" — Salesforce pushes signals via /webhook/pdf-attached and
    #               /webhook/mulesoft-done. Default. Lower latency, requires
    #               Salesforce-side Apex triggers + named credentials.
    #   "poll"    — IDP polls Salesforce on a fixed interval to find ready
    #               cases. No Salesforce-side setup required. Higher latency
    #               and SF API load. Webhooks return 503 in this mode.
    #   "both"    — Both webhook and poller active. Useful during transition.
    audit_mode: str = "webhook"

    # Poller settings (only used when audit_mode in {"poll", "both"})
    audit_poll_interval_seconds: int = 60
    audit_poll_batch_size: int = 5

    # Cases processed concurrently within a poll batch. Each case's OCR
    # is external HTTP and LLM calls are network-bound, so 3 concurrent
    # pipelines roughly triple throughput; one 70-page merge no longer
    # blocks the rest of the batch. Job state transitions are claim-based
    # and page artifacts are isolated per-job, so parallel runs are safe.
    audit_pipeline_concurrency: int = 3

    # Audit job storage (SQLite). Persists case state across restarts.
    audit_job_db: Path = Path("output/audit_jobs.db")

    # When MuleSoft webhook arrives but IDP extraction is still running,
    # the comparison job polls every N seconds until extraction completes
    # or the timeout fires.
    audit_extraction_wait_seconds: int = 600    # 10 minutes
    audit_extraction_poll_seconds: int = 15

    # Watchdog — recovers cases stuck in extracting/extracted/comparing
    # after a worker died mid-flight (deploy, OOM, crash). On every poll
    # cycle the watchdog scans for rows older than `watchdog_seconds`
    # and re-queues them up to `watchdog_max_retries` times. Permanent
    # failure marker after the cap so they drop out of the queue.
    audit_watchdog_seconds: int = 1800           # 30 minutes
    audit_watchdog_max_retries: int = 5

    # Retention — terminal rows (done / *_failed / mulesoft_timeout) older
    # than this are deleted by the periodic maintenance sweep. Keeps the
    # SQLite DB from growing unbounded while still leaving the dashboard a
    # rolling window of recent audits to inspect. Set to 0 to disable.
    audit_retention_days: int = 7
    # How often the maintenance thread runs retention + watchdog (seconds).
    # Independent of audit_mode — runs even in pure webhook deployments
    # where the poller itself is idle.
    audit_maintenance_interval_seconds: int = 3600   # 1 hour
