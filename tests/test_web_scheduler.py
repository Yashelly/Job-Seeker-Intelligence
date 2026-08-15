from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from cvbankas_tracker.web import create_app
from cvbankas_tracker.web_scheduler import (
    DailyScheduler,
    ScheduleConfig,
    ScheduleError,
    SchedulerBusyError,
    load_schedule,
    normalize_time,
    save_schedule,
)

BASE = "http://127.0.0.1"
HEADERS = {"origin": BASE}


def _dt(*args: int) -> datetime:
    """A timezone-aware local stand-in datetime for deterministic scheduler tests."""
    return datetime(*args, tzinfo=UTC)


class ScheduleConfigTests(unittest.TestCase):
    def test_normalize_time_accepts_valid(self) -> None:
        self.assertEqual(normalize_time("09:05"), "09:05")
        self.assertEqual(normalize_time(" 23:59 "), "23:59")

    def test_normalize_time_rejects_invalid(self) -> None:
        for bad in ["7:00", "24:00", "19:60", "", "noon", "1900"]:
            with self.assertRaises(ScheduleError):
                normalize_time(bad)

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scheduler.json"
            cfg = ScheduleConfig(enabled=True, time="08:30", sources=["cvbankas"], keywords=["python"])
            save_schedule(path, cfg)
            loaded = load_schedule(path)
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.time, "08:30")
        self.assertEqual(loaded.sources, ["cvbankas"])
        self.assertEqual(loaded.keywords, ["python"])

    def test_load_missing_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loaded = load_schedule(Path(tmp) / "nope.json")
        self.assertFalse(loaded.enabled)
        self.assertEqual(loaded.time, "19:00")

    def test_load_recovers_from_bad_time_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text('{"enabled": true, "time": "99:99"}', encoding="utf-8")
            loaded = load_schedule(path)
        self.assertEqual(loaded.time, "19:00")


class SchedulerTickTests(unittest.TestCase):
    def _sched(self, tmp: str, cfg: ScheduleConfig, runner):
        return DailyScheduler(Path(tmp) / "s.json", runner, config=cfg)

    def test_fires_once_per_day_at_or_after_time(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            s = self._sched(tmp, ScheduleConfig(enabled=True, time="19:00"), lambda c: calls.append(1) or 7)
            self.assertFalse(s.tick(_dt(2026, 8, 15, 8, 0)))
            self.assertTrue(s.tick(_dt(2026, 8, 15, 19, 0)))
            self.assertFalse(s.tick(_dt(2026, 8, 15, 23, 0)))
            self.assertTrue(s.tick(_dt(2026, 8, 16, 19, 30)))
        self.assertEqual(len(calls), 2)

    def test_disabled_never_fires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = self._sched(tmp, ScheduleConfig(enabled=False, time="00:00"), lambda c: 1)
            self.assertFalse(s.tick(_dt(2026, 8, 15, 12, 0)))

    def test_conflict_leaves_run_date_unset_for_retry(self) -> None:
        def busy(_cfg):
            raise SchedulerBusyError("busy")

        with tempfile.TemporaryDirectory() as tmp:
            s = self._sched(tmp, ScheduleConfig(enabled=True, time="09:00"), busy)
            self.assertFalse(s.tick(_dt(2026, 8, 15, 9, 1)))
            self.assertEqual(s.config.last_status, "conflict")
            self.assertEqual(s.config.last_run_date, "")

    def test_generic_error_sets_run_date_to_avoid_spin(self) -> None:
        def boom(_cfg):
            raise RuntimeError("kaboom")

        with tempfile.TemporaryDirectory() as tmp:
            s = self._sched(tmp, ScheduleConfig(enabled=True, time="09:00"), boom)
            self.assertFalse(s.tick(_dt(2026, 8, 15, 9, 1)))
            self.assertTrue(s.config.last_status.startswith("error"))
            self.assertEqual(s.config.last_run_date, "2026-08-15")

    def test_next_run_rolls_to_tomorrow_after_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = self._sched(tmp, ScheduleConfig(enabled=True, time="19:00"), lambda c: 1)
            before = s.next_run(_dt(2026, 8, 15, 8, 0))
            self.assertEqual(before.isoformat(), "2026-08-15T19:00:00+00:00")
            after = s.next_run(_dt(2026, 8, 15, 20, 0))
            self.assertEqual(after.isoformat(), "2026-08-16T19:00:00+00:00")

    def test_update_persists_and_wakes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            s = DailyScheduler(path, lambda c: 1, config=ScheduleConfig())
            s.update(
                enabled=True,
                time="07:15",
                sources=["cvbankas", "hh"],
                keywords=["python"],
                limit=5,
                max_pages=2,
                analysis_strategy="rule",
            )
            reloaded = load_schedule(path)
        self.assertTrue(reloaded.enabled)
        self.assertEqual(reloaded.time, "07:15")
        self.assertEqual(reloaded.limit, 5)
        self.assertEqual(reloaded.analysis_strategy, "rule")


def _client(tmp: str) -> TestClient:
    app = create_app(Path(tmp) / "web.db", profile_path="sample_data/active_profile.json")
    return TestClient(app, base_url=BASE)


def _csrf(client: TestClient) -> str:
    client.get("/schedule")
    return client.cookies.get("job_seeker_csrf")


class ScheduleRoutesTests(unittest.TestCase):
    def test_schedule_page_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            resp = client.get("/schedule")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Daily schedule", resp.text)

    def test_save_persists_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client)
            resp = client.post(
                "/schedule/save",
                data={
                    "csrf_token": token,
                    "enabled": "on",
                    "time": "06:45",
                    "source_cvbankas": "on",
                    "keywords": "python\nfastapi",
                    "limit": "8",
                    "max_pages": "2",
                    "analysis_strategy": "ai",
                },
                headers=HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
            snap = client.app.state.scheduler.config
        self.assertTrue(snap.enabled)
        self.assertEqual(snap.time, "06:45")
        self.assertEqual(snap.sources, ["cvbankas"])

    def test_save_rejects_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client)
            resp = client.post(
                "/schedule/save",
                data={"csrf_token": token, "enabled": "on", "time": "06:45"},
                headers=HEADERS,
            )
            self.assertEqual(resp.status_code, 400)

    def test_save_rejects_bad_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client)
            resp = client.post(
                "/schedule/save",
                data={"csrf_token": token, "time": "99:99", "source_cvbankas": "on"},
                headers=HEADERS,
            )
            self.assertEqual(resp.status_code, 400)

    def test_run_now_starts_job(self) -> None:
        def fake_run_batch(args, cfg=None) -> int:
            assert getattr(args, "daily_run", False) is True
            print("daily done")
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client)
            with patch("cvbankas_tracker.web.run_batch", fake_run_batch):
                resp = client.post(
                    "/schedule/run-now",
                    data={"csrf_token": token},
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                job_id = int(resp.headers["location"].rsplit("/", 1)[-1])
                deadline = time.time() + 5
                snap = {}
                while time.time() < deadline:
                    snap = client.get(f"/jobs/{job_id}/log").json()
                    if snap["status"] == "done":
                        break
                    time.sleep(0.05)
        self.assertEqual(snap["status"], "done")
        self.assertIn("daily done", snap["log"])


if __name__ == "__main__":
    unittest.main()
