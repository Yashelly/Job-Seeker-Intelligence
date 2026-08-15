"""In-process daily scheduler for the always-on web dashboard.

The dashboard now runs continuously (autostart at logon), so a small background
thread can fire the daily collection + Telegram summary at a configured local
time without relying on the Windows Task Scheduler. Configuration is persisted
next to the database as ``scheduler.json`` and edited from the ``/schedule`` page.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_SOURCES = ("cvbankas", "hh", "justjoin")
DEFAULT_CHECK_INTERVAL = 30.0

# A runner starts the daily job and returns its job id, or raises. It is injected
# by the web layer so this module stays free of FastAPI / run_batch imports.
Runner = Callable[["ScheduleConfig"], int]


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
    last_run_date: str = ""  # local YYYY-MM-DD; guards one-run-per-day
    last_status: str = ""  # started | conflict | error | ""
    last_run_at: str = ""  # local ISO timestamp of the last attempt
    last_job_id: int | None = None

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
    return cfg


def save_schedule(path: Path, config: ScheduleConfig) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


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
        config: ScheduleConfig | None = None,
        clock: Callable[[], datetime] = datetime.now,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        self._path = Path(config_path)
        self._runner = runner
        self._config = config if config is not None else load_schedule(self._path)
        self._clock = clock
        self._interval = check_interval
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_loop_error = ""

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
        try:
            save_schedule(self._path, self._config)
        except OSError:
            pass

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
            except Exception as error:  # noqa: BLE001 - a scheduler must never die on error
                self._last_loop_error = repr(error)
            self._wake.wait(timeout=self._interval)
            self._wake.clear()

    # -- core decision ---------------------------------------------------
    def tick(self, now: datetime | None = None) -> bool:
        """Fire the job if it is due. Returns True if a run was started."""
        moment = now or self._clock()
        today = moment.date().isoformat()
        with self._lock:
            if not self._config.enabled:
                return False
            if self._config.last_run_date == today:
                return False
            hour, minute = self._config.time_hm()
            if (moment.hour, moment.minute) < (hour, minute):
                return False
            config_snapshot = ScheduleConfig(**asdict(self._config))

        # Run the job outside the lock; the runner may block briefly on start.
        try:
            job_id = self._runner(config_snapshot)
            status_text, run_date = "started", today
        except SchedulerBusyError:  # busy: retry on the next tick
            with self._lock:
                self._config.last_status = "conflict"
                self._config.last_run_at = moment.isoformat(timespec="seconds")
                self._persist_locked()
            return False
        except Exception as error:  # noqa: BLE001 - record and don't retry-spin today
            with self._lock:
                self._config.last_status = f"error: {error}"[:200]
                self._config.last_run_at = moment.isoformat(timespec="seconds")
                self._config.last_run_date = today  # avoid tight error loop
                self._persist_locked()
            return False

        with self._lock:
            self._config.last_run_date = run_date
            self._config.last_status = status_text
            self._config.last_run_at = moment.isoformat(timespec="seconds")
            self._config.last_job_id = int(job_id) if job_id is not None else None
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
