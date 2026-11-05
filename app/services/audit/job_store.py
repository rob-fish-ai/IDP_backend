"""Persistent job state for audit cases.

State machine per case:
  queued            — accepted and handed to the intake, not started
  extracting        — the audit is running
  extracted         — extraction done, being delivered
  done              — delivered to Cartograph
  extraction_failed — the audit errored, or the worker died (watchdog)
  comparison_failed — delivery to Cartograph failed or was rejected

`pending`, `comparing` and `mulesoft_timeout` belonged to the retired
Salesforce path; the constants stay so rows left in those states are
still recognised and pruned.

SQLite: small write volume, one process, and the file sits on the pod's
persistent volume so state survives restarts.
"""
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# State constants
PENDING = "pending"          # retired: unclaimed, waiting for a poller
QUEUED = "queued"
EXTRACTING = "extracting"
EXTRACTED = "extracted"
COMPARING = "comparing"
DONE = "done"
EXTRACTION_FAILED = "extraction_failed"
COMPARISON_FAILED = "comparison_failed"
MULESOFT_TIMEOUT = "mulesoft_timeout"

# States in which some worker already owns the case, so a second arrival for
# the same case_id is a duplicate rather than a new request.
_OWNED_STATES = (QUEUED, EXTRACTING, EXTRACTED, COMPARING)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_jobs (
    case_id TEXT PRIMARY KEY,
    case_number TEXT,
    state TEXT NOT NULL,
    cert_type TEXT,
    funding_program TEXT,
    content_document_id TEXT,
    extraction_result TEXT,        -- JSON
    findings_text TEXT,
    mulesoft_snapshot TEXT,        -- JSON snapshot captured at comparison time
    error TEXT,
    confidence REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    extracted_at REAL,
    mulesoft_done_at REAL,
    completed_at REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    -- Which integration put this row here. Salesforce-only maintenance
    -- (the revisit sweep asks Salesforce whether a newer PDF was attached)
    -- must not run against a case_id Salesforce has never heard of.
    source TEXT NOT NULL DEFAULT 'salesforce',
    -- What the consumer said when we handed the extraction over, and what it
    -- said later about storing it. Their ingest answers 202 before the
    -- inserts run, so the response to the delivery cannot report the
    -- outcome — that arrives separately on the result callback. Keeping only
    -- log lines meant a case could be delivered, silently rejected during
    -- import, and look complete here forever.
    cartograph_delivery TEXT,        -- JSON: status, scan_id, bytes sent
    cartograph_import_result TEXT,   -- JSON: their import outcome + warnings
    cartograph_import_at REAL,
    cartograph_request TEXT,         -- the case request body as received
    checklist_rows TEXT,             -- JSON: checklist rows read from it
    -- The Cartograph environment the case came from (dev, staging, prod).
    -- Rows of the default environment are keyed by bare case_ref, the
    -- others "<environment>:<case_ref>" (cartograph.environments.job_key).
    environment TEXT
);

CREATE INDEX IF NOT EXISTS idx_state ON audit_jobs(state);

-- What reviewers said about the findings we sent (source 'runpod'), and
-- what they found that the scan missed (source 'manual'). One row per
-- verdict or manual finding; a later verdict on the same finding key
-- replaces the earlier one — the reviewer's last word is the one kept.
CREATE TABLE IF NOT EXISTS finding_feedback (
    case_id TEXT NOT NULL,
    scan_id TEXT,
    source TEXT NOT NULL,              -- 'runpod' | 'manual'
    finding_key TEXT,                  -- ours, for source 'runpod'
    code TEXT,                         -- derived from the key's prefix
    verdict TEXT,                      -- 'valid' | 'invalid' (runpod rows)
    verdict_reason TEXT,
    description TEXT,                  -- manual rows: what the reviewer wrote
    page INTEGER,
    subject_label TEXT,
    matched_checklist_item TEXT,       -- JSON: key and name, when given
    raw TEXT NOT NULL,                 -- the row as received
    received_at REAL NOT NULL,
    PRIMARY KEY (case_id, source, finding_key, description)
);
CREATE INDEX IF NOT EXISTS idx_feedback_code ON finding_feedback(code);
"""


class JobStore:
    """Thread-safe SQLite-backed audit job tracker."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Backfill retry_count on databases created before the column
            # existed. PRAGMA-then-ALTER is safer than catching exceptions.
            existing_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(audit_jobs)").fetchall()
            }
            if "retry_count" not in existing_cols:
                conn.execute(
                    "ALTER TABLE audit_jobs "
                    "ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )
                logger.info("Added retry_count column to existing audit_jobs table")
            if "mulesoft_snapshot" not in existing_cols:
                conn.execute(
                    "ALTER TABLE audit_jobs ADD COLUMN mulesoft_snapshot TEXT"
                )
                logger.info("Added mulesoft_snapshot column to existing audit_jobs table")
            if "source" not in existing_cols:
                # Every pre-existing row came from Salesforce, so the
                # default is correct for the backfill as well as for new
                # rows written by the Salesforce path.
                conn.execute(
                    "ALTER TABLE audit_jobs "
                    "ADD COLUMN source TEXT NOT NULL DEFAULT 'salesforce'"
                )
                logger.info("Added source column to existing audit_jobs table")
            for column, ddl in (
                ("cartograph_delivery", "TEXT"),
                ("cartograph_import_result", "TEXT"),
                ("cartograph_import_at", "REAL"),
                # The case request exactly as it arrived, and the checklist
                # rows read out of it. The request model reads the fields it
                # knows and ignores the rest, so without the raw body nobody
                # can say what Cartograph sent that the engine dropped.
                ("cartograph_request", "TEXT"),
                ("checklist_rows", "TEXT"),
                ("environment", "TEXT"),
            ):
                if column not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE audit_jobs ADD COLUMN {column} {ddl}"
                    )
                    logger.info(
                        "Added %s column to existing audit_jobs table", column,
                    )
        logger.info("Audit job store ready at %s", db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL lets readers (export script, ad-hoc analysis queries) coexist
        # with the worker's writes — in rollback-journal mode a long read
        # blocks commits past the 10s timeout ("database is locked").
        # journal_mode is persistent in the DB file; re-issuing is a no-op.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def upsert_pending(
        self,
        case_id: str,
        case_number: str | None,
        cert_type: str | None,
        funding_program: str | None,
        content_document_id: str | None,
        source: str = "cartograph",
        claimed: bool = False,
        environment: str | None = None,
    ) -> dict[str, Any]:
        """Create or refresh a job. Idempotent.

        If a job already exists in a state some worker owns, return as-is.
        If it's terminal (done/failed) or unclaimed, recreate.

        `claimed` creates the row already owned (queued), so a duplicate
        notification arriving before the worker starts is recognized as a
        duplicate instead of queuing the case twice.
        """
        initial_state = QUEUED if claimed else PENDING
        with self._lock, self._connect() as conn:
            now = time.time()
            row = conn.execute(
                "SELECT state FROM audit_jobs WHERE case_id = ?", (case_id,)
            ).fetchone()
            if row and row["state"] in _OWNED_STATES:
                logger.info(
                    "Job for %s already in state %s — skipping upsert",
                    case_id, row["state"],
                )
                return {"state": row["state"], "deduplicated": True}

            conn.execute("""
                INSERT INTO audit_jobs (
                    case_id, case_number, state, cert_type, funding_program,
                    content_document_id, source, environment, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    case_number = excluded.case_number,
                    state = excluded.state,
                    environment = excluded.environment,
                    cert_type = excluded.cert_type,
                    funding_program = excluded.funding_program,
                    content_document_id = excluded.content_document_id,
                    source = excluded.source,
                    extraction_result = NULL,
                    findings_text = NULL,
                    error = NULL,
                    confidence = NULL,
                    updated_at = excluded.updated_at,
                    extracted_at = NULL,
                    mulesoft_done_at = NULL,
                    completed_at = NULL
            """, (
                case_id, case_number, initial_state, cert_type, funding_program,
                content_document_id, source, environment, now, now,
            ))
            return {"state": initial_state, "deduplicated": False}

    def mark_extracting(self, case_id: str) -> None:
        self._set_state(case_id, EXTRACTING)

    def touch(self, case_id: str) -> None:
        """Record that a job is still making progress, without changing state.

        The watchdog treats a row as wedged when `updated_at` has not moved
        for `audit_watchdog_seconds`. Without a heartbeat that timestamp is
        stamped once when extraction starts, so the measure reads "how long
        has this been running" rather than "how long since anything
        happened" — and a genuinely slow packet (119 pages, most of them
        scanned sideways, 42 minutes) is indistinguishable from a worker
        that died in the first second.

        Being wrong in that direction is expensive: the sweep re-queues or
        fails a case that is still running, which releases the dedupe and
        lets a re-notification start the same audit a second time.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE audit_jobs SET updated_at = ? WHERE case_id = ?",
                (time.time(), case_id),
            )

    def mark_extracted(self, case_id: str, extraction_result: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            now = time.time()
            conn.execute("""
                UPDATE audit_jobs
                SET state = ?, extraction_result = ?,
                    extracted_at = ?, updated_at = ?
                WHERE case_id = ?
            """, (
                EXTRACTED, json.dumps(extraction_result, default=str),
                now, now, case_id,
            ))

    def mark_extraction_failed(self, case_id: str, error: str) -> None:
        self._set_state(case_id, EXTRACTION_FAILED, error=error)

    def mark_done(self, case_id: str, findings_text: str, confidence: float) -> None:
        """Mark a case delivered, keeping its findings and extraction
        confidence on the row."""
        with self._lock, self._connect() as conn:
            now = time.time()
            conn.execute("""
                UPDATE audit_jobs
                SET state = ?, findings_text = ?, confidence = ?,
                    completed_at = ?, updated_at = ?
                WHERE case_id = ?
            """, (DONE, findings_text, confidence, now, now, case_id))

    def record_request(self, case_id: str, raw: bytes | str, checklist_rows: list[dict] | None) -> None:
        """Keep the case request as received, and the checklist rows read
        from it, on the job row."""
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE audit_jobs SET cartograph_request = ?, checklist_rows = ?, updated_at = ? WHERE case_id = ?",
                (text, json.dumps(checklist_rows or [], default=str), time.time(), case_id),
            )

    def record_delivery(self, case_id: str, response: dict[str, Any]) -> None:
        """Store what the consumer returned when the extraction was handed over.

        The scan id in that response is the only handle for finding the case
        on their side. Logging it and discarding it meant that whenever the
        question came up — where did this end up — the answer had already
        scrolled past.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE audit_jobs SET cartograph_delivery = ?, updated_at = ? "
                "WHERE case_id = ?",
                (json.dumps(response, default=str), time.time(), case_id),
            )

    def record_import_result(self, case_id: str, payload: dict[str, Any]) -> None:
        """Store the consumer's report of how storing the extraction went.

        Separate from the delivery because it arrives separately: their
        ingest returns before its background job runs the inserts. A case
        with a delivery and no import result is one they accepted and never
        confirmed — which is exactly the silence this endpoint exists to
        break.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE audit_jobs SET cartograph_import_result = ?, "
                "cartograph_import_at = ?, updated_at = ? WHERE case_id = ?",
                (json.dumps(payload, default=str), time.time(), time.time(), case_id),
            )

    def record_findings_feedback(self, case_id: str, payload: dict[str, Any]) -> dict[str, int]:
        """Store a reviewer's verdicts on our findings and the findings the
        reviewer added by hand, from one findings_feedback event.

        Rows are keyed on (case, source, finding key, description), so a
        second event for the same case replaces earlier verdicts rather
        than stacking them. Returns how many of each were stored.
        """
        now = time.time()
        scan_id = payload.get("scan_id")
        scan_id = str(scan_id) if scan_id is not None else None
        verdicts = [v for v in (payload.get("verdicts") or []) if isinstance(v, dict)]
        manual = [m for m in (payload.get("manual_findings") or []) if isinstance(m, dict)]
        with self._lock, self._connect() as conn:
            for v in verdicts:
                key = str(v.get("finding_key") or "").strip()
                if not key:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO finding_feedback (case_id, scan_id, source, finding_key, code, "
                    "verdict, verdict_reason, description, page, subject_label, matched_checklist_item, raw, received_at) "
                    "VALUES (?, ?, 'runpod', ?, ?, ?, ?, '', NULL, NULL, NULL, ?, ?)",
                    (case_id, scan_id, key, key.split(":", 1)[0], (v.get("verdict") or "").strip().lower() or None,
                     v.get("verdict_reason"), json.dumps(v, default=str), now),
                )
            for m in manual:
                description = str(m.get("description") or "").strip()
                if not description:
                    continue
                item = m.get("matched_checklist_item")
                conn.execute(
                    "INSERT OR REPLACE INTO finding_feedback (case_id, scan_id, source, finding_key, code, "
                    "verdict, verdict_reason, description, page, subject_label, matched_checklist_item, raw, received_at) "
                    "VALUES (?, ?, 'manual', '', NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)",
                    (case_id, scan_id, description, m.get("page") if isinstance(m.get("page"), int) else None,
                     m.get("subject_label"), json.dumps(item, default=str) if item is not None else None,
                     json.dumps(m, default=str), now),
                )
        return {"verdicts": len(verdicts), "manual_findings": len(manual)}

    def feedback_summary(self) -> list[dict[str, Any]]:
        """Per finding code: how many verdicts, how many valid, and the
        precision that implies. The manual rows are counted separately."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT code, COUNT(*) AS n, SUM(CASE WHEN verdict = 'valid' THEN 1 ELSE 0 END) AS valid "
                "FROM finding_feedback WHERE source = 'runpod' AND verdict IN ('valid', 'invalid') "
                "GROUP BY code ORDER BY n DESC"
            ).fetchall()
            manual = conn.execute(
                "SELECT COUNT(*) FROM finding_feedback WHERE source = 'manual'"
            ).fetchone()[0]
        out = [
            {"code": r["code"], "verdicts": r["n"], "valid": r["valid"],
             "precision": round(r["valid"] / r["n"], 2) if r["n"] else None}
            for r in rows
        ]
        out.append({"code": "(manual findings)", "verdicts": manual, "valid": None, "precision": None})
        return out

    def mark_comparison_failed(self, case_id: str, error: str) -> None:
        self._set_state(case_id, COMPARISON_FAILED, error=error)

    def _set_state(self, case_id: str, state: str, error: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            now = time.time()
            conn.execute("""
                UPDATE audit_jobs SET state = ?, updated_at = ?, error = ?
                WHERE case_id = ?
            """, (state, now, error, case_id))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM audit_jobs WHERE case_id = ?", (case_id,)
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            for json_field in ("extraction_result", "mulesoft_snapshot"):
                if data.get(json_field):
                    try:
                        data[json_field] = json.loads(data[json_field])
                    except json.JSONDecodeError:
                        pass
            return data

    def list_by_state(self, state: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_jobs WHERE state = ? ORDER BY updated_at",
                (state,),
            ).fetchall()
            return [dict(r) for r in rows]

    _TERMINAL_STATES = (
        DONE, EXTRACTION_FAILED, COMPARISON_FAILED, MULESOFT_TIMEOUT,
    )

    def delete_terminal_older_than(self, cutoff_ts: float) -> int:
        """Delete terminal-state rows whose updated_at is older than cutoff.

        Used by the retention sweep. In-flight states are never deleted
        regardless of age; the watchdog handles those.
        """
        placeholders = ",".join("?" * len(self._TERMINAL_STATES))
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM audit_jobs "
                f"WHERE state IN ({placeholders}) AND updated_at < ?",
                (*self._TERMINAL_STATES, cutoff_ts),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Watchdog support — detect & recover wedged rows
    # ------------------------------------------------------------------

    def list_wedged(
        self,
        states: tuple[str, ...],
        stale_seconds: float,
    ) -> list[dict[str, Any]]:
        """Find rows in the given states whose updated_at is older than now-stale_seconds.

        Used by the watchdog to detect cases that started processing but
        never reached a terminal state — typically because the worker
        died mid-flight (deploy, OOM, crash).
        """
        if not states:
            return []
        cutoff = time.time() - stale_seconds
        placeholders = ",".join("?" * len(states))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT case_id, case_number, state, retry_count, updated_at, "
                f"source "
                f"FROM audit_jobs "
                f"WHERE state IN ({placeholders}) AND updated_at < ?",
                (*states, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]


_SINGLETON: JobStore | None = None
_SINGLETON_LOCK = threading.Lock()


def get_job_store(db_path: Path) -> JobStore:
    """Module-level singleton so every handler and thread shares one store."""
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = JobStore(db_path)
    return _SINGLETON
