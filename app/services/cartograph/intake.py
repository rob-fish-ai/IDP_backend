"""The line a notified case waits in before it is audited.

A Cartograph notification used to become a background task of the
process at once, with no limit on how many ran together. On an afternoon
when 34 cases arrived in one hour, 26 packets were being rendered and
read in one process at the same time, the container's 16 GB memory cap
was reached, and the kernel killed the engine with all of them half done.

Now a notification joins this line. At most `audit_pipeline_concurrency`
cases are audited at once — the same bound the poller and the resume path
use — and the rest wait in the order they arrived, in the `queued` state
their row already has. A waiting row's heartbeat is kept, so the watchdog
never mistakes a long wait for a dead worker; and if the process does stop
while cases wait, their rows are still owned and the next start runs them
again (resume.py), so nothing in the line is lost.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

logger = logging.getLogger(__name__)


class Intake:
    """A bounded worker pool with a heartbeat for the cases still waiting.

    `run` is called with the submitted keyword arguments (it is `audit_case`
    in the engine). `store` may be None, in which case no heartbeat is sent.
    """

    def __init__(self, *, workers: int, run, store=None, heartbeat_seconds: float = 60.0):
        self.workers = max(1, int(workers or 1))
        self._run = run
        self._store = store
        self._heartbeat = max(0.01, float(heartbeat_seconds))
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="idp-audit")
        self._lock = threading.Lock()
        self._waiting: dict[str, float] = {}   # case_ref -> when it joined the line
        self._running: set[str] = set()
        self._stop = threading.Event()
        self._keeper = threading.Thread(target=self._keep_alive, name="idp-intake-heartbeat", daemon=True)
        self._keeper.start()

    # -- submitting -------------------------------------------------------

    def submit(self, **kwargs) -> Future:
        case_ref = kwargs["case_ref"]
        with self._lock:
            self._waiting[case_ref] = time.time()
            running, ahead = len(self._running), len(self._waiting) - 1
        if running >= self.workers:
            logger.info("case_ref=%s queued: %d case(s) running, %d ahead of it in the line",
                        case_ref, running, ahead)
        return self._pool.submit(self._audit, kwargs)

    def _audit(self, kwargs: dict) -> None:
        case_ref = kwargs["case_ref"]
        with self._lock:
            joined = self._waiting.pop(case_ref, None)
            self._running.add(case_ref)
        waited = time.time() - joined if joined else 0.0
        if waited >= self._heartbeat:
            logger.info("case_ref=%s waited %ds in the line for a worker", case_ref, int(waited))
        try:
            self._run(**kwargs)
        finally:
            with self._lock:
                self._running.discard(case_ref)

    # -- the heartbeat for the ones still waiting --------------------------

    def _keep_alive(self) -> None:
        while not self._stop.wait(self._heartbeat):
            with self._lock:
                waiting = list(self._waiting)
            if self._store is None:
                continue
            for case_ref in waiting:
                try:
                    self._store.touch(case_ref)
                except Exception:
                    logger.exception("Could not keep case_ref=%s alive while it waits", case_ref)

    # -- introspection and shutdown ----------------------------------------

    def waiting(self) -> list[str]:
        with self._lock:
            return sorted(self._waiting, key=self._waiting.get)

    def running(self) -> list[str]:
        with self._lock:
            return sorted(self._running)

    def shutdown(self) -> None:
        """Stop taking cases. The ones waiting are dropped here — their rows
        stay owned and run again at the next start; the ones running finish
        if the process is given the time."""
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)


# The engine's one intake, built on first use so the settings are read
# once the process is configured, not at import.
_INTAKE: Intake | None = None
_INTAKE_LOCK = threading.Lock()


def get_intake(settings=None) -> Intake:
    global _INTAKE
    with _INTAKE_LOCK:
        if _INTAKE is None:
            from app.core.dependencies import get_settings
            from app.services.audit.job_store import get_job_store
            from app.services.cartograph.tasks import audit_case
            settings = settings or get_settings()
            # Well inside the watchdog's patience, so a waiting row is never
            # the stalest thing it sees.
            heartbeat = min(60.0, max(5.0, float(getattr(settings, "audit_watchdog_seconds", 1800)) / 4))
            _INTAKE = Intake(
                workers=settings.audit_pipeline_concurrency, run=audit_case,
                store=get_job_store(settings.audit_job_db), heartbeat_seconds=heartbeat,
            )
            logger.info("Intake ready: %d case(s) audited at once, the rest wait in line", _INTAKE.workers)
        return _INTAKE


def enqueue_case(**kwargs) -> Future:
    """Put a notified case in the line. Returns at once."""
    return get_intake().submit(**kwargs)


def shutdown_intake() -> None:
    global _INTAKE
    with _INTAKE_LOCK:
        intake, _INTAKE = _INTAKE, None
    if intake is not None:
        intake.shutdown()
