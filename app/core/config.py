from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": a .env still carrying a setting of the retired
    # Salesforce path (IDP_SF_*, IDP_AUDIT_MODE, IDP_WEBHOOK_AUTH_TOKEN, …)
    # must not stop the engine from starting.
    model_config = SettingsConfigDict(env_file=".env", env_prefix="IDP_", extra="ignore")

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
    # The source PDF of every Cartograph case is kept under output_dir/pdfs
    # so a contested finding can be checked against the rendered page
    # without asking the client for the file again. Pruned by age and by
    # total size, oldest first; 0 days disables keeping them.
    pdf_retention_days: int = 14
    pdf_retention_max_mb: int = 1500
    # A Cartograph extraction that fails for a transient reason (the model
    # unavailable, a truncated response, a rate limit) is tried again this
    # many times in all, with this pause, before the failure is reported.
    cartograph_extract_attempts: int = 2
    cartograph_retry_delay_seconds: float = 30.0
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
    # Classification decides which extractor ever sees a page, so a wrong
    # call loses the content entirely rather than producing a wrong value.
    # Moved off Haiku 2026-09-10 after the same packet classified its two
    # income-verification pages as "Verification of Income (VOI)" on one run
    # and "Unknown" on the next, 90 minutes apart, on identical bytes.
    llm_classify_model: str = "claude-sonnet-5"
    # Response budget. Thinking-enabled models (Sonnet 5+) spend reasoning
    # tokens from this same budget, so it needs headroom beyond the largest
    # expected JSON output — 8192 caused mid-JSON truncation on asset-heavy
    # cases. Cost is per generated token, not per the cap itself.
    llm_max_tokens: int = 16384
    llm_temperature: float = 0.0
    # Extended thinking for extraction calls: "disabled" or "adaptive".
    # Sent explicitly on every call, because an omitted parameter means
    # adaptive on Sonnet 5 and off on the 4.6 family. Extraction is closer
    # to transcription than reasoning; the harness decides (see
    # eval/README.md), and classification and page transcription always
    # run disabled regardless of this setting.
    llm_thinking: str = "disabled"

    # Pipeline context (optional overrides)
    funding_program: str = ""  # LIHTC, HUD, USDA, RAD, Public Housing
    certification_type_override: str = ""  # MI, AR, AR-SC, IR

    # --- Audit jobs ---------------------------------------------------------
    # Cases audited at once — through the Cartograph intake, and when
    # interrupted cases are run again at start. Each
    # case's OCR is external HTTP and LLM calls are network-bound, so 3
    # concurrent pipelines roughly triple throughput; one 70-page merge no
    # longer blocks the rest. Job state transitions are claim-based and page
    # artifacts are isolated per-job, so parallel runs are safe. The bound
    # is also the memory guard: a rendered packet is large, the pod is
    # capped at 16 GB, and 26 at once was enough for the kernel to kill the
    # engine (2026-10-13). Cases beyond the bound wait in line.
    audit_pipeline_concurrency: int = 3

    # Audit job storage (SQLite). Persists case state across restarts.
    audit_job_db: Path = Path("output/audit_jobs.db")

    # Watchdog — a row still queued or extracting whose heartbeat has not
    # moved for this long belongs to a worker that died (OOM, crash) while
    # the process lived on. The maintenance sweep fails it and tells
    # Cartograph to re-notify. The intake's waiting rows and the running
    # audits both keep the heartbeat moving, so a slow packet is never
    # mistaken for a dead one.
    audit_watchdog_seconds: int = 1800           # 30 minutes

    # A Cartograph case that was mid-audit when the engine stopped (a pod
    # restart, a deploy, a crash) is run again at the next start, from the
    # request as received and the packet kept under output_dir/pdfs, instead
    # of being failed by the watchdog and re-notified by hand. The rows are
    # claimed the moment the engine starts; the work begins after the delay,
    # so a start that cannot bind its port (the previous process still
    # draining) has exited before any audit is re-run.
    audit_resume_on_start: bool = True
    audit_resume_delay_seconds: float = 10.0

    # Retention — terminal rows (done / *_failed) older than this are
    # deleted by the periodic maintenance sweep, so the job store does not
    # grow without bound. Set to 0 to disable.
    audit_retention_days: int = 7
    # How often the maintenance thread runs retention + watchdog (seconds).
    audit_maintenance_interval_seconds: int = 3600   # 1 hour

    # --- Cartograph integration -------------------------------------------
    # Outbound: where extraction results are POSTed, and the secret used to
    # sign them. Cartograph verifies with the same scheme it uses inbound.
    cartograph_ingest_url: str = ""
    cartograph_ingest_secret: str = ""
    # Inbound: the secret Cartograph signs its result callback with. Distinct
    # from the outbound secret so a compromise in one direction does not
    # expose the other.
    cartograph_callback_secret: str = ""
    # Cartograph's own picklists for income and asset type. Supplied as
    # configuration because they belong to the consumer, not to the engine:
    # they are editable on their side, they differ by funding program, and a
    # list compiled from whatever documents happened to be tested stops
    # working on the next property. Empty means nothing can be confirmed
    # valid, so every value collapses to "other" and is reported — which is
    # the correct behaviour when the picklist is unknown, and produces the
    # list of terms to agree with them.
    cartograph_income_types: list[str] = []
    cartograph_asset_types: list[str] = []
    # Overrides for terms whose normalized spelling matches a value the
    # consumer no longer wants used. Keyed on the normalized extracted term.
    # Cartograph keeps `non_federal_wages` in its picklist but has moved
    # employment income to `wages_and_salaries`, carrying the distinction in
    # a separate employment_type field — so matching on spelling alone would
    # pick the value they are moving away from. Configuration rather than
    # code because it records their decision, not ours.
    cartograph_type_aliases: dict[str, str] = {}
    # Bound the outbound POST so a stalled connection cannot wedge a worker.
    cartograph_timeout_seconds: float = 30.0
    # A notification for a case the engine delivered within this many seconds,
    # naming the same documents, is a repeat and is not audited again. During
    # the 2026-10-29 batch Cartograph re-sent 29 of 61 cases between seconds
    # and 94 minutes after accepting their results; each repeat cost a full
    # OCR and model run and a second import of the same figures. A request
    # with a different document list, or one arriving after the window, is
    # a deliberate re-run and is audited as before. 0 turns the guard off.
    cartograph_repost_window_seconds: int = 7200       # 2 hours
    # Whether a packet is expected to carry the prior year's certification.
    # Cartograph's cases hold one certification plus continuation pages and
    # the prior year lives on its own record, so a missing previous cert is
    # not a gap in the packet; the finding and the checklist rows that ask
    # for a previous form are off unless this is set.
    report_previous_cert_missing: bool = False

