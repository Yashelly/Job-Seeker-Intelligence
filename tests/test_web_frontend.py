from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

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

        def target() -> int:
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

        def boom() -> int:
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

        def slow() -> int:
            started.set()
            time.sleep(0.3)
            return 0

        job = manager.start("search", slow)
        started.wait()
        with self.assertRaises(JobConflictError):
            manager.start("search", lambda: 0)
        # let the first finish
        for _ in range(100):
            if manager.get(job.id).status != "running":
                break
            time.sleep(0.02)


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

        def fake_run_batch(args, cfg=None) -> int:
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

            save_path = Path(tmp) / "out_profile.json"
            token = _csrf(client, "/profile")
            import json as _json

            resp = client.post(
                "/profile/save",
                data={
                    "csrf_token": token,
                    "profile_json": _json.dumps(GENERATED),
                    "save_path": str(save_path),
                },
                headers=HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
            self.assertTrue(save_path.exists())

            from cvbankas_tracker.io_utils import ProfileFileReader

            loaded = ProfileFileReader().read(save_path)
            self.assertEqual(loaded.name, "Jane Doe")

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


if __name__ == "__main__":
    unittest.main()
