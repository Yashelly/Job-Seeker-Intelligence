from __future__ import annotations

import io
import threading
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count

# A long collection run streams a lot of stdout. Cap the retained log so a
# single job cannot grow unbounded in memory, keeping the most recent output.
MAX_LOG_CHARS = 256_000
_TRUNCATION_NOTICE = "…[earlier output truncated]…\n"
# Completed jobs live only in memory (a local single-user dashboard); keep a
# short history so restarting search/import repeatedly cannot leak memory.
MAX_COMPLETED_JOBS = 20


class JobConflictError(RuntimeError):
    """Raised when a new job is started while another is still running."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class JobControl:
    """Cooperative pause/cancel signal shared with a running job.

    A long collection cannot be interrupted mid network request, but the
    collection loops call :meth:`wait_if_paused` and check :meth:`is_cancelled`
    between vacancies, so Pause/Resume/End take effect at the next boundary.
    Thread-safe: the same control is read by several parallel source workers.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._not_paused = threading.Event()
        self._not_paused.set()

    def pause(self) -> None:
        if not self._cancelled.is_set():
            self._not_paused.clear()

    def resume(self) -> None:
        self._not_paused.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._not_paused.set()  # unblock any worker parked in wait_if_paused

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def is_paused(self) -> bool:
        return not self._not_paused.is_set() and not self._cancelled.is_set()

    def wait_if_paused(self) -> None:
        """Block while paused; returns immediately once resumed or cancelled."""
        self._not_paused.wait()


@dataclass(slots=True)
class Job:
    id: int
    kind: str
    status: str = "running"  # running | paused | done | error | cancelled
    exit_code: int | None = None
    log: str = ""
    error: str = ""
    started_at: str = field(default_factory=_now_iso)
    finished_at: str = ""
    control: JobControl = field(default_factory=JobControl)


class _LogBuffer(io.StringIO):
    """Thread-safe stdout sink that appends captured text onto a Job.

    The retained log is bounded to ``MAX_LOG_CHARS``: once exceeded, the oldest
    output is dropped and a one-line notice is kept at the front so the tail
    (the part a user actually watches) always survives.
    """

    def __init__(self, job: Job, lock: threading.Lock) -> None:
        super().__init__()
        self._job = job
        self._lock = lock

    def write(self, value: str) -> int:
        with self._lock:
            log = self._job.log + value
            if len(log) > MAX_LOG_CHARS:
                keep = MAX_LOG_CHARS - len(_TRUNCATION_NOTICE)
                log = _TRUNCATION_NOTICE + log[-keep:]
            self._job.log = log
        return len(value)


class JobManager:
    """Runs one background job at a time, capturing its stdout into a bounded log."""

    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._ids = count(1)
        self._lock = threading.Lock()

    def _has_running(self) -> bool:
        return any(job.status in {"running", "paused"} for job in self._jobs.values())

    def _prune_locked(self) -> None:
        completed = sorted(
            (job for job in self._jobs.values() if job.status not in {"running", "paused"}),
            key=lambda job: job.id,
        )
        for job in completed[: max(0, len(completed) - MAX_COMPLETED_JOBS)]:
            self._jobs.pop(job.id, None)

    def start(self, kind: str, target: Callable[[JobControl], int]) -> Job:
        with self._lock:
            if self._has_running():
                raise JobConflictError("Another job is still running. Wait for it to finish.")
            self._prune_locked()
            job = Job(id=next(self._ids), kind=kind)
            self._jobs[job.id] = job

        thread = threading.Thread(target=self._run, args=(job, target), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, target: Callable[[JobControl], int]) -> None:
        buffer = _LogBuffer(job, self._lock)
        try:
            with redirect_stdout(buffer):
                exit_code = target(job.control)
            with self._lock:
                job.exit_code = int(exit_code) if exit_code is not None else 0
                job.status = "cancelled" if job.control.is_cancelled() else "done"
                job.finished_at = _now_iso()
        except Exception as error:
            with self._lock:
                job.error = str(error)
                job.log += f"\nERROR | {error}\n"
                job.exit_code = 1
                job.status = "error"
                job.finished_at = _now_iso()

    def pause(self, job_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "running":
                return False
            job.control.pause()
            job.status = "paused"
            return True

    def resume(self, job_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "paused":
                return False
            job.control.resume()
            job.status = "running"
            return True

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in {"running", "paused"}:
                return False
            job.control.cancel()
            # Status flips to "cancelled" once the run unwinds; keep it visible
            # as running until then so the UI doesn't claim it stopped early.
            return True

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: int) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "id": job.id,
                "kind": job.kind,
                "status": job.status,
                "exit_code": job.exit_code,
                "log": job.log,
                "error": job.error,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
            }
