"""Tests for the run-timeline UI, the list_collection_runs query, and the
`--recover` admin CLI command."""

from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.cvbankas_tracker.main import run_recover_command
from src.cvbankas_tracker.storage import DatabaseManager
from src.cvbankas_tracker.web import _run_duration, _run_view, create_app

BASE = "http://127.0.0.1"

_SUMMARY = {"cvbankas": {"attempted": 5, "failed": 1, "observed": 4, "saved": 3, "pages": 2}}


class ListCollectionRunsTests(unittest.TestCase):
    def test_returns_runs_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "j.db")
            db.initialize()
            first = db.begin_collection_run()
            db.finish_collection_run(first.id, status="completed", source_summary=_SUMMARY)
            second = db.begin_collection_run()
            db.finish_collection_run(second.id, status="cancelled")

            runs = db.list_collection_runs()
            self.assertEqual([r.id for r in runs], [second.id, first.id])
            self.assertEqual(runs[0].status, "cancelled")
            self.assertEqual(runs[1].source_summary["cvbankas"]["attempted"], 5)


class RunViewHelperTests(unittest.TestCase):
    def test_run_duration_formats_and_handles_unfinished(self) -> None:
        self.assertEqual(_run_duration("2026-01-01T00:00:00Z", "2026-01-01T00:01:23Z"), "1m 23s")
        self.assertEqual(_run_duration("2026-01-01T00:00:00Z", "2026-01-01T00:00:07Z"), "7s")
        self.assertEqual(_run_duration("2026-01-01T00:00:00Z", None), "")
        self.assertEqual(_run_duration("bad", "worse"), "")

    def test_run_view_totals_sum_across_sources(self) -> None:
        run = SimpleNamespace(
            id=9,
            status="partial",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:30Z",
            source_summary={
                "cvbankas": {"attempted": 5, "failed": 1, "observed": 4, "saved": 3, "pages": 2},
                "hh": {"attempted": 2, "failed": 0, "observed": 2, "saved": 1, "pages": 1},
            },
            error_summary={"cvbankas": 1},
        )
        view = _run_view(run)
        self.assertEqual(view["status"], "partial")
        self.assertEqual(view["duration"], "30s")
        self.assertEqual(view["totals"]["attempted"], 7)
        self.assertEqual(view["totals"]["failed"], 1)
        self.assertEqual([s["name"] for s in view["sources"]], ["cvbankas", "hh"])

    def test_run_view_tolerates_malformed_summary(self) -> None:
        run = SimpleNamespace(
            id=1, status="failed", started_at="", finished_at=None,
            source_summary="not-a-dict", error_summary=None,
        )
        view = _run_view(run)
        self.assertEqual(view["sources"], [])
        self.assertEqual(view["totals"]["attempted"], 0)


class RunsPageTests(unittest.TestCase):
    def _client(self, tmp: str) -> TestClient:
        app = create_app(Path(tmp) / "web.db", profile_path="sample_data/active_profile.json")
        return TestClient(app, base_url=BASE)

    def test_runs_page_lists_seeded_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            db = DatabaseManager(client.app.state.db_path)
            run = db.begin_collection_run()
            db.finish_collection_run(
                run.id, status="completed", source_summary=_SUMMARY, error_summary={"cvbankas": 1}
            )

            resp = client.get("/runs")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Collection run history", resp.text)
            self.assertIn("completed", resp.text)
            self.assertIn("cvbankas", resp.text)
            self.assertIn(f"run #{run.id}", resp.text)

    def test_runs_page_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resp = self._client(tmp).get("/runs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No collection runs yet", resp.text)


class RecoverCommandTests(unittest.TestCase):
    def test_recover_reaps_stranded_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(Path(tmp) / "j.db")
            db.initialize()
            db.begin_collection_run()  # stranded (never finished)

            args = Namespace(db="j.db")
            with patch("src.cvbankas_tracker.main.Path.cwd", return_value=Path(tmp)):
                with redirect_stdout(StringIO()) as out:
                    code = run_recover_command(args)

            self.assertEqual(code, 0)
            self.assertIn("Recovered 1", out.getvalue())
            # Lease freed: a new run can begin without CollectionRunAlreadyActive.
            self.assertEqual(DatabaseManager(Path(tmp) / "j.db").begin_collection_run().status, "running")

    def test_recover_reports_nothing_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            DatabaseManager(Path(tmp) / "j.db").initialize()
            args = Namespace(db="j.db")
            with patch("src.cvbankas_tracker.main.Path.cwd", return_value=Path(tmp)):
                with redirect_stdout(StringIO()) as out:
                    code = run_recover_command(args)
            self.assertEqual(code, 0)
            self.assertIn("No stranded", out.getvalue())


if __name__ == "__main__":
    unittest.main()
