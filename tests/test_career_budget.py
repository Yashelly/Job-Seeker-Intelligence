import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.sources.career_budget import (
    DEFAULT_MAX_JOBS,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_TOTAL_BYTES,
    CareerBudgetExceeded,
    RunBudget,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RunBudgetTests(unittest.TestCase):
    def test_defaults_are_finite_and_started_lazily(self) -> None:
        budget = RunBudget()

        self.assertEqual(budget.max_requests, DEFAULT_MAX_REQUESTS)
        self.assertEqual(budget.max_total_bytes, DEFAULT_MAX_TOTAL_BYTES)
        self.assertEqual(budget.max_seconds, DEFAULT_MAX_SECONDS)
        self.assertEqual(budget.max_jobs, DEFAULT_MAX_JOBS)
        self.assertIsNone(budget.started_at)

        budget.before_request()

        self.assertIsNotNone(budget.started_at)
        self.assertEqual(budget.request_count, 1)

    def test_request_budget_counts_attempts_and_raises_specialized_error(self) -> None:
        budget = RunBudget(max_requests=2)

        budget.before_request()
        budget.before_request()

        with self.assertRaisesRegex(CareerBudgetExceeded, "request budget exceeded"):
            budget.before_request()

        self.assertEqual(budget.request_count, 2)
        self.assertIsInstance(CareerBudgetExceeded("stop"), RuntimeError)
        self.assertNotIsInstance(CareerBudgetExceeded("stop"), ValueError)
        self.assertNotIsInstance(CareerBudgetExceeded("stop"), OSError)

    def test_read_limit_caps_response_to_remaining_bytes(self) -> None:
        budget = RunBudget(max_total_bytes=10)

        budget.consume_bytes(7)

        self.assertEqual(budget.read_limit(100), 3)

        budget.consume_bytes(3)
        with self.assertRaisesRegex(CareerBudgetExceeded, "byte budget exceeded"):
            budget.read_limit(1)

    def test_consume_bytes_refuses_oversize_total_without_changing_accounting(self) -> None:
        budget = RunBudget(max_total_bytes=10)
        budget.consume_bytes(8)

        with self.assertRaisesRegex(CareerBudgetExceeded, "above limit 10"):
            budget.consume_bytes(3)

        self.assertEqual(budget.total_bytes, 8)

    def test_add_job_deduplicates_urls_before_enforcing_limit(self) -> None:
        budget = RunBudget(max_jobs=2)

        self.assertTrue(budget.add_job("https://example.test/jobs/1"))
        self.assertFalse(budget.add_job("https://example.test/jobs/1"))
        self.assertTrue(budget.add_job("https://example.test/jobs/2"))

        with self.assertRaisesRegex(CareerBudgetExceeded, "job budget exceeded"):
            budget.add_job("https://example.test/jobs/3")

        self.assertEqual(budget.job_count, 2)

    def test_deadline_is_checked_at_company_boundaries_and_accounting_points(self) -> None:
        clock = FakeClock()
        budget = RunBudget(max_seconds=5, clock=clock)
        budget.start()

        clock.advance(5.1)

        with self.assertRaisesRegex(CareerBudgetExceeded, "company boundary"):
            budget.check()
        with self.assertRaisesRegex(CareerBudgetExceeded, "read"):
            budget.read_limit(1)
        with self.assertRaisesRegex(CareerBudgetExceeded, "byte accounting"):
            budget.consume_bytes(1)
        with self.assertRaisesRegex(CareerBudgetExceeded, "job discovery"):
            budget.add_job("https://example.test/jobs/1")

    def test_reset_clears_counters_and_restarts_clock(self) -> None:
        clock = FakeClock()
        budget = RunBudget(max_requests=5, clock=clock)
        budget.before_request()
        budget.consume_bytes(20)
        budget.add_job("https://example.test/jobs/1")

        clock.advance(3)
        budget.reset()

        self.assertEqual(budget.started_at, 103.0)
        self.assertEqual(budget.request_count, 0)
        self.assertEqual(budget.total_bytes, 0)
        self.assertEqual(budget.job_count, 0)

    def test_limits_reject_zero_negative_infinite_and_non_integer_values(self) -> None:
        invalid_cases = [
            {"max_requests": 0},
            {"max_total_bytes": -1},
            {"max_seconds": 0},
            {"max_seconds": math.inf},
            {"max_jobs": 1.5},
        ]

        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RunBudget(**kwargs)

        budget = RunBudget()
        with self.assertRaises(ValueError):
            budget.read_limit(0)
        with self.assertRaises(ValueError):
            budget.consume_bytes(-1)


if __name__ == "__main__":
    unittest.main()
