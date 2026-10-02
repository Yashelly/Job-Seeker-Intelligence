import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.io_utils import ProfileFileReader, ReportFileWriter
from cvbankas_tracker.models import (
    AnalysisMethod,
    ApplicationRecord,
    ApplicationStatus,
    ApplicationStatusEventKind,
    ApplicationStatusOrigin,
    FitLabel,
    InboxPreferences,
    Vacancy,
    VacancyAnalysis,
)
from cvbankas_tracker.storage import (
    DatabaseManager,
    DatabaseMigrationError,
    _add_utc_hours,
    _merged_application_for_urls,
    _merged_lifecycle_for_rows,
    bootstrap_database,
    canonicalize_source_url,
    project_entrypoint_dir,
    resolve_database_path,
)


class StorageAndIOTests(unittest.TestCase):
    def test_profile_reader_loads_json_profile(self) -> None:
        profile_path = Path(__file__).resolve().parents[1] / "sample_data" / "active_profile.json"
        profile = ProfileFileReader().read(profile_path)
        self.assertIn("python", profile.skills)
        self.assertEqual(profile.experience_level, "Senior")
        self.assertEqual(profile.years_of_experience, 6)
        self.assertIn("python", profile.must_have_skills)
        self.assertIn("docker", profile.nice_to_have_skills)
        self.assertIn("warehouse", profile.excluded_keywords)

    def test_database_round_trip_and_report_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            report_path = Path(tmp_dir) / "report.md"
            database = DatabaseManager(db_path)
            database.initialize()

            vacancy = Vacancy(
                source_name="cvbankas",
                source_id="1-123",
                source_url="https://www.cvbankas.lt/python-role/1-123",
                title="Python Developer",
                company="Test Co",
                location="Vilnius",
                salary_text="2500 EUR",
                requirements=["Python"],
                responsibilities=["Build APIs"],
                raw_text="<html><body>raw vacancy</body></html>",
            )
            analysis = VacancyAnalysis(
                vacancy_source_url=vacancy.source_url,
                analysis_method=AnalysisMethod.RULE_BASED,
                score=70,
                fit_label=FitLabel.MEDIUM,
                explanation="Reasonable fit.",
                matched_points=("Python",),
                missing_points=("Remote not listed",),
                notes="Stored during unit test.",
            )
            application = ApplicationRecord(
                vacancy_source_url=vacancy.source_url,
                status=ApplicationStatus.APPLIED,
                notes="Test note",
            )

            database.save_vacancy(vacancy)
            analysis_id = database.save_analysis(analysis)
            application.analysis_id = analysis_id
            database.save_application_record(application)

            stored_vacancy = database.get_vacancy(vacancy.source_url)
            stored_application = database.get_application_record(vacancy.source_url)
            saved_items = database.list_vacancies_with_latest_scores()
            latest_analysis = database.get_latest_analysis(vacancy.source_url)
            by_source_id = database.get_vacancy_by_source_id(vacancy.source_id)
            self.assertIsNotNone(stored_vacancy)
            self.assertTrue(database.has_vacancy(vacancy.source_url))
            self.assertEqual(stored_vacancy.raw_text, vacancy.raw_text)
            self.assertEqual(stored_vacancy.source_name, "cvbankas")
            self.assertEqual(stored_application.status, ApplicationStatus.APPLIED)
            self.assertEqual(saved_items[0].latest_score, 70)
            self.assertEqual(saved_items[0].source_name, "cvbankas")
            self.assertEqual(latest_analysis.score, 70)
            self.assertEqual(by_source_id.source_url, vacancy.source_url)

            ReportFileWriter().write_report(report_path, [(vacancy, analysis, application)])
            self.assertTrue(report_path.exists())
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Python Developer", report_text)
            self.assertIn("## AI Re-analysis Prompt", report_text)
            self.assertNotIn("## Candidate Profile", report_text)
            self.assertIn("Every ranked/recommended/rejected vacancy must include", report_text)
            self.assertIn(f"- Vacancy URL: [{vacancy.source_url}]({vacancy.source_url})", report_text)
            self.assertIn("### Extracted Vacancy Data", report_text)
            self.assertIn("  - Python", report_text)
            self.assertIn("  - Build APIs", report_text)

            database.close()

    def test_database_initialization_migrates_old_vacancy_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "legacy.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE vacancies (
                    source_url TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    salary_text TEXT NOT NULL,
                    requirements_json TEXT NOT NULL,
                    responsibilities_json TEXT NOT NULL
                );
                """
            )
            connection.close()

            database = DatabaseManager(db_path)
            result = database.initialize()
            with database.connection() as connection:
                columns = connection.execute("PRAGMA table_info(vacancies)").fetchall()
            database.close()
            backup_exists = result.backup_path is not None and result.backup_path.exists()

        column_names = {column["name"] for column in columns}
        self.assertIn("source_name", column_names)
        self.assertIn("raw_text", column_names)
        self.assertIsNotNone(result.backup_path)
        self.assertTrue(backup_exists)

    def test_database_initialize_is_idempotent_and_uses_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "wal.db"
            database = DatabaseManager(db_path)

            first = database.initialize()
            second = database.initialize()

            self.assertEqual(first.journal_mode, "wal")
            self.assertEqual(second.journal_mode, "wal")
            self.assertFalse(second.migrated)
            with database.connection() as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 30000)

    def test_operational_connections_are_distinct_and_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "connections.db")
            database.initialize()

            with database.connection() as first, database.connection() as second:
                self.assertIsNot(first, second)
                self.assertEqual(first.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(second.execute("PRAGMA foreign_keys").fetchone()[0], 1)

            with self.assertRaises(sqlite3.ProgrammingError):
                first.execute("SELECT 1")
            with self.assertRaises(sqlite3.ProgrammingError):
                second.execute("SELECT 1")

    def test_foreign_keys_are_enforced_on_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "fk.db")
            database.initialize()
            analysis = VacancyAnalysis(
                vacancy_source_url="https://example.test/missing",
                analysis_method=AnalysisMethod.RULE_BASED,
                score=50,
                fit_label=FitLabel.LOW,
                explanation="Missing parent vacancy.",
                matched_points=(),
                missing_points=(),
            )

            with self.assertRaises(sqlite3.IntegrityError):
                database.save_analysis(analysis)

    def test_bootstrap_audits_orphans_before_schema_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "orphan.db"
            connection = sqlite3.connect(db_path)
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                CREATE TABLE vacancies (
                    source_url TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    salary_text TEXT NOT NULL,
                    requirements_json TEXT NOT NULL,
                    responsibilities_json TEXT NOT NULL
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
                INSERT INTO analyses (
                    vacancy_source_url, analysis_method, score, fit_label, explanation,
                    matched_points_json, missing_points_json, notes
                ) VALUES (
                    'https://example.test/orphan', 'rule_based', 1, 'Low', 'orphan',
                    '[]', '[]', ''
                );
                """
            )
            connection.commit()
            connection.close()

            with self.assertRaises(DatabaseMigrationError):
                bootstrap_database(db_path, create_backup=True)

            connection = sqlite3.connect(db_path)
            try:
                columns = connection.execute("PRAGMA table_info(vacancies)").fetchall()
            finally:
                connection.close()

            backups = list(Path(tmp_dir).glob("orphan.db.*.bak"))
            self.assertEqual(backups, [])
            column_names = {column[1] for column in columns}
            self.assertNotIn("source_name", column_names)
            self.assertNotIn("raw_text", column_names)

    def test_concurrent_bootstrap_serializes_legacy_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "concurrent_legacy.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE vacancies (
                    source_url TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    salary_text TEXT NOT NULL,
                    requirements_json TEXT NOT NULL,
                    responsibilities_json TEXT NOT NULL
                );
                """
            )
            connection.close()

            root = Path(__file__).resolve().parents[1]
            code = (
                "import sys; "
                f"sys.path.insert(0, {str(root / 'src')!r}); "
                "from cvbankas_tracker.storage import bootstrap_database; "
                f"result = bootstrap_database({str(db_path)!r}); "
                "print(result.journal_mode, result.backup_path)"
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            completed = [process.communicate(timeout=30) for process in processes]

            for process, (stdout, stderr) in zip(processes, completed, strict=True):
                self.assertEqual(
                    process.returncode,
                    0,
                    msg=f"stdout={stdout}\nstderr={stderr}",
                )
            backups = list(Path(tmp_dir).glob("concurrent_legacy.db.*.bak"))
            self.assertEqual(len(backups), 1)
            connection = sqlite3.connect(db_path)
            try:
                columns = connection.execute("PRAGMA table_info(vacancies)").fetchall()
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                connection.close()

        column_names = {column[1] for column in columns}
        self.assertIn("source_name", column_names)
        self.assertIn("raw_text", column_names)
        self.assertEqual(journal_mode, "wal")

    def test_resolve_database_path_is_stable_for_config_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_dir = Path(tmp_dir) / "config"
            config_dir.mkdir()
            config_path = config_dir / "settings.yaml"
            config_path.write_text("db: data/jobs.db\n", encoding="utf-8")

            original_cwd = Path.cwd()
            try:
                first_cwd = Path(tmp_dir)
                second_cwd = config_dir
                import os

                os.chdir(first_cwd)
                first = resolve_database_path("data/jobs.db", config_path=config_path)
                os.chdir(second_cwd)
                second = resolve_database_path("data/jobs.db", config_path=config_path)
            finally:
                os.chdir(original_cwd)

        self.assertEqual(first, second)
        self.assertEqual(first, (config_dir / "data" / "jobs.db").resolve())

    def test_file_backed_wal_allows_independent_reader_and_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "concurrent.db")
            database.initialize()
            vacancy = Vacancy(
                source_name="sample",
                source_id="wal-1",
                source_url="https://example.test/wal-1",
                title="WAL Developer",
                company="Test Co",
                location="Remote",
                salary_text="",
                requirements=[],
                responsibilities=[],
            )

            with database.connection() as reader:
                reader.execute("BEGIN")
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0], 0)
                database.save_vacancy(vacancy)
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0], 0)
                reader.rollback()

            self.assertTrue(database.has_vacancy(vacancy.source_url))


def _vac(url: str, source_id: str) -> Vacancy:
    return Vacancy(
        source_name="sample",
        source_id=source_id,
        source_url=url,
        title="Role",
        company="Co",
        location="Remote",
        salary_text="",
        requirements=[],
        responsibilities=[],
    )


def _analysis(url: str, score: int) -> VacancyAnalysis:
    return VacancyAnalysis(
        vacancy_source_url=url,
        analysis_method=AnalysisMethod.RULE_BASED,
        score=score,
        fit_label=FitLabel.LOW if score < 40 else FitLabel.MEDIUM,
        explanation="x",
        matched_points=(),
        missing_points=(),
        notes="",
    )


class AutoSaveAndPruneTests(unittest.TestCase):
    def test_exact_reprocessed_analysis_is_idempotent_but_changed_analysis_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "idempotent.db")
            db.initialize()
            url = "https://example.test/idempotent"
            first_id, _ = db.save_processed_vacancy(
                vacancy=_vac(url, "1"), analysis=_analysis(url, 50)
            )
            duplicate_id, _ = db.save_processed_vacancy(
                vacancy=_vac(url, "1"), analysis=_analysis(url, 50)
            )
            changed_id, _ = db.save_processed_vacancy(
                vacancy=_vac(url, "1"), analysis=_analysis(url, 55)
            )
            with db.connection() as connection:
                analysis_count = connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
            self.assertEqual(first_id, duplicate_id)
            self.assertNotEqual(first_id, changed_id)
            self.assertEqual(analysis_count, 2)
            db.close()


class StorageValidationCoverageTests(unittest.TestCase):
    def test_path_and_url_boundary_normalization(self) -> None:
        root = project_entrypoint_dir()
        self.assertTrue((root / "main.py").is_file())
        self.assertEqual(
            resolve_database_path("jobs.db", config_path="config/settings.yaml"),
            (root / "config" / "jobs.db").resolve(),
        )
        self.assertEqual(resolve_database_path("jobs.db"), (root / "jobs.db").resolve())
        self.assertEqual(
            resolve_database_path("jobs.db", entrypoint_dir="runtime"),
            (root / "runtime" / "jobs.db").resolve(),
        )
        self.assertEqual(canonicalize_source_url("relative/path"), "relative/path")
        self.assertEqual(
            canonicalize_source_url("https://EXAMPLE.test:8443/job/?utm_source=x"),
            "https://example.test:8443/job",
        )

    def test_recovery_reaps_a_run_with_an_unparseable_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "recovery.db")
            db.initialize()
            run = db.begin_collection_run()
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE collection_runs SET started_at = ? WHERE id = ?",
                    ("not-an-instant", run.id),
                )

            self.assertEqual(db.recover_stranded_collection_runs(older_than_seconds=60), 1)
            self.assertEqual(db.get_collection_run(run.id).status, "failed")
            db.close()

    def test_action_item_validation_and_open_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "actions.db")
            db.initialize()
            url = "https://example.test/action"
            db.save_vacancy(_vac(url, "action"))

            with self.assertRaisesRegex(ValueError, "title is required"):
                db.create_action_item(vacancy_source_url=url, title="  ")
            with self.assertRaisesRegex(ValueError, "Vacancy not found"):
                db.create_action_item(
                    vacancy_source_url="https://example.test/missing",
                    title="Follow up",
                )
            with self.assertRaisesRegex(ValueError, "title is required"):
                db.update_action_item(999, title="  ")
            with self.assertRaisesRegex(ValueError, "Action not found"):
                db.update_action_item(999, title="Follow up")
            with self.assertRaisesRegex(ValueError, "Action not found"):
                db.complete_action_item(999)
            with self.assertRaisesRegex(ValueError, "Action not found"):
                db.reopen_action_item(999)

            action = db.create_action_item(vacancy_source_url=url, title="Follow up")
            db.complete_action_item(action.id)
            self.assertEqual(db.list_action_items(include_completed=False), [])
            db.close()

    def test_inbox_preference_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "inbox-validation.db")
            db.initialize()
            with self.assertRaisesRegex(ValueError, "sort_by"):
                db.save_inbox_preferences(InboxPreferences(sort_by="invalid"))
            with self.assertRaisesRegex(ValueError, "fit_label"):
                db.save_inbox_preferences(InboxPreferences(fit_label="Excellent"))
            with self.assertRaisesRegex(ValueError, "application_status"):
                db.save_inbox_preferences(InboxPreferences(application_status="Unknown"))
            db.close()

    def test_migration_merge_helpers_preserve_fallback_run_ids(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT '2026-01-01T00:00:00Z' AS first_seen_at,
                   NULL AS first_seen_run_id,
                   '2026-03-01T00:00:00Z' AS last_seen_at,
                   NULL AS last_seen_run_id
            UNION ALL
            SELECT '2026-02-01T00:00:00Z', 7, '2026-02-01T00:00:00Z', 9
            """
        ).fetchall()

        merged = _merged_lifecycle_for_rows(rows)

        self.assertEqual(merged["first_seen_run_id"], 7)
        self.assertEqual(merged["last_seen_run_id"], 9)
        connection.close()

    def test_empty_application_merge_and_naive_reminder_time(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE applications (
                vacancy_source_url TEXT PRIMARY KEY,
                analysis_id INTEGER,
                status TEXT NOT NULL,
                notes TEXT NOT NULL
            )
            """
        )
        self.assertIsNone(
            _merged_application_for_urls(
                connection,
                ["https://example.test/missing"],
                "https://example.test/missing",
            )
        )
        self.assertEqual(
            _add_utc_hours("2026-01-01T10:00:00", 2),
            "2026-01-01T12:00:00Z",
        )
        connection.close()

    def test_noop_and_not_found_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "boundaries.db")
            db.initialize()
            with self.assertRaisesRegex(ValueError, "Invalid terminal"):
                db.finish_collection_run(999, status="running")  # type: ignore[arg-type]
            db.mark_telegram_summaries_delivered([])
            self.assertFalse(db.record_vacancy_observation("https://example.test/missing", collection_run_id=None))
            self.assertIsNone(db.get_latest_analysis("https://example.test/missing"))
            self.assertIsNone(db.get_vacancy_by_source_id("missing"))
            db.close()

    def test_source_id_lookup_requires_source_when_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "source-id.db")
            db.initialize()
            first = _vac("https://example.test/one", "shared")
            first.source_name = "one"
            second = _vac("https://example.test/two", "shared")
            second.source_name = "two"
            db.save_vacancy(first)
            db.save_vacancy(second)
            self.assertEqual(db.get_vacancy_by_source_id("shared", "one"), first)
            self.assertIsNone(db.get_vacancy_by_source_id("shared", "missing"))
            with self.assertRaisesRegex(ValueError, "exists in multiple sources"):
                db.get_vacancy_by_source_id("shared")
            db.close()

    def test_application_status_validation_and_idempotent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "status.db")
            db.initialize()
            url = "https://example.test/status"
            db.save_processed_vacancy(vacancy=_vac(url, "status"), analysis=_analysis(url, 50))
            with self.assertRaisesRegex(ValueError, "Invalid application status origin"):
                db.update_application_status_with_event(
                    url, ApplicationStatus.SAVED, origin="invalid"  # type: ignore[arg-type]
                )
            with self.assertRaisesRegex(ValueError, "requires a reason"):
                db.update_application_status_with_event(
                    url,
                    ApplicationStatus.SAVED,
                    origin=ApplicationStatusOrigin.SYSTEM,
                    kind=ApplicationStatusEventKind.CORRECTIVE,
                )
            with self.assertRaisesRegex(ValueError, "only accepted for corrective"):
                db.update_application_status_with_event(
                    url,
                    ApplicationStatus.SAVED,
                    origin=ApplicationStatusOrigin.SYSTEM,
                    reason="not corrective",
                )
            with self.assertRaisesRegex(ValueError, "No application record"):
                db.update_application_status_with_event(
                    "https://example.test/missing",
                    ApplicationStatus.SAVED,
                    origin=ApplicationStatusOrigin.SYSTEM,
                )
            unchanged = db.update_application_status_with_event(
                url, ApplicationStatus.SAVED, origin=ApplicationStatusOrigin.SYSTEM
            )
            self.assertEqual(unchanged.status, ApplicationStatus.SAVED)
            db.close()

    def test_tracked_applications_are_sorted_by_match_descending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "sorted.db")
            db.initialize()
            low = "https://example.test/low"
            high = "https://example.test/high"
            db.save_processed_vacancy(
                vacancy=_vac(low, "1"),
                analysis=_analysis(low, 25),
                auto_save=True,
                auto_save_threshold=0,
            )
            db.save_processed_vacancy(
                vacancy=_vac(high, "2"),
                analysis=_analysis(high, 90),
                auto_save=True,
                auto_save_threshold=0,
            )

            tracked = db.list_tracked_applications()

            self.assertEqual([item.latest_score for item in tracked], [90, 25])
            self.assertTrue(all(item.analysis_method == "rule_based" for item in tracked))
            self.assertTrue(all(item.saved_at_utc for item in tracked))
            db.close()

    def test_auto_save_disabled_creates_no_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "a.db")
            db.initialize()
            url = "https://example.test/low"
            _id, app = db.save_processed_vacancy(
                vacancy=_vac(url, "1"),
                analysis=_analysis(url, 20),
                auto_save=False,
            )
            self.assertIsNone(app)
            self.assertIsNone(db.get_application_record(url))
            # The vacancy and its analysis are still stored.
            self.assertTrue(db.has_vacancy(url))
            db.close()

    def test_auto_save_threshold_only_saves_high_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "b.db")
            db.initialize()
            low = "https://example.test/low"
            high = "https://example.test/high"
            _i, low_app = db.save_processed_vacancy(
                vacancy=_vac(low, "1"), analysis=_analysis(low, 30),
                auto_save=True, auto_save_threshold=40,
            )
            _j, high_app = db.save_processed_vacancy(
                vacancy=_vac(high, "2"), analysis=_analysis(high, 55),
                auto_save=True, auto_save_threshold=40,
            )
            self.assertIsNone(low_app)
            self.assertIsNotNone(high_app)
            self.assertEqual(high_app.status, ApplicationStatus.SAVED)
            db.close()

    def test_prune_removes_unsaved_below_threshold_but_keeps_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "c.db")
            db.initialize()
            low_unsaved = "https://example.test/low-unsaved"
            low_saved = "https://example.test/low-saved"
            high = "https://example.test/high"
            db.save_processed_vacancy(
                vacancy=_vac(low_unsaved, "1"), analysis=_analysis(low_unsaved, 12),
                auto_save=False,
            )
            db.save_processed_vacancy(
                vacancy=_vac(low_saved, "2"), analysis=_analysis(low_saved, 15),
                auto_save=True, auto_save_threshold=0,  # force a Saved record
            )
            db.save_processed_vacancy(
                vacancy=_vac(high, "3"), analysis=_analysis(high, 80),
                auto_save=False,
            )

            removed = db.prune_low_score_unsaved(40)

            self.assertEqual(removed, 1)
            self.assertFalse(db.has_vacancy(low_unsaved))   # unsaved + low -> gone
            self.assertTrue(db.has_vacancy(low_saved))       # saved -> kept despite low score
            self.assertTrue(db.has_vacancy(high))            # high score -> kept
            db.close()


if __name__ == "__main__":
    unittest.main()
