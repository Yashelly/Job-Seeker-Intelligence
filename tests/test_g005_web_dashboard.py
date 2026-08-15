from __future__ import annotations

from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from cvbankas_tracker.models import (
    AnalysisMethod,
    ApplicationStatus,
    FitLabel,
    InboxPreferences,
    Vacancy,
    VacancyAnalysis,
)
from cvbankas_tracker.storage import DatabaseManager
from cvbankas_tracker.tracking import ActionService
from cvbankas_tracker.web import create_app, run_web, validate_loopback_bind_host

BASE = "http://127.0.0.1"
HEADERS = {"origin": BASE}


def seed_database(db_path: Path, *, hostile: bool = False) -> str:
    database = DatabaseManager(db_path)
    database.initialize(create_backup=False)
    run = database.begin_collection_run()
    url = "https://example.test/web-job"
    vacancy = Vacancy(
        source_name="sample",
        source_id="web-job",
        source_url=url,
        title='<script>alert("x")</script> Engineer' if hostile else "Web Automation Engineer",
        company="Example & Co",
        location="Remote",
        salary_text="",
        requirements=["Python", "FastAPI"],
        responsibilities=["Build dashboards"],
        raw_text='<img src=x onerror=alert(1)>' if hostile else "Plain description",
    )
    database.save_vacancy(vacancy, collection_run_id=run.id)
    database.save_analysis(
        VacancyAnalysis(
            vacancy_source_url=url,
            analysis_method=AnalysisMethod.RULE_BASED,
            score=86,
            fit_label=FitLabel.HIGH,
            explanation='Strong <b>server</b> fit.' if hostile else "Strong server fit.",
            matched_points=("Python", "FastAPI"),
            missing_points=("None",),
        )
    )
    database.finish_collection_run(run.id, status="completed")
    database.save_inbox_preferences(
        InboxPreferences(minimum_score=80, hide_below_threshold=True, sort_by="score")
    )
    database.close()
    return url


class G005WebDashboardTests(unittest.TestCase):
    def test_pages_render_accessible_shared_dashboard_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "web_pages.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            ActionService(database).create_action(vacancy_source_url=vacancy_url, title="Follow up")
            database.close()

            with TestClient(create_app(db_path), base_url=BASE) as client:
                for path, heading in [
                    ("/today", "Today"),
                    ("/vacancies", "Vacancies"),
                    (f"/vacancy?url={vacancy_url}", "Web Automation Engineer"),
                    ("/applications", "Applications"),
                    ("/settings", "Settings"),
                ]:
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, path)
                    self.assertIn(f">{heading}</h1>", response.text)
                self.assertIn("Strong server fit.", client.get("/vacancies").text)
                self.assertIn("Follow up", client.get("/actions").text)

    def test_settings_post_prg_persists_preferences_visible_to_shared_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "web_settings.db"
            seed_database(db_path)
            with TestClient(create_app(db_path), base_url=BASE) as client:
                token = client.get("/settings").cookies["job_seeker_csrf"]
                response = client.post(
                    "/settings",
                    headers=HEADERS,
                    data={
                        "csrf_token": token,
                        "minimum_score": "42",
                        "sort_by": "title",
                        "hide_below_threshold": "1",
                        "source_name": "sample",
                        "fit_label": "High",
                        "application_status": "",
                        "new_only": "1",
                        "current_run_only": "",
                        "timezone": "Europe/Vilnius",
                    },
                    follow_redirects=False,
                )
            database = DatabaseManager(db_path)
            preferences = database.get_inbox_preferences()
            timezone = database.get_user_timezone()

        self.assertEqual(response.status_code, 303)
        self.assertEqual(preferences.minimum_score, 42)
        self.assertEqual(preferences.sort_by, "title")
        self.assertEqual(preferences.source_name, "sample")
        self.assertTrue(preferences.new_only)
        self.assertEqual(timezone, "Europe/Vilnius")

    def test_web_actions_and_application_status_use_shared_services_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "web_mutations.db"
            vacancy_url = seed_database(db_path)
            with TestClient(create_app(db_path), base_url=BASE) as client:
                token = client.get("/actions").cookies["job_seeker_csrf"]
                created = client.post(
                    "/actions/create",
                    headers=HEADERS,
                    data={
                        "csrf_token": token,
                        "vacancy_source_url": vacancy_url,
                        "title": "Prepare notes",
                        "due_at": "2026-08-08T10:00:00",
                        "notes": "Use browser UI",
                    },
                    follow_redirects=False,
                )
                status_response = client.post(
                    "/applications/status",
                    headers=HEADERS,
                    data={"csrf_token": token, "vacancy_source_url": vacancy_url, "status": "applied"},
                    follow_redirects=False,
                )
            database = DatabaseManager(db_path)
            actions = database.list_action_items()
            events = database.list_application_status_events(vacancy_url)
            record = database.get_application_record(vacancy_url)

        self.assertEqual(created.status_code, 303)
        self.assertEqual(status_response.status_code, 303)
        self.assertIn("url=https%3A%2F%2Fexample.test%2Fweb-job", status_response.headers["location"])
        self.assertEqual(actions[0].title, "Prepare notes")
        self.assertEqual(record.status, ApplicationStatus.APPLIED)
        self.assertEqual(events[-1].origin.value, "web")

    def test_unsafe_posts_require_loopback_host_origin_and_csrf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "web_security.db"
            seed_database(db_path)
            with TestClient(create_app(db_path), base_url=BASE) as client:
                token = client.get("/settings").cookies["job_seeker_csrf"]
                good = client.post("/settings", headers=HEADERS, data={"csrf_token": token, "minimum_score": "10", "sort_by": "score"}, follow_redirects=False)
                no_csrf = client.post("/settings", headers=HEADERS, data={"minimum_score": "10", "sort_by": "score"})
                bad_origin = client.post("/settings", headers={"origin": "http://evil.test"}, data={"csrf_token": token, "minimum_score": "10", "sort_by": "score"})
            with TestClient(create_app(db_path), base_url="http://evil.test") as hostile_client:
                bad_host = hostile_client.post("/settings", headers={"origin": "http://evil.test"}, data={"csrf_token": "x", "minimum_score": "10", "sort_by": "score"})

        self.assertEqual(good.status_code, 303)
        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(bad_origin.status_code, 403)
        self.assertEqual(bad_host.status_code, 400)

    def test_loopback_bind_validation_rejects_public_hosts_and_no_submit_route_exists(self) -> None:
        self.assertEqual(validate_loopback_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_loopback_bind_host("localhost"), "localhost")
        for host in ("0.0.0.0", "::", "192.168.1.20", "example.com"):
            with self.assertRaises(ValueError, msg=host):
                validate_loopback_bind_host(host)
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "routes.db"
            seed_database(db_path)
            routes = {route.path for route in create_app(db_path).routes}
        self.assertFalse(any("submit" in route.lower() for route in routes))


    def test_app_factory_uses_backup_gated_bootstrap_unless_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "bootstrap_gate.db"
            with patch("cvbankas_tracker.web.bootstrap_database") as bootstrap:
                create_app(db_path)
            bootstrap.assert_called_once_with(db_path.resolve())

            with patch("cvbankas_tracker.web.bootstrap_database") as bootstrap_disabled:
                create_app(db_path, bootstrap=False)
            bootstrap_disabled.assert_not_called()

    def test_run_web_rejects_public_bind_before_start_and_accepts_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch("uvicorn.run") as uvicorn_run:
            db_path = Path(tmp_dir) / "launch.db"
            DatabaseManager(db_path).initialize(create_backup=False)
            with self.assertRaises(ValueError):
                run_web(db_path, host="0.0.0.0", port=8765)
            uvicorn_run.assert_not_called()

            code = run_web(db_path, host="127.0.0.1", port=8765)
            self.assertEqual(code, 0)
            self.assertEqual(uvicorn_run.call_args.kwargs["host"], "127.0.0.1")
            self.assertEqual(uvicorn_run.call_args.kwargs["port"], 8765)

    def test_cli_web_bind_rejection_happens_before_uvicorn_start(self) -> None:
        from cvbankas_tracker.main import main

        with tempfile.TemporaryDirectory() as tmp_dir, patch("uvicorn.run") as uvicorn_run, patch.object(
            sys,
            "argv",
            [
                "job-seeker",
                "--web",
                "--web-host",
                "192.168.1.10",
                "--db",
                str(Path(tmp_dir) / "cli_web.db"),
            ],
        ):
            with self.assertRaises(ValueError):
                main()
            uvicorn_run.assert_not_called()


    def test_partial_run_and_local_due_are_visible_in_web_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "web_partial_local.db"
            vacancy_url = seed_database(db_path)
            database = DatabaseManager(db_path)
            actions = ActionService(database)
            actions.set_user_timezone("Europe/Vilnius")
            actions.create_action(
                vacancy_source_url=vacancy_url,
                title="Local due check",
                local_due_at="2026-08-08T10:00:00",
            )
            run = database.begin_collection_run()
            database.record_vacancy_observation(vacancy_url, collection_run_id=run.id)
            database.finish_collection_run(run.id, status="partial")
            database.close()

            with TestClient(create_app(db_path), base_url=BASE) as client:
                vacancies = client.get("/vacancies")
                actions_page = client.get("/actions")

        self.assertIn("PARTIAL RUN", vacancies.text)
        self.assertIn("2026-08-08T10:00:00+03:00 (Europe/Vilnius)", actions_page.text)

    def test_missing_vacancy_status_mutation_returns_safe_4xx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "web_missing_status.db"
            seed_database(db_path)
            with TestClient(create_app(db_path), base_url=BASE, raise_server_exceptions=False) as client:
                token = client.get("/applications").cookies["job_seeker_csrf"]
                response = client.post(
                    "/applications/status",
                    headers=HEADERS,
                    data={"csrf_token": token, "vacancy_source_url": "https://example.test/missing", "status": "applied"},
                )

        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(response.status_code, 500)

    def test_localhost_resolution_rejects_real_socket_public_address(self) -> None:
        public_info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 0))]
        with patch("cvbankas_tracker.web.socket.getaddrinfo", return_value=public_info):
            with self.assertRaises(ValueError):
                validate_loopback_bind_host("localhost")

    def test_hostile_vacancy_text_is_escaped_and_links_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "escaping.db"
            vacancy_url = seed_database(db_path, hostile=True)
            with TestClient(create_app(db_path), base_url=BASE) as client:
                response = client.get(f"/vacancy?url={vacancy_url}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;script&gt;alert", response.text)
        self.assertNotIn("<script>alert", response.text)
        self.assertNotIn("<img src=x", response.text)


if __name__ == "__main__":
    unittest.main()
