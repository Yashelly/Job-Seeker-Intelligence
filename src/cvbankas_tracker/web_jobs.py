from __future__ import annotations

import io
import threading
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count


class JobConflictError(RuntimeError):
    """Raised when a new job is started while another is still running."""


@dataclass(slots=True)
class Job:
    id: int
    kind: str
    status: str = "running"  # running | done | error
    exit_code: int | None = None
    log: str = ""
    error: str = ""
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


class _LogBuffer(io.StringIO):
    """Thread-safe stdout sink that appends captured text onto a Job."""

    def __init__(self, job: Job, lock: threading.Lock) -> None:
        super().__init__()
        self._job = job
        self._lock = lock

    def write(self, value: str) -> int:
        with self._lock:
            self._job.log += value
        return len(value)


class JobManager:
    """Runs one background job at a time, capturing its stdout into a growing log."""

    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._ids = count(1)
        self._lock = threading.Lock()

    def _has_running(self) -> bool:
        return any(job.status == "running" for job in self._jobs.values())

    def start(self, kind: str, target: Callable[[], int]) -> Job:
        with self._lock:
            if self._has_running():
                raise JobConflictError("Another job is still running. Wait for it to finish.")
            job = Job(id=next(self._ids), kind=kind)
            self._jobs[job.id] = job

        thread = threading.Thread(target=self._run, args=(job, target), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, target: Callable[[], int]) -> None:
        buffer = _LogBuffer(job, self._lock)
        try:
            with redirect_stdout(buffer):
                exit_code = target()
            with self._lock:
                job.exit_code = int(exit_code) if exit_code is not None else 0
                job.status = "done"
        except Exception as error:  # noqa: BLE001 - surfaced to the job log
            with self._lock:
                job.error = str(error)
                job.log += f"\nERROR | {error}\n"
                job.exit_code = 1
                job.status = "error"

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
            }
