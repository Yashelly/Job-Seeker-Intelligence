from __future__ import annotations

import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from cvbankas_tracker.models import (
    ActionState,
    ApplicationStatus,
    ApplicationStatusEventKind,
    ApplicationStatusOrigin,
    Vacancy,
    VacancyListItem,
)
from cvbankas_tracker.storage import DatabaseManager
from cvbankas_tracker.tracking import (
    ActionService,
    ApplicationTracker,
    discover_local_timezone,
    local_datetime_to_utc_iso,
    utc_iso_to_local_datetime,
)
from cvbankas_tracker.tui import JobSeekerTui


def make_vacancy(url: str = "https://example.test/job") -> Vacancy:
    return Vacancy(
        source_name="sample",
        source_id="job",
        source_url=url,
        title="Backend Engineer",
        company="Example",
        location="Remote",
        salary_text="",
    )


class G003PipelineActionsTests(unittest.TestCase):
    def test_migration_creates_one_baseline_event_for_each_legacy_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "legacy_events.db"
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
                    raw_text TEXT NOT NULL DEFAULT ''
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
                """
            )
            for index, status in enumerate(("Saved", "Interview"), start=1):
                url = f"https://example.test/job/{index}"
                connection.execute(
                    "INSERT INTO vacancies (source_url, source_id, title, company, location, salary_text, requirements_json, responsibilities_json) VALUES (?, ?, ?, 'Co', 'Remote', '', '[]', '[]')",
                    (url, f"job-{index}", f"Job {index}"),
                )
                connection.execute(
                    "INSERT INTO applications (vacancy_source_url, status, notes) VALUES (?, ?, ?)",
                    (url, status, f"legacy {index}"),
                )
            connection.commit()
            connection.close()

            database = DatabaseManager(db_path)
            database.initialize()

            first = database.list_application_status_events("https://example.test/job/1")
            second = database.list_application_status_events("https://example.test/job/2")

        self.assertEqual([event.origin for event in first + second], [ApplicationStatusOrigin.MIGRATION] * 2)
        self.assertEqual([event.kind for event in first + second], [ApplicationStatusEventKind.BASELINE] * 2)
        self.assertEqual(first[0].previous_status, None)
        self.assertEqual(second[0].new_status, ApplicationStatus.INTERVIEW)
        self.assertIn("prior status history is unavailable", second[0].reason)

    def test_guarded_status_transitions_and_corrective_audit_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "status.db")
            database.initialize()
            vacancy = make_vacancy()
            database.save_vacancy(vacancy)
            tracker = ApplicationTracker(database)
            tracker.ensure_record(vacancy.source_url)

            with self.assertRaises(ValueError):
                tracker.update_status(vacancy.source_url, ApplicationStatus.OFFER)

            tracker.update_status(vacancy.source_url, ApplicationStatus.APPLIED, origin=ApplicationStatusOrigin.TUI)
            tracker.set_status(
                vacancy.source_url,
                ApplicationStatus.SAVED,
                origin=ApplicationStatusOrigin.WEB,
                reason="Undo accidental applied mark.",
            )
            events = database.list_application_status_events(vacancy.source_url)
            record = database.get_application_record(vacancy.source_url)

        self.assertEqual(record.status, ApplicationStatus.SAVED)
        self.assertEqual([event.new_status for event in events], [ApplicationStatus.SAVED, ApplicationStatus.APPLIED, ApplicationStatus.SAVED])
        self.assertEqual(events[-2].origin, ApplicationStatusOrigin.TUI)
        self.assertEqual(events[-1].origin, ApplicationStatusOrigin.WEB)
        self.assertEqual(events[-1].kind, ApplicationStatusEventKind.CORRECTIVE)
        self.assertEqual(events[-1].previous_status, ApplicationStatus.APPLIED)
        self.assertEqual(events[-1].reason, "Undo accidental applied mark.")

    def test_action_lifecycle_reminders_and_timezone_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "actions.db")
            database.initialize()
            vacancy = make_vacancy()
            database.save_vacancy(vacancy)
            actions = ActionService(database)
            actions.set_user_timezone("Europe/Vilnius")

            overdue = actions.create_action(
                vacancy_source_url=vacancy.source_url,
                title="Follow up",
                notes="Send short email",
                local_due_at="2026-08-08T10:00:00",
            )
            soon = database.create_action_item(
                vacancy_source_url=vacancy.source_url,
                title="Prepare interview",
                due_at_utc="2026-08-08T20:00:00Z",
            )
            later = database.create_action_item(
                vacancy_source_url=vacancy.source_url,
                title="Later",
                due_at_utc="2026-08-10T20:00:00Z",
            )
            later = database.update_action_item(later.id, title="Later follow-up", due_at_utc=None)
            completed = actions.complete_action(soon.id)
            reopened = actions.reopen_action(soon.id)
            reminders = database.query_action_reminders(now_utc="2026-08-08T12:00:00Z")
            user_timezone = database.get_user_timezone()

        self.assertEqual(user_timezone, "Europe/Vilnius")
        self.assertEqual(overdue.due_at_utc, "2026-08-08T07:00:00Z")
        self.assertEqual(completed.state, ActionState.COMPLETED)
        self.assertIsNotNone(completed.completed_at_utc)
        self.assertEqual(reopened.state, ActionState.OPEN)
        self.assertIsNone(reopened.completed_at_utc)
        self.assertEqual(later.state, ActionState.OPEN)
        self.assertEqual(later.title, "Later follow-up")
        self.assertIsNone(later.due_at_utc)
        self.assertEqual([(item.action.title, item.reminder_state) for item in reminders], [("Follow up", "overdue"), ("Prepare interview", "due_soon")])


    def test_timezone_discovery_is_display_only_until_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict("os.environ", {"TZ": "Europe/Vilnius"}):
            database = DatabaseManager(Path(tmp_dir) / "timezone_confirm.db")
            database.initialize()
            actions = ActionService(database)
            discovered = discover_local_timezone()
            resolved = actions.ensure_user_timezone_confirmed()
            stored_before_confirmation = database.get_user_timezone()
            confirmed_at_before = database.get_user_timezone_confirmation()
            confirmed = actions.confirm_user_timezone(resolved)
            stored_after_confirmation = database.get_user_timezone()
            confirmed_at_after = database.get_user_timezone_confirmation()

        self.assertEqual(discovered, "Europe/Vilnius")
        self.assertEqual(resolved, "Europe/Vilnius")
        self.assertEqual(stored_before_confirmation, "UTC")
        self.assertIsNone(confirmed_at_before)
        self.assertEqual(confirmed, "Europe/Vilnius")
        self.assertEqual(stored_after_confirmation, "Europe/Vilnius")
        self.assertIsNotNone(confirmed_at_after)

    def test_windows_timezone_discovery_maps_fle_standard_time_to_vilnius(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch("cvbankas_tracker.tracking.sys.platform", "win32"), patch("cvbankas_tracker.tracking._windows_timezone_key", return_value="FLE Standard Time"), patch("cvbankas_tracker.tracking._timezone_offsets_match", return_value=True):
            self.assertEqual(discover_local_timezone(), "Europe/Vilnius")

    def test_dst_nonexistent_and_ambiguous_local_time_contract(self) -> None:
        with self.assertRaises(ValueError):
            local_datetime_to_utc_iso("2026-03-29T03:30:00", "Europe/Vilnius")
        with self.assertRaises(ValueError):
            local_datetime_to_utc_iso("2026-10-25T03:30:00", "Europe/Vilnius")

        earlier = local_datetime_to_utc_iso("2026-10-25T03:30:00", "Europe/Vilnius", fold=0)
        later = local_datetime_to_utc_iso("2026-10-25T03:30:00", "Europe/Vilnius", fold=1)

        self.assertEqual(earlier, "2026-10-25T00:30:00Z")
        self.assertEqual(later, "2026-10-25T01:30:00Z")
        self.assertTrue(utc_iso_to_local_datetime(earlier, "Europe/Vilnius").startswith("2026-10-25T03:30:00"))

    def test_storage_rejects_invalid_direct_status_write_without_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "direct_status.db")
            database.initialize()
            vacancy = make_vacancy()
            database.save_vacancy(vacancy)
            tracker = ApplicationTracker(database)
            record = tracker.ensure_record(vacancy.source_url)
            record.set_status(ApplicationStatus.OFFER)

            with self.assertRaises(ValueError):
                database.save_application_record(record)

            events = database.list_application_status_events(vacancy.source_url)
            stored = database.get_application_record(vacancy.source_url)

        self.assertEqual(stored.status, ApplicationStatus.SAVED)
        self.assertEqual([event.new_status for event in events], [ApplicationStatus.SAVED])

    def test_due_at_utc_persistence_boundary_normalizes_and_rejects_bad_instants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "due_boundary.db")
            database.initialize()
            vacancy = make_vacancy()
            database.save_vacancy(vacancy)

            normalized = database.create_action_item(
                vacancy_source_url=vacancy.source_url,
                title="Offset due time",
                due_at_utc="2026-08-08T15:30:00+03:00",
            )
            with self.assertRaises(ValueError):
                database.create_action_item(
                    vacancy_source_url=vacancy.source_url,
                    title="Naive due time",
                    due_at_utc="2026-08-08T15:30:00",
                )
            with self.assertRaises(ValueError):
                database.query_action_reminders(now_utc="not a date")

        self.assertEqual(normalized.due_at_utc, "2026-08-08T12:30:00Z")

    def test_tui_status_uses_normal_tui_origin_then_corrective_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "tui_status.db"
            database = DatabaseManager(db_path)
            database.initialize()
            vacancy = make_vacancy()
            database.save_vacancy(vacancy)
            item = VacancyListItem(
                source_name=vacancy.source_name,
                source_id=vacancy.source_id,
                source_url=vacancy.source_url,
                title=vacancy.title,
                company=vacancy.company,
                location=vacancy.location,
                latest_score=None,
                latest_fit_label=None,
                application_status=None,
            )
            tui = JobSeekerTui.__new__(JobSeekerTui)
            tui.state = Namespace(db=str(db_path))
            tui.console = __import__("rich.console").console.Console(file=__import__("io").StringIO())
            tui._pause = lambda: None

            with patch("cvbankas_tracker.tui.Prompt.ask", return_value="applied"):
                tui._change_status(item)
            with patch("cvbankas_tracker.tui.Prompt.ask", side_effect=["saved", "Undo from TUI"]), patch(
                "cvbankas_tracker.tui.Confirm.ask", return_value=True
            ):
                tui._change_status(item)

            events = database.list_application_status_events(vacancy.source_url)
            stored = database.get_application_record(vacancy.source_url)

        self.assertEqual(stored.status, ApplicationStatus.SAVED)
        self.assertEqual([event.origin for event in events], [ApplicationStatusOrigin.TUI] * 3)
        self.assertEqual(events[-2].kind, ApplicationStatusEventKind.NORMAL)
        self.assertEqual(events[-1].kind, ApplicationStatusEventKind.CORRECTIVE)
        self.assertEqual(events[-1].reason, "Undo from TUI")


if __name__ == "__main__":
    unittest.main()
