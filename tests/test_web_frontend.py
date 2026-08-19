from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from cvbankas_tracker.models import (
    AnalysisMethod,
    FitLabel,
    Vacancy,
    VacancyAnalysis,
)
from cvbankas_tracker.storage import DatabaseManager
from cvbankas_tracker.web import create_app
from cvbankas_tracker.web_jobs import JobConflictError, JobManager

BASE = "http://127.0.0.1"
HEADERS = {"origin": BASE}


def _client(tmp_dir: str) -> TestClient:
    app = create_app(Path(tmp_dir) / "web.db", profile_path="sample_data/active_profile.json")
    return TestClient(app, base_url=BASE)


def _csrf(client: TestClient, page: str) -> str:
    client.get(page)
    return client.cookies.get("job_seeker_csrf")


def _wait_for_status(client: TestClient, job_id: int, target: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    snapshot: dict = {}
    while time.time() < deadline:
        snapshot = client.get(f"/jobs/{job_id}/log").json()
        if snapshot["status"] == target:
            return snapshot
        time.sleep(0.05)
    return snapshot


class JobManagerTests(unittest.TestCase):
    def test_runs_job_and_captures_log(self) -> None:
        manager = JobManager()

        def target(control) -> int:
            print("hello from job")
            return 0

        job = manager.start("search", target)
        for _ in range(100):
            if manager.get(job.id).status != "running":
                break
            time.sleep(0.02)
        snap = manager.snapshot(job.id)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["exit_code"], 0)
        self.assertIn("hello from job", snap["log"])

    def test_error_is_captured(self) -> None:
        manager = JobManager()

        def boom(control) -> int:
            raise RuntimeError("kaboom")

        job = manager.start("search", boom)
        for _ in range(100):
            if manager.get(job.id).status != "running":
                break
            time.sleep(0.02)
        snap = manager.snapshot(job.id)
        self.assertEqual(snap["status"], "error")
        self.assertIn("kaboom", snap["log"])

    def test_single_active_job_guard(self) -> None:
        manager = JobManager()
        started = _Barrier()

        def slow(control) -> int:
            started.set()
            time.sleep(0.3)
            return 0

        job = manager.start("search", slow)
        started.wait()
        with self.assertRaises(JobConflictError):
            manager.start("search", lambda control: 0)
        # let the first finish
        for _ in range(100):
            if manager.get(job.id).status != "running":
                break
            time.sleep(0.02)

    def _run_to_completion(self, manager: JobManager, kind: str, target) -> int:
        job = manager.start(kind, target)
        for _ in range(200):
            if manager.get(job.id).status != "running":
                break
            time.sleep(0.01)
        return job.id

    def test_log_is_bounded(self) -> None:
        from cvbankas_tracker.web_jobs import MAX_LOG_CHARS

        manager = JobManager()

        def noisy(control) -> int:
            print("A" * (MAX_LOG_CHARS * 2))
            return 0

        job_id = self._run_to_completion(manager, "search", noisy)
        snap = manager.snapshot(job_id)
        self.assertLessEqual(len(snap["log"]), MAX_LOG_CHARS)
        self.assertTrue(snap["log"].startswith("…[earlier output truncated]…"))

    def test_finished_at_is_recorded(self) -> None:
        manager = JobManager()
        job_id = self._run_to_completion(manager, "search", lambda control: 0)
        snap = manager.snapshot(job_id)
        self.assertEqual(snap["status"], "done")
        self.assertTrue(snap["finished_at"])

    def test_pause_resume_cancel_flow(self) -> None:
        import threading

        manager = JobManager()
        started = _Barrier()

        def target(control) -> int:
            started.set()
            for _ in range(200):
                control.wait_if_paused()
                if control.is_cancelled():
                    return 0
                time.sleep(0.01)
            return 0

        job = manager.start("search", target)
        started.wait()
        self.assertTrue(manager.pause(job.id))
        self.assertEqual(manager.get(job.id).status, "paused")
        self.assertTrue(manager.resume(job.id))
        self.assertEqual(manager.get(job.id).status, "running")
        self.assertTrue(manager.cancel(job.id))
        for _ in range(200):
            if manager.get(job.id).status != "running":
                break
            time.sleep(0.02)
        self.assertEqual(manager.snapshot(job.id)["status"], "cancelled")
        _ = threading  # imported for clarity that the flow is concurrent

    def test_job_control_blocks_while_paused(self) -> None:
        import threading

        from cvbankas_tracker.web_jobs import JobControl

        control = JobControl()
        control.pause()
        self.assertTrue(control.is_paused)
        unblocked = threading.Event()

        def worker() -> None:
            control.wait_if_paused()
            unblocked.set()

        threading.Thread(target=worker, daemon=True).start()
        self.assertFalse(unblocked.wait(0.2))  # still parked while paused
        control.resume()
        self.assertTrue(unblocked.wait(1.0))  # released on resume
        control.pause()
        control.cancel()  # cancel also unblocks and clears paused
        self.assertTrue(control.is_cancelled())
        self.assertFalse(control.is_paused)

    def test_completed_jobs_are_pruned(self) -> None:
        from cvbankas_tracker.web_jobs import MAX_COMPLETED_JOBS

        manager = JobManager()
        for _ in range(MAX_COMPLETED_JOBS + 5):
            self._run_to_completion(manager, "search", lambda control: 0)
        # Pruning happens at the start of each run, so the most recent finished
        # job survives until the next start(): the bound is MAX + 1.
        with manager._lock:
            self.assertLessEqual(len(manager._jobs), MAX_COMPLETED_JOBS + 1)


class _Barrier:
    def __init__(self) -> None:
        import threading

        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def wait(self) -> None:
        self._event.wait(timeout=2)


class SearchImportTests(unittest.TestCase):
    def test_start_search_runs_job_with_expected_args(self) -> None:
        captured: dict = {}

        def fake_run_batch(args, cfg=None, control=None) -> int:
            captured["sources"] = args.sources
            captured["keywords"] = args.keywords
            captured["limit"] = args.limit
            print("batch done")
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/search")
            with patch("cvbankas_tracker.web.run_batch", fake_run_batch):
                resp = client.post(
                    "/search/start",
                    data={
                        "csrf_token": token,
                        "source_cvbankas": "on",
                        "use_keywords": "on",
                        "keywords": "python developer\nfastapi",
                        "limit": "7",
                        "max_pages": "2",
                        "analysis_strategy": "ai",
                    },
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                job_id = int(resp.headers["location"].rsplit("/", 1)[-1])
                snap = _wait_for_status(client, job_id, "done")
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["exit_code"], 0)
        self.assertEqual(captured["sources"], "cvbankas")
        self.assertIn("python developer", captured["keywords"])
        self.assertEqual(captured["limit"], 7)

    def test_profile_search_derives_keywords_from_profile(self) -> None:
        captured: dict = {}

        def fake_run_batch(args, cfg=None, control=None) -> int:
            captured["keywords"] = args.keywords
            captured["cfg"] = cfg
            print("batch done")
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/search")
            with patch("cvbankas_tracker.web.run_batch", fake_run_batch):
                resp = client.post(
                    "/search/start",
                    data={"csrf_token": token, "source_cvbankas": "on"},
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                job_id = int(resp.headers["location"].rsplit("/", 1)[-1])
                _wait_for_status(client, job_id, "done")
        # The sample profile's additional_keywords seed the search, and they are
        # injected per-source into the run config so run_batch actually uses them.
        self.assertIn("fastapi", captured["keywords"])
        self.assertIn("fastapi", captured["cfg"]["sources"]["keywords"]["cvbankas"])

    def test_search_passes_prune_autosave_and_infinite_to_runner(self) -> None:
        captured: dict = {}

        def fake_run_batch(args, cfg=None, control=None) -> int:
            captured["prune_threshold"] = args.prune_threshold
            captured["auto_save"] = args.auto_save
            captured["auto_save_threshold"] = args.auto_save_threshold
            captured["infinite"] = args.infinite
            print("done")
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/search")
            with patch("cvbankas_tracker.web.run_batch", fake_run_batch):
                resp = client.post(
                    "/search/start",
                    data={
                        "csrf_token": token,
                        "source_cvbankas": "on",
                        "use_keywords": "on",
                        "keywords": "python",
                        "prune_threshold": "50",
                        "auto_save": "on",
                        "auto_save_threshold": "65",
                        "infinite": "on",
                    },
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                job_id = int(resp.headers["location"].rsplit("/", 1)[-1])
                _wait_for_status(client, job_id, "done")
        self.assertEqual(captured["prune_threshold"], 50)
        self.assertTrue(captured["auto_save"])
        self.assertEqual(captured["auto_save_threshold"], 65)
        self.assertTrue(captured["infinite"])

    def test_keyword_search_without_keywords_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/search")
            resp = client.post(
                "/search/start",
                data={"csrf_token": token, "source_cvbankas": "on", "use_keywords": "on"},
                headers=HEADERS,
            )
            self.assertEqual(resp.status_code, 400)

    def test_start_search_requires_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/search")
            resp = client.post(
                "/search/start",
                data={"csrf_token": token, "keywords": "x"},
                headers=HEADERS,
            )
            self.assertEqual(resp.status_code, 400)

    def test_start_import_runs_job(self) -> None:
        def fake_run_import(args, cfg=None) -> int:
            print("import " + args.import_urls)
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/import")
            with patch("cvbankas_tracker.web.run_import", fake_run_import):
                resp = client.post(
                    "/import/start",
                    data={"csrf_token": token, "import_urls": "https://example.test/job"},
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                job_id = int(resp.headers["location"].rsplit("/", 1)[-1])
                snap = _wait_for_status(client, job_id, "done")
        self.assertIn("example.test", snap["log"])

    def test_save_button_creates_saved_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "web.db"
            url = "https://example.test/save-me"
            db = DatabaseManager(db_path)
            db.initialize()
            db.save_processed_vacancy(
                vacancy=Vacancy(
                    source_name="sample", source_id="1", source_url=url,
                    title="Role", company="Co", location="Remote",
                    salary_text="", requirements=[], responsibilities=[],
                ),
                analysis=VacancyAnalysis(
                    vacancy_source_url=url, analysis_method=AnalysisMethod.RULE_BASED,
                    score=12, fit_label=FitLabel.LOW, explanation="x",
                    matched_points=(), missing_points=(), notes="",
                ),
                auto_save=False,  # unsaved low-match vacancy
            )
            db.close()

            client = TestClient(create_app(db_path), base_url=BASE)
            token = _csrf(client, "/today")
            self.assertIsNone(DatabaseManager(db_path).get_application_record(url))
            resp = client.post(
                "/vacancy/save",
                data={"csrf_token": token, "vacancy_source_url": url},
                headers=HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
            record = DatabaseManager(db_path).get_application_record(url)
            self.assertIsNotNone(record)

    def test_active_profile_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "web.db"
            profile_file = Path(tmp) / "mine.json"
            profile_file.write_text(json.dumps(GENERATED), encoding="utf-8")

            db = DatabaseManager(db_path)
            db.initialize()
            db.save_active_profile_path(str(profile_file))
            db.close()

            # A fresh app (as after a restart/redeploy) restores the saved choice
            # instead of falling back to the launch profile.
            app = create_app(db_path, profile_path="sample_data/active_profile.json")
            self.assertEqual(app.state.profile_path, str(profile_file))

    def test_start_search_rejected_without_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/search")
            resp = client.post(
                "/search/start",
                data={"csrf_token": token, "source_cvbankas": "on"},
            )
            self.assertEqual(resp.status_code, 403)


GENERATED = {
    "name": "Jane Doe",
    "experience_level": "Senior",
    "years_of_experience": 8,
    "salary_expectation": None,
    "target_roles": ["Python Developer"],
    "skills": ["python", "fastapi"],
    "preferred_locations": ["Remote"],
    "must_have_skills": ["python"],
    "nice_to_have_skills": ["aws"],
    "excluded_keywords": ["warehouse"],
    "additional_keywords": ["backend"],
}


class ProfileUploadTests(unittest.TestCase):
    def test_build_and_save_profile_txt_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/profile")
            with patch("cvbankas_tracker.web.resolve_ai_backend", return_value="claude_cli"), patch(
                "cvbankas_tracker.web.generate_profile_dict", return_value=GENERATED
            ):
                resp = client.post(
                    "/profile/build",
                    data={"csrf_token": token},
                    files={"cv_file": ("cv.txt", b"Jane Doe\nSenior Python developer", "text/plain")},
                    headers=HEADERS,
                )
                self.assertEqual(resp.status_code, 200)
                self.assertIn("Jane Doe", resp.text)

            import json as _json
            import os

            # Saves are constrained to the working tree; run from tmp so a
            # relative path lands inside it instead of polluting the repo.
            prev_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                token = _csrf(client, "/profile")
                resp = client.post(
                    "/profile/save",
                    data={
                        "csrf_token": token,
                        "profile_json": _json.dumps(GENERATED),
                        "save_path": "out_profile.json",
                    },
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                saved = Path(tmp) / "out_profile.json"
                self.assertTrue(saved.exists())

                from cvbankas_tracker.io_utils import ProfileFileReader

                loaded = ProfileFileReader().read(saved)
                self.assertEqual(loaded.name, "Jane Doe")
            finally:
                os.chdir(prev_cwd)

    def test_save_rejects_path_escaping_working_tree(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/profile")
            for bad_path in ("../escape.json", str(Path(tmp) / "abs_escape.json")):
                resp = client.post(
                    "/profile/save",
                    data={
                        "csrf_token": token,
                        "profile_json": _json.dumps(GENERATED),
                        "save_path": bad_path,
                    },
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 400, bad_path)
                self.assertFalse((Path(tmp) / "abs_escape.json").exists())

    def test_build_rejects_oversized_cv(self) -> None:
        from cvbankas_tracker.profile_builder import MAX_CV_UPLOAD_BYTES

        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/profile")
            oversized = b"x" * (MAX_CV_UPLOAD_BYTES + 1024)
            with patch("cvbankas_tracker.web.resolve_ai_backend", return_value="claude_cli"), patch(
                "cvbankas_tracker.web.generate_profile_dict"
            ) as gen:
                resp = client.post(
                    "/profile/build",
                    data={"csrf_token": token},
                    files={"cv_file": ("cv.txt", oversized, "text/plain")},
                    headers=HEADERS,
                )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("too large", resp.text)
            gen.assert_not_called()

    def test_build_rejects_non_ai_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/profile")
            with patch("cvbankas_tracker.web.resolve_ai_backend", return_value="rule"), patch(
                "cvbankas_tracker.web.generate_profile_dict"
            ) as gen:
                resp = client.post(
                    "/profile/build",
                    data={"csrf_token": token},
                    files={"cv_file": ("cv.txt", b"text", "text/plain")},
                    headers=HEADERS,
                )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("needs an AI backend", resp.text)
            gen.assert_not_called()

    def test_build_rejects_unsupported_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/profile")
            with patch("cvbankas_tracker.web.resolve_ai_backend", return_value="claude_cli"):
                resp = client.post(
                    "/profile/build",
                    data={"csrf_token": token},
                    files={"cv_file": ("cv.rtf", b"text", "application/rtf")},
                    headers=HEADERS,
                )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Unsupported CV type", resp.text)


class BackendSwitchTests(unittest.TestCase):
    def _restore(self, prev: str | None) -> None:
        import os

        if prev is None:
            os.environ.pop("AI_BACKEND", None)
        else:
            os.environ["AI_BACKEND"] = prev

    def test_settings_page_shows_backend_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            resp = client.get("/settings")
        self.assertIn("Apply backend", resp.text)

    def test_switch_backend_sets_env_and_redirects(self) -> None:
        import os

        prev = os.environ.get("AI_BACKEND")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = _client(tmp)
                token = _csrf(client, "/settings")
                resp = client.post(
                    "/settings/backend",
                    data={"csrf_token": token, "ai_backend": "claude_cli", "next": "/profile"},
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                self.assertEqual(resp.headers["location"], "/profile")
                from cvbankas_tracker.main import resolve_ai_backend

                self.assertEqual(resolve_ai_backend(), "claude_cli")
        finally:
            self._restore(prev)

    def test_switch_backend_rejects_invalid(self) -> None:
        import os

        prev = os.environ.get("AI_BACKEND")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = _client(tmp)
                token = _csrf(client, "/settings")
                resp = client.post(
                    "/settings/backend",
                    data={"csrf_token": token, "ai_backend": "bogus"},
                    headers=HEADERS,
                )
            self.assertEqual(resp.status_code, 400)
        finally:
            self._restore(prev)

    def test_switch_backend_ignores_external_next(self) -> None:
        import os

        prev = os.environ.get("AI_BACKEND")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = _client(tmp)
                token = _csrf(client, "/settings")
                resp = client.post(
                    "/settings/backend",
                    data={"csrf_token": token, "ai_backend": "rule", "next": "http://evil.test/x"},
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                self.assertEqual(resp.headers["location"], "/settings")
        finally:
            self._restore(prev)


class ProfileActivationTests(unittest.TestCase):
    def test_discovery_lists_valid_profiles_only(self) -> None:
        from cvbankas_tracker.web import _discover_profile_files

        profiles = _discover_profile_files(Path.cwd(), "sample_data/active_profile.json")
        paths = {p["path"].replace("\\", "/") for p in profiles}
        self.assertIn("profile_from_cv.json", paths)
        self.assertIn("sample_data/active_profile.json", paths)
        # config/scheduler.json is JSON but not a profile, so it must be skipped.
        self.assertNotIn("config/scheduler.json", paths)
        active = [p for p in profiles if p["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["path"].replace("\\", "/"), "sample_data/active_profile.json")

    def test_activate_switches_and_persists_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/profile")

            before = client.get("/profile").text
            self.assertRegex(before, r"Path:[^<]*active_profile\.json")

            resp = client.post(
                "/profile/activate",
                headers=HEADERS,
                data={"csrf_token": token, "profile_path": "profile_from_cv.json"},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)

            after = client.get("/profile").text
            self.assertRegex(after, r"Path:[^<]*profile_from_cv\.json")

    def test_activate_rejects_unknown_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/profile")
            resp = client.post(
                "/profile/activate",
                headers=HEADERS,
                data={"csrf_token": token, "profile_path": "does_not_exist.json"},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Could not activate profile", resp.text)
            # The active profile must be unchanged after a failed activation.
            self.assertRegex(resp.text, r"Path:[^<]*active_profile\.json")


class WorkModePersistenceTests(unittest.TestCase):
    def _write_profile(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "name": "Temp",
                    "target_roles": ["developer"],
                    "skills": ["python"],
                    "preferred_locations": ["Remote"],
                    "experience_level": "Junior",
                }
            ),
            encoding="utf-8",
        )

    def test_save_work_modes_persists_to_active_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "profile.json"
            self._write_profile(profile_path)
            client = _client(tmp)
            # Point the app at the temp profile so the real sample is untouched.
            client.app.state.profile_path = str(profile_path)

            token = _csrf(client, "/profile")
            resp = client.post(
                "/profile/work-modes",
                headers=HEADERS,
                data={
                    "csrf_token": token,
                    "remote": "on",
                    "hybrid": "on",
                    "hybrid_country": "Lithuania",
                },
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)

            saved = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["work_modes"],
                [
                    {"mode": "remote", "country": ""},
                    {"mode": "hybrid", "country": "Lithuania"},
                ],
            )
            # The form now reflects the saved selection on reload.
            page = client.get("/profile").text
            self.assertIn("Lithuania", page)

    def test_office_unchecked_clears_that_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "profile.json"
            self._write_profile(profile_path)
            client = _client(tmp)
            client.app.state.profile_path = str(profile_path)
            token = _csrf(client, "/profile")
            # Only office selected.
            client.post(
                "/profile/work-modes",
                headers=HEADERS,
                data={"csrf_token": token, "office": "on", "office_country": "Lithuania"},
                follow_redirects=False,
            )
            saved = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["work_modes"], [{"mode": "office", "country": "Lithuania"}])


if __name__ == "__main__":
    unittest.main()
