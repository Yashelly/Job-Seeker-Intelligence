import time
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_MAX_REQUESTS = 10_000
DEFAULT_MAX_TOTAL_BYTES = 1_073_741_824
DEFAULT_MAX_SECONDS = 7_200.0
DEFAULT_MAX_JOBS = 50_000


class CareerBudgetExceeded(RuntimeError):
    """Raised when a career discovery run exceeds its configured safety budget."""


def _validate_positive_number(name: str, value: int | float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a positive finite value")
    if isinstance(value, float) and not (value < float("inf")):
        raise ValueError(f"{name} must be a positive finite value")


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


@dataclass
class RunBudget:
    """Bound request, byte, time, and discovered-job growth for one career crawl."""

    max_requests: int = DEFAULT_MAX_REQUESTS
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_seconds: float = DEFAULT_MAX_SECONDS
    max_jobs: int = DEFAULT_MAX_JOBS
    clock: Callable[[], float] = time.monotonic
    request_count: int = 0
    total_bytes: int = 0
    _started_at: float | None = field(default=None, init=False, repr=False)
    _job_urls: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_positive_int("max_requests", self.max_requests)
        _validate_positive_int("max_total_bytes", self.max_total_bytes)
        _validate_positive_number("max_seconds", self.max_seconds)
        _validate_positive_int("max_jobs", self.max_jobs)

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def job_count(self) -> int:
        return len(self._job_urls)

    def start(self) -> None:
        self._started_at = self.clock()

    def reset(self) -> None:
        self.request_count = 0
        self.total_bytes = 0
        self._job_urls.clear()
        self.start()

    def before_request(self) -> None:
        self._ensure_started()
        if self.request_count >= self.max_requests:
            raise CareerBudgetExceeded(
                f"Career discovery request budget exceeded: {self.request_count} of "
                f"{self.max_requests} requests already used"
            )
        self.request_count += 1
        self._check_deadline("request")

    def read_limit(self, per_response_limit: int) -> int:
        _validate_positive_int("per_response_limit", per_response_limit)
        self._ensure_started()
        self._check_deadline("read")
        remaining = self.max_total_bytes - self.total_bytes
        if remaining <= 0:
            raise CareerBudgetExceeded(
                f"Career discovery byte budget exceeded: {self.total_bytes} of "
                f"{self.max_total_bytes} bytes already read"
            )
        return min(per_response_limit, remaining)

    def consume_bytes(self, byte_count: int) -> None:
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ValueError("byte_count must be a non-negative integer")
        self._ensure_started()
        self._check_deadline("byte accounting")
        next_total = self.total_bytes + byte_count
        if next_total > self.max_total_bytes:
            raise CareerBudgetExceeded(
                f"Career discovery byte budget exceeded: reading {byte_count} bytes would "
                f"raise total bytes to {next_total}, above limit {self.max_total_bytes}"
            )
        self.total_bytes = next_total

    def add_job(self, url: str) -> bool:
        self._ensure_started()
        self._check_deadline("job discovery")
        if url in self._job_urls:
            return False
        if len(self._job_urls) >= self.max_jobs:
            raise CareerBudgetExceeded(
                f"Career discovery job budget exceeded: {len(self._job_urls)} unique "
                f"jobs already found, limit is {self.max_jobs}"
            )
        self._job_urls.add(url)
        return True

    def check(self) -> None:
        self._ensure_started()
        self._check_deadline("company boundary")

    def _ensure_started(self) -> None:
        if self._started_at is None:
            self.start()

    def _check_deadline(self, operation: str) -> None:
        assert self._started_at is not None
        elapsed = self.clock() - self._started_at
        if elapsed > self.max_seconds:
            raise CareerBudgetExceeded(
                f"Career discovery time budget exceeded during {operation}: elapsed "
                f"{elapsed:.3f}s is above limit {self.max_seconds:.3f}s"
            )
