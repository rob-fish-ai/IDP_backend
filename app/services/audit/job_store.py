"""Persistent job state for IDP audit cases.

State machine per case:
  pending           — initial
  extracting        — IDP extraction running
  extracted         — extraction done, awaiting MuleSoft
  comparing         — comparison running
  done              — findings produced
  extraction_failed — IDP extraction errored
  comparison_failed — comparison errored
  mulesoft_timeout  — MuleSoft webhook never arrived

SQLite is sufficient for v1: small write volume, single-process worker,
survives Render restarts when stored in a persistent volume.
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
PENDING = "pending"
# Accepted and handed to a worker that has not started yet. Distinct from
# PENDING because the two answer the dedupe question differently: a PENDING
# row is waiting for a poller to claim it and must stay claimable, while a
# QUEUED row already has a worker committed to it and a second notification
# would run the same case twice.
QUEUED = "queued"
EXTRACTING = "extracting"
EXTRACTED = "extracted"
COMPARING = "comparing"
DONE = "done"
EXTRACTION_FAILED = "extraction_failed"
COMPARISON_FAILED = "comparison_failed"
MULESOFT_TIMEOUT = "mulesoft_timeout"

# States in which some worker already owns the case, so a second arrival for
# the same case_id is a duplicate rather than a new request. PENDING is
# deliberately absent: it means "nobody has claimed this yet", which is what
# the watchdog resets a wedged Salesforce row to so the next poll re-runs it.
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
    cartograph_import_at REAL
);

CREATE INDEX IF NOT EXISTS idx_state ON audit_jobs(state);
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
        source: str = "salesforce",
        claimed: bool = False,
    ) -> dict[str, Any]:
        """Create or refresh a pending job. Idempotent.

        If a job already exists in a state some worker owns, return as-is.
        If it's terminal (done/failed) or unclaimed, recreate.

        `claimed` is for callers that queue the work themselves rather than
        leaving the row for a poller to pick up — the row is created already
        owned, so a duplicate notification arriving before the worker starts
        is recognized as a duplicate instead of queuing the case twice.
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
                    content_document_id, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    case_number = excluded.case_number,
                    state = excluded.state,
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
                content_document_id, source, now, now,
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

    def mark_mulesoft_done(self, case_id: str) -> None:
        """Record that MuleSoft signaled completion. INSERTs a stub if the
        case is unknown so the signal isn't lost when webhook 2 races
        ahead of webhook 1.
        """
        with self._lock, self._connect() as conn:
            now = time.time()
            cur = conn.execute(
                "UPDATE audit_jobs SET mulesoft_done_at = ?, updated_at = ? "
                "WHERE case_id = ?",
                (now, now, case_id),
            )
            if cur.rowcount == 0:
                # Out-of-order signal: webhook 2 arrived before webhook 1.
                # Insert a stub so the timestamp is preserved and a later
                # extraction trigger can find it.
                conn.execute("""
                    INSERT INTO audit_jobs (
                        case_id, state, created_at, updated_at, mulesoft_done_at
                    ) VALUES (?, ?, ?, ?, ?)
                """, (case_id, PENDING, now, now, now))
                logger.warning(
                    "mark_mulesoft_done: case %s unknown — created stub "
                    "to preserve signal", case_id,
                )

    def try_claim_for_comparison(self, case_id: str) -> bool:
        """Atomically transition EXTRACTED → COMPARING. Returns True if this
        caller won the claim. Used to prevent duplicate concurrent comparisons
        when webhook 2 and the post-extraction trigger race.
        """
        with self._lock, self._connect() as conn:
            now = time.time()
            cur = conn.execute("""
                UPDATE audit_jobs SET state = ?, updated_at = ?
                WHERE case_id = ? AND state = ?
            """, (COMPARING, now, case_id, EXTRACTED))
            return cur.rowcount > 0

    def mark_comparing(self, case_id: str) -> None:
        self._set_state(case_id, COMPARING)

    def mark_done(
        self,
        case_id: str,
        findings_text: str,
        confidence: float,
        mulesoft_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Mark a case done and persist the MuleSoft data used for the
        comparison. The snapshot lets the dashboard render exactly what
        the analyst's findings were computed against, even if Salesforce
        changes later.
        """
        with self._lock, self._connect() as conn:
            now = time.time()
            snapshot_json = (
                json.dumps(mulesoft_snapshot, default=str)
                if mulesoft_snapshot is not None
                else None
            )
            conn.execute("""
                UPDATE audit_jobs
                SET state = ?, findings_text = ?, confidence = ?,
                    mulesoft_snapshot = ?,
                    completed_at = ?, updated_at = ?
                WHERE case_id = ?
            """, (DONE, findings_text, confidence, snapshot_json, now, now, case_id))

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

    def list_all(
        self,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List jobs with optional state filter + pagination.

        Excludes the bulky extraction_result blob — callers fetch the full
        record via get(case_id) when they need it.
        """
        cols = (
            "case_id, case_number, state, cert_type, funding_program, "
            "confidence, error, created_at, updated_at, extracted_at, "
            "mulesoft_done_at, completed_at"
        )
        with self._connect() as conn:
            if state:
                rows = conn.execute(
                    f"SELECT {cols} FROM audit_jobs WHERE state = ? "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (state, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {cols} FROM audit_jobs "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [dict(r) for r in rows]

    def count(self, state: str | None = None) -> int:
        with self._connect() as conn:
            if state:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM audit_jobs WHERE state = ?",
                    (state,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM audit_jobs",
                ).fetchone()
            return row["n"]

    def list_known_case_ids(self) -> set[str]:
        """Return all case IDs IDP has touched (in any state).

        Used by the poller to exclude already-known cases from the
        Salesforce SOQL query so the same case isn't fetched repeatedly.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT case_id FROM audit_jobs"
            ).fetchall()
            return {r["case_id"] for r in rows}

    def list_failed_case_ids(self) -> set[str]:
        """Case IDs in failed states — eligible for retry on next poll."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT case_id FROM audit_jobs
                WHERE state IN (?, ?, ?)
            """, (EXTRACTION_FAILED, COMPARISON_FAILED, MULESOFT_TIMEOUT)).fetchall()
            return {r["case_id"] for r in rows}

    # ------------------------------------------------------------------
    # Admin / maintenance
    # ------------------------------------------------------------------

    def delete_by_state(self, state: str) -> int:
        """Delete all rows in the given state. Returns rowcount.

        Used by the admin reset endpoint to re-queue completed audits
        without wiping in-flight work.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM audit_jobs WHERE state = ?", (state,),
            )
            return cur.rowcount

    def delete_by_case_ids(self, case_ids: list[str]) -> int:
        """Delete rows for specific case IDs. Returns rowcount.

        SQLite has a 999-param ceiling for IN clauses; we chunk to be safe.
        """
        if not case_ids:
            return 0
        total = 0
        with self._lock, self._connect() as conn:
            for i in range(0, len(case_ids), 500):
                chunk = case_ids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f"DELETE FROM audit_jobs WHERE case_id IN ({placeholders})",
                    chunk,
                )
                total += cur.rowcount
        return total

    def delete_all(self) -> int:
        """Delete every row. Returns rowcount. Nuclear option."""
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM audit_jobs")
            return cur.rowcount

    _TERMINAL_STATES = (
        DONE, EXTRACTION_FAILED, COMPARISON_FAILED, MULESOFT_TIMEOUT,
    )

    def delete_terminal_older_than(self, cutoff_ts: float) -> int:
        """Delete terminal-state rows whose updated_at is older than cutoff.

        Used by the retention sweep to cap unbounded growth. Only touches
        rows the pipeline is done with — in-flight states (pending,
        extracting, extracted, comparing) are never deleted regardless of
        age (the watchdog handles those).
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

    def reset_to_pending(self, case_id: str) -> bool:
        """Reset a row back to PENDING state and increment retry_count.

        Preserves identity fields (case_number, cert_type, funding_program,
        content_document_id) but wipes anything from the prior attempt
        (extraction_result, findings, timestamps, error). Returns True
        if a row was reset, False if no row existed.
        """
        with self._lock, self._connect() as conn:
            now = time.time()
            cur = conn.execute("""
                UPDATE audit_jobs
                SET state = ?,
                    retry_count = retry_count + 1,
                    updated_at = ?,
                    error = NULL,
                    extraction_result = NULL,
                    findings_text = NULL,
                    confidence = NULL,
                    extracted_at = NULL,
                    mulesoft_done_at = NULL,
                    completed_at = NULL
                WHERE case_id = ?
            """, (PENDING, now, case_id))
            return cur.rowcount > 0

    def increment_retry(self, case_id: str) -> int:
        """Bump retry_count and return the new value (0 if row missing).

        Used by the retryable-failure paths so automated re-attempts share
        one bounded budget with the watchdog (reset_to_pending also
        increments). upsert_pending deliberately preserves retry_count, so
        the count survives poll-cycle re-queues.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE audit_jobs SET retry_count = retry_count + 1 "
                "WHERE case_id = ?",
                (case_id,),
            )
            row = conn.execute(
                "SELECT retry_count FROM audit_jobs WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            return int(row["retry_count"]) if row else 0


_SINGLETON: JobStore | None = None
_SINGLETON_LOCK = threading.Lock()


def get_job_store(db_path: Path) -> JobStore:
    """Module-level singleton so all webhook handlers share one store."""
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = JobStore(db_path)
    return _SINGLETON
