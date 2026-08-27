"""In-process daily scheduler for the always-on web dashboard.

The dashboard now runs continuously (autostart at logon), so a small background
thread can fire the daily collection + Telegram summary at a configured local
time without relying on the Windows Task Scheduler. Configuration is persisted
next to the database as ``scheduler.json`` and edited from the ``/schedule`` page.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_SOURCES = ("cvbankas", "hh", "justjoin")
DEFAULT_CHECK_INTERVAL = 30.0
# A daily job that starts but then fails is retried, bounded to this many starts
# per day so a job that fails instantly (e.g. a stranded lease -> exit 3) cannot
# spin forever; after the cap the day is marked durably ``failed``.
MAX_DAILY_ATTEMPTS = 3

# A runner starts the daily job and returns its job id, or raises. It is injected
# by the web layer so this module stays free of FastAPI / run_batch imports.
Runner = Callable[["ScheduleConfig"], int]
# Resolves a started job id to its current job status
# ("running" | "paused" | "done" | "error" | "cancelled"), or ``None`` if the job
# is unknown (e.g. the process restarted and the in-memory job is gone). Injected
# so the scheduler can confirm a run actually *succeeded* rather than merely
# starting -- otherwise a job that fails after launch is recorded as a done day.
OutcomeGetter = Callable[[int], str | None]


class ScheduleError(ValueError):
    """Raised when a schedule configuration is invalid."""


class SchedulerBusyError(Exception):
    """Injected runner raises this when a job is already active (retry later)."""


def normalize_time(value: str) -> str:
    """Return a validated ``HH:MM`` string or raise ``ScheduleError``."""
    text = str(value or "").strip()
    if not _TIME_RE.match(text):
        raise ScheduleError("Time must be in HH:MM 24-hour format, e.g. 19:00.")
    return text


def _positive_int(value: object, *, default: int) -> int:
    """Coerce an arbitrary persisted value into a positive int, else ``default``."""
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass
class ScheduleConfig:
    """Persisted schedule settings plus last-run bookkeeping."""

    enabled: bool = False
    time: str = "19:00"
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    keywords: list[str] = field(default_factory=list)
    limit: int = 10
    max_pages: int = 1
    analysis_strategy: str = "ai"
    # Bookkeeping (not user-edited directly):
    last_run_date: str = ""  # local YYYY-MM-DD; guards one-run-per-day (set only on success)
    last_status: str = ""  # running | completed | failed | cancelled | conflict | error | started | ""
    last_run_at: str = ""  # local ISO timestamp of the last attempt
    last_job_id: int | None = None
    attempts: int = 0  # daily-job starts attempted today (bounds retry after failure)
    last_attempt_date: str = ""  # local YYYY-MM-DD the attempt counter belongs to

    def time_hm(self) -> tuple[int, int]:
        hour, minute = self.time.split(":")
        return int(hour), int(minute)


def load_schedule(path: Path) -> ScheduleConfig:
    """Load a schedule from ``path``; return defaults if missing or unreadable."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ScheduleConfig()
    if not isinstance(raw, dict):
        return ScheduleConfig()
    defaults = ScheduleConfig()
    known = set(asdict(defaults))
    merged = {**asdict(defaults), **{k: v for k, v in raw.items() if k in known}}
    cfg = ScheduleConfig(**merged)
    # Defensive coercion for values that arrived from disk. The whole point is
    # that scheduler.json may be hand-edited or partially corrupted, so every
    # field is coerced with a fallback and nothing raises out of load_schedule.
    cfg.sources = [str(s) for s in cfg.sources] if isinstance(cfg.sources, list) else list(DEFAULT_SOURCES)
    cfg.keywords = [str(k) for k in cfg.keywords] if isinstance(cfg.keywords, list) else []
    cfg.enabled = bool(cfg.enabled)
    try:
        cfg.time = normalize_time(cfg.time)
    except ScheduleError:
        cfg.time = "19:00"
    cfg.limit = _positive_int(cfg.limit, default=10)
    cfg.max_pages = _positive_int(cfg.max_pages, default=1)
    cfg.analysis_strategy = str(cfg.analysis_strategy or "ai")
    cfg.last_run_date = str(cfg.last_run_date or "")
    cfg.attempts = max(0, _positive_int(cfg.attempts, default=0))
    cfg.last_attempt_date = str(cfg.last_attempt_date or "")
    return cfg


def save_schedule(path: Path, config: ScheduleConfig) -> None:
    """Persist the schedule atomically: a crash mid-write leaves the prior file.

    Writing straight over the target risks a torn/empty ``scheduler.json`` on
    power loss or a full disk (audit finding #6). Instead write a sibling temp
    file, flush+fsync it, then ``os.replace`` -- an atomic rename on the same
    filesystem -- so a reader ever sees only the old or the new complete file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(config), indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class DailyScheduler:
    """Fires a daily job at a configured local time, at most once per day.

    The check loop runs on a daemon thread and wakes every ``check_interval``
    seconds (or immediately when settings change). ``clock`` returns local now
    and is injectable for tests.
    """

    def __init__(
        self,
        config_path: Path | str,
        runner: Runner,
        *,
        outcome_getter: OutcomeGetter | None = None,
        config: ScheduleConfig | None = None,
        clock: Callable[[], datetime] = datetime.now,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        self._path = Path(config_path)
        self._runner = runner
        self._outcome_getter = outcome_getter
        self._config = config if config is not None else load_schedule(self._path)
        self._clock = clock
        self._interval = check_interval
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_loop_error = ""
        self._last_persist_error = ""

    # -- configuration ---------------------------------------------------
    @property
    def config(self) -> ScheduleConfig:
        with self._lock:
            return ScheduleConfig(**asdict(self._config))

    def update(
        self,
        *,
        enabled: bool,
        time: str,
        sources: list[str],
        keywords: list[str],
        limit: int,
        max_pages: int,
        analysis_strategy: str,
    ) -> ScheduleConfig:
        """Replace the user-editable settings, persist, and wake the loop."""
        normalized_time = normalize_time(time)
        with self._lock:
            self._config.enabled = bool(enabled)
            self._config.time = normalized_time
            self._config.sources = list(sources)
            self._config.keywords = list(keywords)
            self._config.limit = int(limit) if int(limit) > 0 else 1
            self._config.max_pages = int(max_pages) if int(max_pages) > 0 else 1
            self._config.analysis_strategy = analysis_strategy
            self._persist_locked()
            snapshot = ScheduleConfig(**asdict(self._config))
        self._wake.set()
        return snapshot

    def _persist_locked(self) -> None:
        # Persisting must never crash the scheduler thread, but a failure is now
        # recorded and surfaced instead of silently swallowed (audit finding #6).
        try:
            save_schedule(self._path, self._config)
            self._last_persist_error = ""
        except OSError as error:
            self._last_persist_error = repr(error)
            print(f"[scheduler] failed to persist schedule to {self._path}: {error}")

    @property
    def last_persist_error(self) -> str:
        with self._lock:
            return self._last_persist_error

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="daily-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:
                self._last_loop_error = repr(error)
            self._wake.wait(timeout=self._interval)
            self._wake.clear()

    # -- core decision ---------------------------------------------------
    def tick(self, now: datetime | None = None) -> bool:
        """Fire the job if it is due. Returns True if a run was started.

        When an ``outcome_getter`` is configured, a started job's ``last_run_date``
        (the one-run-per-day guard) is committed only once its terminal status
        confirms success. A job that fails *after* launching is recorded as
        ``failed`` and retried, bounded by ``MAX_DAILY_ATTEMPTS`` -- rather than
        being silently treated as a completed day (audit finding #5).
        """
        moment = now or self._clock()
        today = moment.date().isoformat()
        with self._lock:
            # Resolve a previously-started async job before any fire decision.
            if self._config.last_status == "running" and self._outcome_getter is not None:
                if not self._resolve_pending_locked(moment, today):
                    return False  # still in flight -- do not fire again or mark done

            if not self._config.enabled:
                return False
            if self._config.last_run_date == today:
                return False
            hour, minute = self._config.time_hm()
            if (moment.hour, moment.minute) < (hour, minute):
                return False

            # Reset the per-day attempt counter when the day rolls over.
            if self._config.last_attempt_date != today:
                self._config.attempts = 0
                self._config.last_attempt_date = today
            if self._config.attempts >= MAX_DAILY_ATTEMPTS:
                # Retries exhausted: stop for the day, but record it as failed
                # rather than a success so the durable state stays honest.
                self._config.last_run_date = today
                if self._config.last_status not in {"failed", "cancelled"}:
                    self._config.last_status = "failed"
                self._persist_locked()
                return False

            config_snapshot = ScheduleConfig(**asdict(self._config))

        # Run the job outside the lock; the runner may block briefly on start.
        try:
            job_id = self._runner(config_snapshot)
        except SchedulerBusyError:  # busy: retry on the next tick
            with self._lock:
                self._config.last_status = "conflict"
                self._config.last_run_at = moment.isoformat(timespec="seconds")
                self._persist_locked()
            return False
        except Exception as error:
            with self._lock:
                self._config.last_status = f"error: {error}"[:200]
                self._config.last_run_at = moment.isoformat(timespec="seconds")
                self._config.last_run_date = today  # start failed hard -> avoid tight error loop
                self._persist_locked()
            return False

        with self._lock:
            self._config.attempts += 1
            self._config.last_run_at = moment.isoformat(timespec="seconds")
            self._config.last_job_id = int(job_id) if job_id is not None else None
            if self._outcome_getter is None:
                # No async outcome channel: a successful start guards one-per-day
                # (legacy behavior; the daily job's own logs remain the record).
                self._config.last_run_date = today
                self._config.last_status = "started"
            else:
                # Success is confirmed later from the job's terminal status.
                self._config.last_status = "running"
            self._persist_locked()
        return True

    def _resolve_pending_locked(self, moment: datetime, today: str) -> bool:
        """Resolve the previously-started job. Returns False while still in flight.

        Caller holds ``self._lock``. ``done`` confirms the day; ``cancelled``
        records a user abort (no auto-retry today); anything else -- including a
        job lost to a process restart (``None``) -- is a failure that leaves
        ``last_run_date`` unset so a bounded retry can fire.
        """
        assert self._outcome_getter is not None
        job_id = self._config.last_job_id
        outcome = self._outcome_getter(int(job_id)) if job_id is not None else None
        if outcome in {"running", "paused"}:
            return False  # genuinely still running
        self._config.last_job_id = None
        if outcome == "done":
            self._config.last_run_date = today
            self._config.last_status = "completed"
        elif outcome == "cancelled":
            self._config.last_run_date = today  # aborted by user; don't auto-retry today
            self._config.last_status = "cancelled"
        else:  # "error", unknown, or lost job -> failed; retry if attempt budget remains
            self._config.last_status = "failed"
        self._config.last_run_at = self._config.last_run_at or moment.isoformat(timespec="seconds")
        self._persist_locked()
        return True

    def next_run(self, now: datetime | None = None) -> datetime:
        """Return the next datetime the job is expected to fire."""
        moment = now or self._clock()
        hour, minute = self._config.time_hm()
        candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        already_ran_today = self._config.last_run_date == moment.date().isoformat()
        if already_ran_today or (moment.hour, moment.minute) >= (hour, minute):
            candidate = candidate + timedelta(days=1)
        return candidate

    def snapshot(self, now: datetime | None = None) -> dict[str, object]:
        cfg = self.config
        data = asdict(cfg)
        data["next_run_at"] = self.next_run(now).isoformat(timespec="minutes")
        return data
