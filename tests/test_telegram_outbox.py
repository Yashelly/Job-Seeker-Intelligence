from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.cvbankas_tracker.main import SourceBatchResult, run_batch
from src.cvbankas_tracker.models import AnalysisMethod, FitLabel, Vacancy, VacancyAnalysis
from src.cvbankas_tracker.storage import DatabaseManager
from src.cvbankas_tracker.telegram import TelegramNotificationError

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class StubSource:
    name: str


def _vacancy(url: str = "https://example.test/recovered") -> Vacancy:
    return Vacancy(
        source_name="test_source",
        source_id="recovered-1",
        source_url=url,
        title="Recovered AI Engineer",
        company="Example",
        location="Remote",
        salary_text="",
        requirements=["AI automation"],
        responsibilities=["Build workflows"],
        raw_text="AI automation engineer",
    )


def _analysis(url: str = "https://example.test/recovered") -> VacancyAnalysis:
    return VacancyAnalysis(
        vacancy_source_url=url,
        analysis_method=AnalysisMethod.RULE_BASED,
        score=72,
        fit_label=FitLabel.HIGH,
        explanation="Strong automation match.",
        matched_points=("automation",),
        missing_points=(),
    )


def _daily_args(db_path: Path, report_path: Path) -> Namespace:
    return Namespace(
        profile="sample_data/active_profile.json",
        db=str(db_path),
        export=str(report_path),
        enabled_sources=["test_source"],
        listing_url="",
        daily_run=True,
        infinite=False,
        prune_threshold=40,
    )


class TelegramSummaryOutboxStorageTests(unittest.TestCase):
    def test_initialize_adds_outbox_to_an_existing_database_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = DatabaseManager(Path(tmp) / "jobs.db")
            database.initialize()
            database.save_vacancy(_vacancy())
            with database.transaction() as connection:
                connection.execute("DROP TABLE telegram_summary_outbox")

            migration = database.initialize()

            self.assertTrue(migration.migrated)
            self.assertIsNotNone(migration.backup_path)
            self.assertIsNotNone(database.get_vacancy(_vacancy().source_url))
            with database.connection() as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ? AND name = ?",
                    ("table", "telegram_summary_outbox"),
                ).fetchone()
            self.assertIsNotNone(table)

    def test_pending_rows_remain_until_delivery_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = DatabaseManager(Path(tmp) / "jobs.db")
            database.initialize()
            run = database.begin_collection_run(queue_telegram_summary=True)
            database.save_processed_vacancy(
                vacancy=_vacancy(),
                analysis=_analysis(),
                collection_run_id=run.id,
            )
            database.finish_collection_run(run.id, status="completed")

            run_ids, rows = database.list_pending_telegram_summary_rows()
            self.assertEqual(run_ids, [run.id])
            self.assertEqual([row[0].title for row in rows], ["Recovered AI Engineer"])

            database.mark_telegram_summaries_delivered(run_ids)
            self.assertEqual(database.list_pending_telegram_summary_rows(), ([], []))

    def test_pruning_cannot_delete_an_unsent_low_score_vacancy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = DatabaseManager(Path(tmp) / "jobs.db")
            database.initialize()
            run = database.begin_collection_run(queue_telegram_summary=True)
            low_analysis = VacancyAnalysis(
                vacancy_source_url=_vacancy().source_url,
                analysis_method=AnalysisMethod.RULE_BASED,
                score=10,
                fit_label=FitLabel.LOW,
                explanation="Low match.",
                matched_points=(),
                missing_points=("automation",),
            )
            database.save_processed_vacancy(
                vacancy=_vacancy(),
                analysis=low_analysis,
                collection_run_id=run.id,
                auto_save=False,
            )
            database.finish_collection_run(run.id, status="failed")

            self.assertEqual(database.prune_low_score_unsaved(40), 0)
            self.assertIsNotNone(database.get_vacancy(_vacancy().source_url))

            database.mark_telegram_summaries_delivered([run.id])
            self.assertEqual(database.prune_low_score_unsaved(40), 1)
            self.assertIsNone(database.get_vacancy(_vacancy().source_url))


class TelegramSummaryOutboxBatchTests(unittest.TestCase):
    def _seed_stranded_result(self, database: DatabaseManager) -> int:
        run = database.begin_collection_run(queue_telegram_summary=True)
        database.save_processed_vacancy(
            vacancy=_vacancy(),
            analysis=_analysis(),
            collection_run_id=run.id,
        )
        self.assertEqual(database.recover_stranded_collection_runs(), 1)
        return run.id

    def test_retry_reports_rows_committed_by_a_stranded_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.db"
            report_path = Path(tmp) / "report.md"
            database = DatabaseManager(db_path)
            database.initialize()
            stranded_run_id = self._seed_stranded_result(database)
            notifier = MagicMock()
            notifier.send_daily_summary.return_value = 1

            with patch(
                "src.cvbankas_tracker.main.Path.cwd", return_value=REPO_ROOT
            ), patch(
                "src.cvbankas_tracker.main.resolve_sources",
                return_value=[StubSource("test_source")],
            ), patch(
                "src.cvbankas_tracker.main._execute_source_batches",
                return_value=[SourceBatchResult("test_source", [])],
            ), patch(
                "src.cvbankas_tracker.main.TelegramNotifier.from_env",
                return_value=notifier,
            ):
                exit_code = run_batch(_daily_args(db_path, report_path), {})

            sent_rows = notifier.send_daily_summary.call_args.args[0]
            self.assertEqual(exit_code, 0)
            self.assertEqual([row[0].title for row in sent_rows], ["Recovered AI Engineer"])
            self.assertEqual(
                notifier.send_daily_summary.call_args.kwargs["attempted_count"], 1
            )
            self.assertEqual(
                notifier.send_daily_summary.call_args.kwargs["recovered_count"], 1
            )
            self.assertIn("Recovered AI Engineer", report_path.read_text(encoding="utf-8"))
            self.assertEqual(database.list_pending_telegram_summary_rows(), ([], []))
            with database.connection() as connection:
                delivered = connection.execute(
                    "SELECT delivered_at FROM telegram_summary_outbox WHERE run_id = ?",
                    (stranded_run_id,),
                ).fetchone()[0]
            self.assertIsNotNone(delivered)

    def test_failed_delivery_leaves_rows_pending_for_the_next_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.db"
            report_path = Path(tmp) / "report.md"
            database = DatabaseManager(db_path)
            database.initialize()
            self._seed_stranded_result(database)
            notifier = MagicMock()
            notifier.send_daily_summary.side_effect = TelegramNotificationError(
                "Telegram unavailable"
            )

            with patch(
                "src.cvbankas_tracker.main.Path.cwd", return_value=REPO_ROOT
            ), patch(
                "src.cvbankas_tracker.main.resolve_sources",
                return_value=[StubSource("test_source")],
            ), patch(
                "src.cvbankas_tracker.main._execute_source_batches",
                return_value=[SourceBatchResult("test_source", [])],
            ), patch(
                "src.cvbankas_tracker.main.TelegramNotifier.from_env",
                return_value=notifier,
            ):
                exit_code = run_batch(_daily_args(db_path, report_path), {})

            pending_run_ids, pending_rows = database.list_pending_telegram_summary_rows()
            self.assertEqual(exit_code, 1)
            self.assertEqual(len(pending_run_ids), 2)
            self.assertEqual([row[0].title for row in pending_rows], ["Recovered AI Engineer"])


if __name__ == "__main__":
    unittest.main()
