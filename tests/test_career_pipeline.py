from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cvbankas_tracker.analysis import VacancyAnalysisService
from cvbankas_tracker.companies import CompanyRegistry
from cvbankas_tracker.main import _process_vacancy_url, run_batch
from cvbankas_tracker.sources.career_budget import CareerBudgetExceeded
from cvbankas_tracker.sources.careers import CareerPagesSource
from cvbankas_tracker.storage import DatabaseManager
from cvbankas_tracker.web import _runner_namespace

ROOT = Path(__file__).resolve().parents[1]


class CareerPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = DatabaseManager(Path(self.directory.name) / "pipeline.db")
        self.database.initialize(create_backup=False)
        self.registry = CompanyRegistry(self.database)
        self.registry.save({
            "name": "Example", "career_url": "https://jobs.ashbyhq.com/example", "collection_enabled": True,
        })
        self.source = CareerPagesSource(company_registry=self.registry)
        self.jobs = [{
            "id": str(index), "title": "Python Automation Engineer",
            "jobUrl": f"https://jobs.ashbyhq.com/example/{index}",
            "location": "Europe", "isRemote": True,
            "descriptionPlain": "Build Python automation and FastAPI integrations.",
        } for index in range(3)]

    def run_pipeline(self, *, limit: int = 100, daily: bool = False) -> int:
        args = _runner_namespace(
            profile_path=str(ROOT / "sample_data" / "active_profile.json"),
            db_path=str(self.database.db_path), enabled_sources=["careers"], keywords=["automation"],
            limit=limit, max_pages=3, analysis_strategy="rule", refresh=False,
            daily_run=daily, auto_save=False,
        )
        args.export = str(Path(self.directory.name) / "report.md")
        with (
            patch("cvbankas_tracker.main.resolve_sources", return_value=[self.source]),
            patch("cvbankas_tracker.main.load_dotenv_if_present"),
            patch("cvbankas_tracker.main._send_telegram_batch_summary", return_value=True),
            patch.object(self.source, "_fetch_json", return_value={"jobs": self.jobs}),
            redirect_stdout(io.StringIO()),
        ):
            return run_batch(args, {})

    def test_full_pipeline_saves_analyzed_jobs_and_repeated_run_deduplicates(self) -> None:
        self.assertEqual(self.run_pipeline(), 0)
        self.assertEqual(self.database.list_collection_runs(limit=1)[0].status, "completed")
        for index in range(3):
            url = f"https://jobs.ashbyhq.com/example/{index}"
            self.assertEqual(self.database.get_vacancy(url).source_name, "careers")
            self.assertIsNotNone(self.database.get_latest_analysis(url))
        self.run_pipeline()
        with self.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0], 3)

    def test_limit_marks_run_partial_instead_of_hiding_omitted_jobs(self) -> None:
        self.assertEqual(self.run_pipeline(limit=1), 2)
        run = self.database.list_collection_runs(limit=1)[0]
        self.assertEqual(run.status, "partial")
        self.assertEqual(run.source_summary["careers"]["observed"], 1)
        self.assertEqual(self.registry.get("example")["last_scan_jobs_count"], 3)

    def test_daily_scan_finds_new_jobs_after_known_job_in_unordered_feed(self) -> None:
        self.run_pipeline(limit=1)
        self.run_pipeline(limit=1, daily=True)
        with self.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0], 3)
        run = self.database.list_collection_runs(limit=1)[0]
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.source_summary["careers"]["observed"], 2)

    def test_processing_deadline_stops_remaining_jobs_and_marks_run_partial(self) -> None:
        with patch.object(self.source, "before_vacancy_processing", side_effect=[None, CareerBudgetExceeded("Time budget exceeded")]) as check:
            self.assertEqual(self.run_pipeline(daily=True), 2)
        self.assertEqual(check.call_count, 2)
        run = self.database.list_collection_runs(limit=1)[0]
        self.assertEqual(run.status, "partial")
        self.assertEqual(run.source_summary["careers"]["observed"], 1)
        self.assertEqual(self.registry.get("example")["last_scan_jobs_count"], 3)

    def test_successful_empty_run_has_success_exit_code(self) -> None:
        self.jobs = []
        self.assertEqual(self.run_pipeline(daily=True), 0)
        self.assertEqual(self.database.list_collection_runs(limit=1)[0].status, "completed")

    def test_failed_run_has_failure_exit_code(self) -> None:
        with patch.object(self.source, "_collect_company_jobs", side_effect=OSError("Unavailable")):
            self.assertEqual(self.run_pipeline(daily=True), 1)
        self.assertEqual(self.database.list_collection_runs(limit=1)[0].status, "failed")

    def test_company_disabled_during_analysis_rolls_back_all_processed_writes(self) -> None:
        self.jobs = self.jobs[:1]
        analyze = VacancyAnalysisService.analyze
        def disable_after_analysis(service, vacancy, profile):
            result = analyze(service, vacancy, profile)
            self.registry.save({"collection_enabled": False}, company_id="example")
            return result
        with patch.object(VacancyAnalysisService, "analyze", disable_after_analysis):
            self.assertEqual(self.run_pipeline(daily=True), 1)
        with self.database.connection() as connection:
            for table in ("vacancies", "analyses", "applications", "application_status_events", "collection_run_observations", "vacancy_url_aliases"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_company_disabled_before_known_observation_keeps_last_seen_unchanged(self) -> None:
        self.run_pipeline(limit=1)
        with patch.object(self.source, "_fetch_json", return_value={"jobs": self.jobs}):
            urls, _ = self.source.collect_vacancy_urls()
        url = urls[0]
        run = self.database.begin_collection_run()
        with self.database.connection() as connection:
            original_seen = tuple(connection.execute("SELECT last_seen_at,last_seen_run_id FROM vacancies WHERE source_url=?", (url,)).fetchone())
        record = self.database.record_vacancy_observation
        def disable_before_transaction(*args, **kwargs):
            self.registry.save({"collection_enabled": False}, company_id="example")
            return record(*args, **kwargs)
        with patch.object(self.database, "record_vacancy_observation", side_effect=disable_before_transaction), self.assertRaisesRegex(ValueError, "stale vacancy data"):
            _process_vacancy_url(source=self.source, url=url, args=SimpleNamespace(refresh=False), profile=Mock(), database=self.database,
                                 extraction_service=Mock(), analysis_service=Mock(), report_rows=[], title="Known", collection_run_id=run.id)
        with self.database.connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT last_seen_at,last_seen_run_id FROM vacancies WHERE source_url=?", (url,)).fetchone()), original_seen)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM collection_run_observations WHERE run_id=?", (run.id,)).fetchone()[0], 0)
        self.database.finish_collection_run(run.id, status="failed")


if __name__ == "__main__":
    unittest.main()
