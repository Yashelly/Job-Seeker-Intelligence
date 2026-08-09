from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from cvbankas_tracker.models import (
    AnalysisMethod,
    ApplicationRecord,
    ApplicationStatus,
    FitLabel,
    InboxPreferences,
    Vacancy,
    VacancyAnalysis,
)
from cvbankas_tracker.main import SourceBatchResult, _collection_terminal_status
from cvbankas_tracker.storage import (
    CollectionRunAlreadyActive,
    DatabaseManager,
    canonicalize_source_url,
)


def make_vacancy(url: str, *, title: str = "Python Developer", source_name: str = "sample") -> Vacancy:
    return Vacancy(
        source_name=source_name,
        source_id=title.lower().replace(" ", "-"),
        source_url=url,
        title=title,
        company="Test Co",
        location="Remote",
        salary_text="",
        requirements=["Python"],
        responsibilities=[],
    )


def make_analysis(url: str, *, score: int, fit: FitLabel = FitLabel.MEDIUM) -> VacancyAnalysis:
    return VacancyAnalysis(
        vacancy_source_url=url,
        analysis_method=AnalysisMethod.RULE_BASED,
        score=score,
        fit_label=fit,
        explanation=f"score {score}",
        matched_points=("Python",),
        missing_points=("Docker",),
    )


class CollectionLifecycleInboxTests(unittest.TestCase):
    def test_canonical_url_identity_and_run_observation_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "g002.db")
            database.initialize()
            first_run = database.begin_collection_run()
            original = "HTTPS://Example.test:443/jobs/python/?utm_source=newsletter&b=2&a=1#frag"
            canonical = "https://example.test/jobs/python?a=1&b=2"

            vacancy = make_vacancy(original)
            database.save_vacancy(vacancy, collection_run_id=first_run.id, original_source_url=original)
            database.save_analysis(make_analysis(original, score=80, fit=FitLabel.HIGH))
            database.finish_collection_run(first_run.id, status="completed")

            second_run = database.begin_collection_run()
            self.assertTrue(
                database.record_vacancy_observation(
                    "https://example.test/jobs/python?b=2&a=1&utm_medium=email",
                    collection_run_id=second_run.id,
                    source_name="sample",
                )
            )
            database.finish_collection_run(second_run.id, status="completed")

            stored = database.get_vacancy(original)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.source_url, canonical)
            inbox_first = database.query_inbox(run_id=first_run.id, new_only=True)
            inbox_second_new = database.query_inbox(run_id=second_run.id, new_only=True)
            inbox_second_current = database.query_inbox(run_id=second_run.id, current_run_only=True)

        self.assertEqual(canonicalize_source_url(original), canonical)
        self.assertEqual(len(inbox_first), 1)
        self.assertEqual(inbox_first[0].first_seen_run_id, first_run.id)
        self.assertEqual(inbox_first[0].last_seen_run_id, second_run.id)
        self.assertEqual(inbox_second_new, [])
        self.assertEqual(len(inbox_second_current), 1)
        self.assertTrue(inbox_second_current[0].is_current_run)

    def test_collection_run_lease_rejects_concurrent_active_run_and_terminal_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "lease.db")
            database.initialize()
            active = database.begin_collection_run()
            with self.assertRaises(CollectionRunAlreadyActive):
                DatabaseManager(database.db_path).begin_collection_run()
            finished = database.finish_collection_run(
                active.id,
                status="partial",
                source_summary={"sample": {"attempted": 1, "failed": 1}},
                error_summary={"sample": 1},
            )
            next_run = database.begin_collection_run()
            failed = database.finish_collection_run(next_run.id, status="failed")

        self.assertEqual(finished.status, "partial")
        self.assertEqual(finished.error_summary, {"sample": 1})
        self.assertEqual(failed.status, "failed")

    def test_inbox_preferences_filters_threshold_sort_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "inbox.db")
            database.initialize()
            run = database.begin_collection_run()
            high = make_vacancy("https://example.test/high", title="Backend Engineer")
            low = make_vacancy("https://example.test/low", title="Analyst", source_name="other")
            database.save_vacancy(high, collection_run_id=run.id)
            database.save_vacancy(low, collection_run_id=run.id)
            high_analysis_id = database.save_analysis(make_analysis(high.source_url, score=90, fit=FitLabel.HIGH))
            database.save_analysis(make_analysis(low.source_url, score=30, fit=FitLabel.LOW))
            database.save_application_record(
                ApplicationRecord(
                    vacancy_source_url=high.source_url,
                    analysis_id=high_analysis_id,
                    status=ApplicationStatus.APPLIED,
                )
            )
            database.finish_collection_run(run.id, status="completed")
            database.save_inbox_preferences(
                InboxPreferences(minimum_score=50, hide_below_threshold=True, sort_by="title")
            )

            visible = database.query_inbox()
            with_low = database.query_inbox(include_below_threshold=True)
            applied = database.query_inbox(application_status=ApplicationStatus.APPLIED)
            low_fit = database.query_inbox(fit_label=FitLabel.LOW, include_below_threshold=True)

        self.assertEqual([item.title for item in visible], ["Backend Engineer"])
        self.assertEqual([item.title for item in with_low], ["Analyst", "Backend Engineer"])
        self.assertEqual([item.application_status for item in applied], [ApplicationStatus.APPLIED])
        self.assertEqual([item.latest_fit_label for item in low_fit], ["Low"])



    def test_legacy_migration_canonicalizes_colliding_primary_urls_without_losing_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "legacy_collision.db"
            connection = sqlite3.connect(db_path)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE vacancies (
                    source_url TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL DEFAULT 'sample',
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    salary_text TEXT NOT NULL,
                    requirements_json TEXT NOT NULL,
                    responsibilities_json TEXT NOT NULL,
                    raw_text TEXT NOT NULL DEFAULT '',
                    original_source_url TEXT,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    first_seen_run_id INTEGER,
                    last_seen_run_id INTEGER
                );
                CREATE TABLE analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vacancy_source_url TEXT NOT NULL,
                    analysis_method TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    fit_label TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    matched_points_json TEXT NOT NULL,
                    missing_points_json TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    FOREIGN KEY (vacancy_source_url) REFERENCES vacancies(source_url)
                );
                CREATE TABLE applications (
                    vacancy_source_url TEXT PRIMARY KEY,
                    analysis_id INTEGER,
                    status TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    FOREIGN KEY (vacancy_source_url) REFERENCES vacancies(source_url),
                    FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                );
                CREATE TABLE collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    db_path TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'partial', 'failed')),
                    source_summary_json TEXT NOT NULL DEFAULT '{}',
                    error_summary_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE collection_run_observations (
                    run_id INTEGER NOT NULL,
                    vacancy_source_url TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    original_source_url TEXT,
                    PRIMARY KEY (run_id, vacancy_source_url),
                    FOREIGN KEY (run_id) REFERENCES collection_runs(id),
                    FOREIGN KEY (vacancy_source_url) REFERENCES vacancies(source_url)
                );
                """
            )
            canonical = "https://example.test/job"
            variant = "https://example.test/job?utm_source=newsletter"
            connection.execute(
                "INSERT INTO collection_runs (id, db_path, started_at, finished_at, status) VALUES (1, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 'completed')",
                (str(db_path),),
            )
            fixture_rows = [
                (variant, "variant", "Variant Title", "2026-01-01T00:00:05Z", "2026-01-01T00:00:20Z", 1, 1),
                (canonical, "canonical", "Canonical Title", "2026-01-01T00:00:10Z", "2026-01-01T00:00:30Z", 1, 1),
            ]
            for url, source_id, title, first_seen_at, last_seen_at, first_seen_run_id, last_seen_run_id in fixture_rows:
                connection.execute(
                    """
                    INSERT INTO vacancies (
                        source_url, source_name, source_id, title, company, location, salary_text,
                        requirements_json, responsibilities_json, raw_text,
                        first_seen_at, last_seen_at, first_seen_run_id, last_seen_run_id
                    ) VALUES (?, 'sample', ?, ?, 'Co', 'Remote', '', '[]', '[]', ?, ?, ?, ?, ?)
                    """,
                    (url, source_id, title, f"raw {source_id}", first_seen_at, last_seen_at, first_seen_run_id, last_seen_run_id),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO analyses (
                        vacancy_source_url, analysis_method, score, fit_label, explanation,
                        matched_points_json, missing_points_json, notes
                    ) VALUES (?, 'rule_based', 50, 'Medium', 'ok', '[]', '[]', '')
                    """,
                    (url,),
                )
                connection.execute(
                    "INSERT INTO applications (vacancy_source_url, analysis_id, status, notes) VALUES (?, ?, ?, ?)",
                    (url, int(cursor.lastrowid), "Interview" if url == variant else "Saved", f"note {source_id}"),
                )
                connection.execute(
                    "INSERT INTO collection_run_observations (run_id, vacancy_source_url, observed_at, source_name, original_source_url) VALUES (1, ?, ?, 'sample', ?)",
                    (url, last_seen_at, url),
                )
            connection.commit()
            connection.close()

            database = DatabaseManager(db_path)
            database.initialize()
            with database.connection() as migrated:
                vacancy_rows = migrated.execute("SELECT source_url, title, first_seen_at, last_seen_at, first_seen_run_id, last_seen_run_id FROM vacancies").fetchall()
                analysis_rows = migrated.execute("SELECT vacancy_source_url FROM analyses ORDER BY id").fetchall()
                app_rows = migrated.execute("SELECT vacancy_source_url, status, notes FROM applications").fetchall()
                alias_rows = migrated.execute(
                    "SELECT original_source_url, canonical_source_url, legacy_vacancy_json FROM vacancy_url_aliases ORDER BY original_source_url"
                ).fetchall()
                observation_rows = migrated.execute(
                    "SELECT run_id, vacancy_source_url, observed_at, original_source_url FROM collection_run_observations"
                ).fetchall()

        self.assertEqual([(row["source_url"], row["title"]) for row in vacancy_rows], [(canonical, "Canonical Title")])
        self.assertEqual(vacancy_rows[0]["first_seen_at"], "2026-01-01T00:00:05Z")
        self.assertEqual(vacancy_rows[0]["last_seen_at"], "2026-01-01T00:00:30Z")
        self.assertEqual(vacancy_rows[0]["first_seen_run_id"], 1)
        self.assertEqual(vacancy_rows[0]["last_seen_run_id"], 1)
        self.assertEqual([row["vacancy_source_url"] for row in analysis_rows], [canonical, canonical])
        self.assertEqual(len(app_rows), 1)
        self.assertEqual(app_rows[0]["vacancy_source_url"], canonical)
        self.assertEqual(app_rows[0]["status"], "Interview")
        self.assertIn("note variant", app_rows[0]["notes"])
        self.assertIn("note canonical", app_rows[0]["notes"])
        self.assertEqual(len(alias_rows), 2)
        self.assertTrue(all(row["canonical_source_url"] == canonical for row in alias_rows))
        self.assertTrue(all(row["legacy_vacancy_json"] for row in alias_rows))
        self.assertEqual(len(observation_rows), 1)
        self.assertEqual(observation_rows[0]["vacancy_source_url"], canonical)
        self.assertEqual(observation_rows[0]["observed_at"], "2026-01-01T00:00:30Z")
        self.assertIn(variant, observation_rows[0]["original_source_url"])
        self.assertIn(canonical, observation_rows[0]["original_source_url"])

    def test_duplicate_only_success_with_source_failure_is_partial(self) -> None:
        results = [
            SourceBatchResult(source_name="duplicate_source", report_rows=[], attempted_count=2, observed_count=2),
            SourceBatchResult(source_name="broken", report_rows=[], failed_count=1),
        ]
        self.assertEqual(_collection_terminal_status(results, []), "partial")

    def test_default_inbox_is_scoped_to_latest_completed_or_partial_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "latest_inbox.db")
            database.initialize()
            first_run = database.begin_collection_run()
            stale = make_vacancy("https://example.test/stale", title="Stale Role")
            database.save_vacancy(stale, collection_run_id=first_run.id)
            database.save_analysis(make_analysis(stale.source_url, score=95, fit=FitLabel.HIGH))
            database.finish_collection_run(first_run.id, status="completed")

            second_run = database.begin_collection_run()
            current = make_vacancy("https://example.test/current", title="Current Role")
            database.save_vacancy(current, collection_run_id=second_run.id)
            database.save_analysis(make_analysis(current.source_url, score=40, fit=FitLabel.LOW))
            database.finish_collection_run(second_run.id, status="partial")

            default_inbox = database.query_inbox()
            first_run_inbox = database.query_inbox(run_id=first_run.id)

        self.assertEqual([item.title for item in default_inbox], ["Current Role"])
        self.assertEqual([item.title for item in first_run_inbox], ["Stale Role"])

    def test_collection_does_not_auto_set_applied_status(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "no_auto_applied.db"
            report_path = Path(tmp_dir) / "report.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--source",
                    "sample",
                    "--limit",
                    "1",
                    "--db",
                    str(db_path),
                    "--export",
                    str(report_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            database = DatabaseManager(db_path)
            database.initialize()
            tracked = database.list_tracked_applications()

        self.assertIn("Status     : Saved", completed.stdout)
        self.assertEqual([item.status for item in tracked], [ApplicationStatus.SAVED])


if __name__ == "__main__":
    unittest.main()
