from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from cvbankas_tracker.models import (
    AnalysisMethod,
    ApplicationStatus,
    FitLabel,
    InboxPreferences,
    Vacancy,
    VacancyAnalysis,
)
from cvbankas_tracker.main import run_database_command
from cvbankas_tracker.storage import DatabaseManager
from cvbankas_tracker.tui import JobSeekerTui, run_tui
from cvbankas_tracker.tracking import ActionService, ApplicationTracker
from rich.console import Console


def make_args(db_path: Path, **overrides) -> Namespace:
    values = {
        "db": str(db_path),
        "list_vacancies": False,
        "inbox": False,
        "inbox_min_score": None,
        "inbox_hide_below_threshold": False,
        "inbox_show_below_threshold": False,
        "inbox_sort": None,
        "inbox_source": "",
        "inbox_fit": None,
        "inbox_status": None,
        "inbox_new_only": None,
        "inbox_current_run_only": None,
        "inbox_all_runs": None,
        "clear_inbox_filters": None,
        "save_inbox_preferences": False,
        "list_tracked": False,
        "today": False,
        "list_actions": False,
        "action_id": None,
        "action_title": "",
        "action_notes": "",
        "action_due": "",
        "action_fold": None,
        "update_action": False,
        "clear_action_due": False,
        "complete_action": False,
        "reopen_action": False,
        "list_status_history": False,
        "vacancy_url": "",
        "vacancy_id": "",
        "vacancy_source": "",
        "show_vacancy": False,
        "status": None,
        "status_correction_reason": "",
        "note": None,
        "export_tracked": "",
    }
    values.update(overrides)
    return Namespace(**values)


def capture_database_command(args: Namespace) -> tuple[int, str]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = run_database_command(args)
    return code, stream.getvalue()


def render_rich(renderable) -> str:
    stream = io.StringIO()
    Console(file=stream, width=120, color_system=None).print(renderable)
    return stream.getvalue()


def seed_database(db_path: Path) -> str:
    database = DatabaseManager(db_path)
    database.initialize()
    run = database.begin_collection_run()
    vacancy = Vacancy(
        source_name="sample",
        source_id="g004-job",
        source_url="https://example.test/g004-job",
        title="Automation Engineer",
        company="Example Co",
        location="Remote",
        salary_text="",
    )
    database.save_vacancy(vacancy, collection_run_id=run.id)
    database.save_analysis(
        VacancyAnalysis(
            vacancy_source_url=vacancy.source_url,
            analysis_method=AnalysisMethod.RULE_BASED,
            score=82,
            fit_label=FitLabel.HIGH,
            explanation="Strong Python automation fit.",
            matched_points=("Python", "automation"),
            missing_points=("cloud",),
        )
    )
    database.finish_collection_run(run.id, status="completed")
    database.save_inbox_preferences(
        InboxPreferences(minimum_score=50, hide_below_threshold=True, sort_by="score")
    )
    database.close()
    return vacancy.source_url


class G004TuiCliAdapterTests(unittest.TestCase):
    def test_cli_explained_inbox_saves_preferences_and_filters_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_inbox.db"
            seed_database(db_path)

            code, output = capture_database_command(
                make_args(
                    db_path,
                    inbox=True,
                    inbox_min_score=80,
                    inbox_hide_below_threshold=True,
                    inbox_sort="title",
                    save_inbox_preferences=True,
                )
            )
            stored = DatabaseManager(db_path).get_inbox_preferences()

        self.assertEqual(code, 0)
        self.assertIn("Explained Inbox", output)
        self.assertIn("Strong Python automation fit.", output)
        self.assertIn("Matched   : Python, automation", output)
        self.assertEqual(stored, InboxPreferences(minimum_score=80, hide_below_threshold=True, sort_by="title"))

    def test_cli_actions_today_and_status_history_share_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_actions.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            tracker = ApplicationTracker(database)
            tracker.ensure_record(vacancy_url)
            tracker.update_status(vacancy_url, ApplicationStatus.APPLIED)
            database.close()

            created_code, created = capture_database_command(
                make_args(
                    db_path,
                    vacancy_url=vacancy_url,
                    action_title="Follow up",
                    action_due="2026-08-08T10:00:00",
                    action_notes="Send email",
                )
            )
            actions = DatabaseManager(db_path).list_action_items()
            today_code, today = capture_database_command(make_args(db_path, today=True))
            completed_code, completed = capture_database_command(
                make_args(db_path, action_id=actions[0].id, complete_action=True)
            )
            history_code, history = capture_database_command(
                make_args(db_path, vacancy_url=vacancy_url, list_status_history=True)
            )

        self.assertEqual(created_code, 0)
        self.assertIn("Action Created", created)
        self.assertIn("Follow up", today)
        self.assertIn("new recommended vacancies", today.lower())
        self.assertEqual(today_code, 0)
        self.assertEqual(completed_code, 0)
        self.assertIn("completed", completed)
        self.assertEqual(history_code, 0)
        self.assertIn("Saved -> Applied", history)
        self.assertIn("origin=cli", history)

    def test_action_service_cli_and_tui_can_edit_action_fields_and_clear_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_action_edit.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            service = ActionService(database)
            service.set_user_timezone("UTC")
            action = service.create_action(
                vacancy_source_url=vacancy_url,
                title="Initial title",
                notes="old notes",
                local_due_at="2026-08-08T10:00:00",
            )
            updated = service.update_action(
                action.id,
                title="Service title",
                notes="service notes",
                local_due_at="2026-08-09T11:00:00",
            )
            database.close()

            self.assertEqual(updated.title, "Service title")
            self.assertEqual(updated.notes, "service notes")
            self.assertEqual(updated.due_at_utc, "2026-08-09T11:00:00Z")

            code, output = capture_database_command(
                make_args(
                    db_path,
                    action_id=action.id,
                    update_action=True,
                    action_title="CLI title",
                    action_notes="cli notes",
                    clear_action_due=True,
                )
            )
            stored_after_cli = DatabaseManager(db_path).get_action_item(action.id)

            tui = JobSeekerTui.__new__(JobSeekerTui)
            tui.state = Namespace(db=str(db_path))
            tui.console = Console(file=io.StringIO(), width=100, color_system=None)
            tui._pause = lambda: None
            with patch("cvbankas_tracker.tui.Prompt.ask", side_effect=["TUI title", "tui notes", ""]), patch(
                "cvbankas_tracker.tui.Confirm.ask", return_value=False
            ):
                tui._edit_action(action.id)
            stored_after_tui = DatabaseManager(db_path).get_action_item(action.id)

        self.assertEqual(code, 0)
        self.assertIn("Action Updated", output)
        self.assertEqual(stored_after_cli.title, "CLI title")
        self.assertEqual(stored_after_cli.notes, "cli notes")
        self.assertIsNone(stored_after_cli.due_at_utc)
        self.assertEqual(stored_after_tui.title, "TUI title")
        self.assertEqual(stored_after_tui.notes, "tui notes")

    def test_tui_create_action_prompts_for_ambiguous_dst_fold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_tui_dst_create.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            ActionService(database).set_user_timezone("Europe/Vilnius")
            database.close()

            output = io.StringIO()
            tui = JobSeekerTui.__new__(JobSeekerTui)
            tui.state = Namespace(db=str(db_path))
            tui.console = Console(file=output, width=100, color_system=None)
            tui._pause = lambda: None

            with patch(
                "cvbankas_tracker.tui.Prompt.ask",
                side_effect=["1", "DST follow up", "2026-10-25T03:30:00", "after fallback", "later"],
            ):
                tui._create_action()

            actions = DatabaseManager(db_path).list_action_items(vacancy_source_url=vacancy_url)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].due_at_utc, "2026-10-25T01:30:00Z")
        self.assertIn("Created action", output.getvalue())

    def test_tui_edit_action_prompts_for_ambiguous_dst_fold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_tui_dst_edit.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            service = ActionService(database)
            service.set_user_timezone("Europe/Vilnius")
            action = service.create_action(vacancy_source_url=vacancy_url, title="Initial")
            database.close()

            output = io.StringIO()
            tui = JobSeekerTui.__new__(JobSeekerTui)
            tui.state = Namespace(db=str(db_path))
            tui.console = Console(file=output, width=100, color_system=None)
            tui._pause = lambda: None

            with patch(
                "cvbankas_tracker.tui.Prompt.ask",
                side_effect=["Edited", "notes", "2026-10-25T03:30:00", "earlier"],
            ), patch("cvbankas_tracker.tui.Confirm.ask", return_value=False):
                tui._edit_action(action.id)

            stored = DatabaseManager(db_path).get_action_item(action.id)

        self.assertEqual(stored.due_at_utc, "2026-10-25T00:30:00Z")
        self.assertIn("Updated action", output.getvalue())

    def test_tui_create_action_reports_nonexistent_dst_time_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_tui_dst_nonexistent.db"
            seed_database(db_path)
            database = DatabaseManager(db_path)
            ActionService(database).set_user_timezone("Europe/Vilnius")
            database.close()

            output = io.StringIO()
            tui = JobSeekerTui.__new__(JobSeekerTui)
            tui.state = Namespace(db=str(db_path))
            tui.console = Console(file=output, width=100, color_system=None)
            tui._pause = lambda: None

            with patch(
                "cvbankas_tracker.tui.Prompt.ask",
                side_effect=["1", "Impossible reminder", "2026-03-29T03:30:00", "notes"],
            ):
                tui._create_action()

            actions = DatabaseManager(db_path).list_action_items()

        self.assertEqual(actions, [])
        self.assertIn("Local time does not exist in Europe/Vilnius", output.getvalue())

    def test_cli_persists_full_inbox_filters_for_tui_and_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_filters.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            tracker = ApplicationTracker(database)
            tracker.ensure_record(vacancy_url)
            tracker.update_status(vacancy_url, ApplicationStatus.APPLIED)
            database.close()

            code, output = capture_database_command(
                make_args(
                    db_path,
                    inbox=True,
                    inbox_source="sample",
                    inbox_fit="High",
                    inbox_status="applied",
                    inbox_new_only=True,
                    inbox_all_runs=True,
                    save_inbox_preferences=True,
                )
            )
            database = DatabaseManager(db_path)
            stored = database.get_inbox_preferences()
            filtered = database.query_inbox()
            database.close()

            tui = JobSeekerTui.__new__(JobSeekerTui)
            panel_output = render_rich(tui._inbox_preferences_panel(stored))

        self.assertEqual(code, 0)
        self.assertIn("status=Applied", output)
        self.assertEqual(stored.source_name, "sample")
        self.assertEqual(stored.fit_label, "High")
        self.assertEqual(stored.application_status, "Applied")
        self.assertTrue(stored.new_only)
        self.assertFalse(stored.current_run_only)
        self.assertEqual([item.source_url for item in filtered], [vacancy_url])
        self.assertIn("Source filter: sample", panel_output)
        self.assertIn("Status filter: Applied", panel_output)

    def test_plain_cli_inbox_uses_saved_new_only_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_plain_new_only.db"
            database = DatabaseManager(db_path)
            database.initialize()

            first_run = database.begin_collection_run()
            old_vacancy = Vacancy(
                source_name="sample",
                source_id="old-job",
                source_url="https://example.test/old-job",
                title="Old Automation Role",
                company="Example Co",
                location="Remote",
                salary_text="",
            )
            database.save_vacancy(old_vacancy, collection_run_id=first_run.id)
            database.save_analysis(
                VacancyAnalysis(
                    vacancy_source_url=old_vacancy.source_url,
                    analysis_method=AnalysisMethod.RULE_BASED,
                    score=88,
                    fit_label=FitLabel.HIGH,
                    explanation="Older strong fit.",
                    matched_points=("automation",),
                    missing_points=(),
                )
            )
            database.finish_collection_run(first_run.id, status="completed")

            second_run = database.begin_collection_run()
            database.record_vacancy_observation(
                old_vacancy.source_url,
                collection_run_id=second_run.id,
                source_name="sample",
            )
            new_vacancy = Vacancy(
                source_name="sample",
                source_id="new-job",
                source_url="https://example.test/new-job",
                title="New Automation Role",
                company="Example Co",
                location="Remote",
                salary_text="",
            )
            database.save_vacancy(new_vacancy, collection_run_id=second_run.id)
            database.save_analysis(
                VacancyAnalysis(
                    vacancy_source_url=new_vacancy.source_url,
                    analysis_method=AnalysisMethod.RULE_BASED,
                    score=80,
                    fit_label=FitLabel.HIGH,
                    explanation="New strong fit.",
                    matched_points=("automation",),
                    missing_points=(),
                )
            )
            database.finish_collection_run(second_run.id, status="completed")
            database.save_inbox_preferences(
                InboxPreferences(new_only=True, current_run_only=True, sort_by="title")
            )
            database.close()

            code, output = capture_database_command(make_args(db_path, inbox=True))

        self.assertEqual(code, 0)
        self.assertIn("new_only=True", output)
        self.assertIn("New Automation Role", output)
        self.assertNotIn("Old Automation Role", output)

    def test_cli_invalid_preferences_and_action_due_return_friendly_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_invalid.db"
            vacancy_url = seed_database(db_path)
            bad_pref_code, bad_pref = capture_database_command(
                make_args(db_path, inbox=True, inbox_min_score=101)
            )
            bad_due_code, bad_due = capture_database_command(
                make_args(
                    db_path,
                    vacancy_url=vacancy_url,
                    action_title="Bad due",
                    action_due="not a datetime",
                )
            )

        self.assertEqual(bad_pref_code, 1)
        self.assertIn("Error: Inbox minimum_score must be between 0 and 100.", bad_pref)
        self.assertNotIn("Traceback", bad_pref)
        self.assertEqual(bad_due_code, 1)
        self.assertIn("Error:", bad_due)
        self.assertNotIn("Traceback", bad_due)

    def test_tui_tables_surface_inbox_today_actions_and_status_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_tui.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            action = ActionService(database).create_action(
                vacancy_source_url=vacancy_url,
                title="Prepare notes",
                local_due_at="2026-08-08T10:00:00",
            )
            tracker = ApplicationTracker(database)
            tracker.ensure_record(vacancy_url)
            tracker.update_status(vacancy_url, ApplicationStatus.APPLIED)
            inbox = database.query_inbox(new_only=True)
            reminders = database.query_action_reminders(now_utc="2026-08-08T08:00:00Z")
            events = database.list_application_status_events(vacancy_url)
            database.close()

        tui = JobSeekerTui.__new__(JobSeekerTui)
        tui.state = Namespace(db=str(db_path))
        inbox_table = tui._inbox_table(inbox, title="Explained inbox")
        today_table = tui._today_table(inbox, reminders)
        actions_table = tui._actions_table([action], title="Actions")

        inbox_output = render_rich(inbox_table)
        today_output = render_rich(today_table)
        actions_output = render_rich(actions_table)

        self.assertIn("Automation Engineer", inbox_output)
        self.assertIn("Strong Python automation", inbox_output)
        self.assertIn("Prepare notes", today_output)
        self.assertIn("Prepare notes", actions_output)
        self.assertIn("Saved -> Applied", tui._status_history_text(events))

    def test_tui_today_actions_use_detected_timezone_after_declined_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "g004_declined_timezone.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            database.create_action_item(
                vacancy_source_url=vacancy_url,
                title="Detected timezone follow-up",
                due_at_utc="2026-08-08T07:00:00Z",
            )
            self.assertEqual(database.get_user_timezone(), "UTC")
            self.assertIsNone(database.get_user_timezone_confirmation())
            database.close()

            output_stream = io.StringIO()
            tui = JobSeekerTui.__new__(JobSeekerTui)
            tui.state = Namespace(db=str(db_path))
            tui.console = Console(file=output_stream, width=120, color_system=None)

            with patch("cvbankas_tracker.tracking.discover_local_timezone", return_value="Europe/Vilnius"), patch(
                "cvbankas_tracker.tui.Prompt.ask", return_value="back"
            ):
                tui._show_today_and_actions()

            database = DatabaseManager(db_path)
            stored_timezone = database.get_user_timezone()
            confirmed_at = database.get_user_timezone_confirmation()
            database.close()

        output = output_stream.getvalue()
        self.assertIn("Detected timezone follow-up", output)
        self.assertIn("2026-08-08T10:00:00+03:00", output)
        self.assertIn("(Europe/Vilnius)", output)
        self.assertNotIn("2026-08-08T07:00:00+00:00", output)
        self.assertNotIn("(UTC)", output)
        self.assertEqual(stored_timezone, "UTC")
        self.assertIsNone(confirmed_at)

    def test_partial_run_label_is_visible_in_cli_and_tui_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "partial_visible.db"
            seed_database(db_path)
            database = DatabaseManager(db_path)
            run = database.begin_collection_run()
            vacancy = Vacancy(
                source_name="sample", source_id="partial-job", source_url="https://example.test/partial-job",
                title="Partial Run Role", company="Example", location="Remote", salary_text=""
            )
            database.save_vacancy(vacancy, collection_run_id=run.id)
            database.save_analysis(VacancyAnalysis(
                vacancy_source_url=vacancy.source_url, analysis_method=AnalysisMethod.RULE_BASED,
                score=91, fit_label=FitLabel.HIGH, explanation="Partial run item.",
                matched_points=("Python",), missing_points=(),
            ))
            database.finish_collection_run(run.id, status="partial")
            preferences = database.get_inbox_preferences()
            latest_run = database.get_latest_inbox_run()
            database.close()

            code, output = capture_database_command(make_args(db_path, inbox=True))
            tui = JobSeekerTui.__new__(JobSeekerTui)

        panel_text = render_rich(tui._inbox_preferences_panel(preferences, latest_run_status=latest_run.status))
        self.assertEqual(code, 0)
        self.assertIn("PARTIAL", output)
        self.assertIn("incomplete collection", panel_text)

    def test_tui_bootstrap_is_one_time_before_ordinary_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch("cvbankas_tracker.tui.DatabaseManager.initialize") as initialize, patch("cvbankas_tracker.tui.ActionService.resolve_user_timezone", return_value="UTC"), patch("cvbankas_tracker.tui.DatabaseManager.get_user_timezone_confirmation", return_value="confirmed"), patch("cvbankas_tracker.tui.Confirm.ask", return_value=False), patch.object(JobSeekerTui, "run", return_value=0):
            db_path = Path(tmp_dir) / "tui_bootstrap.db"
            args = make_args(db_path)
            args.profile = "sample_data/active_profile.json"
            args.export = "exports/test.md"
            args.openai_model = "gpt-4.1-mini"
            args.analysis_strategy = "rule"
            args.limit = 1
            args.max_pages = 1
            args.config = ""
            code = run_tui(args, {})

        self.assertEqual(code, 0)
        initialize.assert_called_once()

    def test_noninteractive_tui_launch_does_not_consume_menu_input_for_timezone_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "noninteractive_tui.db"
            tui = MagicMock()
            tui.state = Namespace(db=str(db_path))
            tui.console = Console(file=io.StringIO(), force_terminal=False)
            tui.run.return_value = 0

            with patch("cvbankas_tracker.tui.JobSeekerTui", return_value=tui), patch(
                "cvbankas_tracker.tui.Confirm.ask"
            ) as confirm:
                code = run_tui(Namespace(), {})

            database = DatabaseManager(db_path)
            confirmed_at = database.get_user_timezone_confirmation()
            database.close()

        self.assertEqual(code, 0)
        confirm.assert_not_called()
        self.assertIsNone(confirmed_at)

    def test_tui_status_change_does_not_reinitialize_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "tui_status.db"
            seed_database(db_path)
            item = DatabaseManager(db_path).list_vacancies_with_latest_scores()[0]
            tui = JobSeekerTui.__new__(JobSeekerTui)
            tui.state = Namespace(db=str(db_path))
            tui.console = Console(file=io.StringIO(), width=120, color_system=None)
            with patch("cvbankas_tracker.tui.Prompt.ask", return_value="applied"), patch.object(JobSeekerTui, "_pause"), patch("cvbankas_tracker.tui.DatabaseManager.initialize") as initialize:
                tui._change_status(item)

        initialize.assert_not_called()

    def test_partial_run_label_prints_for_empty_cli_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "partial_empty.db"
            database = DatabaseManager(db_path)
            database.initialize()
            run = database.begin_collection_run()
            database.finish_collection_run(run.id, status="partial")
            database.close()

            code, output = capture_database_command(make_args(db_path, inbox=True))

        self.assertEqual(code, 2)
        self.assertIn("PARTIAL", output)
        self.assertIn("No inbox vacancies", output)


if __name__ == "__main__":
    unittest.main()
