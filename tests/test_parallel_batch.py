from __future__ import annotations

import threading
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from src.cvbankas_tracker.main import (
    SourceBatchResult,
    _execute_source_batches,
    run_batch,
)
from src.cvbankas_tracker.models import Vacancy
from src.cvbankas_tracker.storage import DatabaseManager


@dataclass
class StubSource:
    name: str


class BatchSource:
    vacancy_request_delay_seconds = 0
    listing_request_delay_seconds = 0

    def __init__(self, name: str, start_barrier: threading.Barrier) -> None:
        self.name = name
        self._start_barrier = start_barrier
        self.closed = False

    def collect_vacancy_urls(self, **_kwargs) -> tuple[list[str], list[str]]:
        self._start_barrier.wait()
        url = f"https://example.test/{self.name}/vacancy"
        return [url], [f"https://example.test/{self.name}/search"]

    def fetch_vacancy_page(self, _url: str) -> str:
        return "<html></html>"

    def parse_vacancy(self, _html: str, url: str) -> Vacancy:
        return Vacancy(
            source_name=self.name,
            source_id=f"{self.name}-1",
            source_url=url,
            title="Automation Engineer",
            company="Example",
            location="Remote, EU",
            salary_text="Not specified",
            requirements=["Automation"],
            responsibilities=["Build workflows"],
            raw_text="Automation Engineer remote EU",
        )

    def close(self) -> None:
        self.closed = True


class ParallelSourceBatchTests(unittest.TestCase):
    def test_source_workers_start_in_parallel_and_results_keep_source_order(self) -> None:
        sources = [StubSource("first"), StubSource("second")]
        start_barrier = threading.Barrier(len(sources), timeout=2)

        def worker(source: StubSource) -> SourceBatchResult:
            start_barrier.wait()
            return SourceBatchResult(source_name=source.name, report_rows=[])

        results = _execute_source_batches(sources, worker)

        self.assertEqual([result.source_name for result in results], ["first", "second"])
        self.assertTrue(all(result.failed_count == 0 for result in results))

    def test_one_source_worker_failure_does_not_stop_other_sources(self) -> None:
        sources = [StubSource("broken"), StubSource("healthy")]

        def worker(source: StubSource) -> SourceBatchResult:
            if source.name == "broken":
                raise RuntimeError("source failed")
            return SourceBatchResult(
                source_name=source.name,
                report_rows=[],
                attempted_count=3,
            )

        results = _execute_source_batches(sources, worker)

        self.assertEqual(results[0].failed_count, 1)
        self.assertEqual(results[1].attempted_count, 3)

    def test_run_batch_processes_two_sources_concurrently_into_one_database(self) -> None:
        start_barrier = threading.Barrier(2, timeout=2)
        sources = [
            BatchSource("source_a", start_barrier),
            BatchSource("source_b", start_barrier),
        ]
        root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "parallel.db"
            report_path = Path(tmp_dir) / "parallel.md"
            args = Namespace(
                profile="sample_data/active_profile.json",
                db=str(db_path),
                export=str(report_path),
                enabled_sources=["source_a", "source_b"],
                listing_url="",
                keyword="automation",
                search_keywords=["automation"],
                limit=1,
                max_pages=1,
                openai_model="gpt-4.1-mini",
                analysis_strategy="rule",
                refresh=False,
            )

            with patch(
                "src.cvbankas_tracker.main.resolve_sources",
                return_value=sources,
            ):
                with patch("src.cvbankas_tracker.main.Path.cwd", return_value=root):
                    stdout = StringIO()
                    with redirect_stdout(stdout):
                        exit_code = run_batch(args, {})
                    output = stdout.getvalue()

            database = DatabaseManager(db_path)
            database.initialize()
            try:
                saved = database.list_vacancies()
                analyses = database.list_analyses()
                tracked = database.list_tracked_applications()
                with database.connection() as connection:
                    event_count = connection.execute(
                        "SELECT COUNT(*) FROM application_status_events"
                    ).fetchone()[0]
                    run = connection.execute(
                        "SELECT status, source_summary_json FROM collection_runs"
                    ).fetchone()
            finally:
                database.close()
            report = report_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("database is locked", output.lower())
        self.assertNotIn("database table is locked", output.lower())
        self.assertIn("failed=0", output)
        self.assertEqual({vacancy.source_name for vacancy in saved}, {"source_a", "source_b"})
        self.assertEqual(len(saved), 2)
        self.assertEqual(len(analyses), 2)
        self.assertEqual(len(tracked), 2)
        self.assertEqual(event_count, 2)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(report.count("## Automation Engineer"), 2)
        self.assertTrue(all(source.closed for source in sources))


if __name__ == "__main__":
    unittest.main()
