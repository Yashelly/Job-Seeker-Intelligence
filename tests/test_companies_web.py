from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from cvbankas_tracker.companies import CompanyRegistry
from cvbankas_tracker.storage import DatabaseManager
from cvbankas_tracker.web import create_app

BASE = "http://127.0.0.1"
HEADERS = {"origin": BASE}


def _client(tmp_dir: str) -> TestClient:
    app = create_app(Path(tmp_dir) / "web.db", profile_path="sample_data/active_profile.json")
    return TestClient(app, base_url=BASE)


def _csrf(client: TestClient, page: str = "/companies") -> str:
    client.get(page)
    return client.cookies.get("job_seeker_csrf")


def _zip_registry(companies: list[dict]) -> bytes:
    handle = io.BytesIO()
    with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("career_registry/companies.json", json.dumps(companies))
    return handle.getvalue()


def _company_payload(**overrides) -> dict:
    payload = {
        "name": "Acme AI",
        "company_id": "acme-ai",
        "pool": "LT",
        "priority": 1,
        "career_url": "https://jobs.ashbyhq.com/acme",
        "source_url": "https://example.test/source",
        "ats_url": "",
        "ats_type": "",
        "ats_token": "",
        "aliases": ["Acme"],
        "notes": "Target company",
        "check_status": "verified",
        "checked_on": "2026-10-01",
        "remote_eligibility": "remote_first",
        "collection_enabled": True,
    }
    payload.update(overrides)
    return payload


def _wait_done(client: TestClient, job_id: int) -> dict:
    deadline = time.time() + 5
    snapshot: dict = {}
    while time.time() < deadline:
        snapshot = client.get(f"/jobs/{job_id}/log").json()
        if snapshot["status"] != "running":
            return snapshot
        time.sleep(0.05)
    return snapshot


class CompaniesWebTests(unittest.TestCase):
    def test_zip_import_is_idempotent_and_preserves_existing_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client)
            archive = _zip_registry([_company_payload()])

            resp = client.post(
                "/companies/import",
                data={"csrf_token": token},
                files={"company_file": ("career_registry.zip", archive, "application/zip")},
                headers=HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
            self.assertIn("Imported+1+company", resp.headers["location"])

            registry = CompanyRegistry(DatabaseManager(Path(tmp) / "web.db"))
            registry.save({"notes": "Manual edit"}, company_id="acme-ai")

            token = _csrf(client)
            resp = client.post(
                "/companies/import",
                data={"csrf_token": token},
                files={"company_file": ("career_registry.zip", archive, "application/zip")},
                headers=HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
            self.assertIn("skipped+1+existing", resp.headers["location"])
            self.assertEqual(registry.get("acme-ai")["notes"], "Manual edit")

    def test_create_edit_and_export_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/companies/new")
            resp = client.post(
                "/companies/save",
                data={
                    "csrf_token": token,
                    "name": "Manual Co",
                    "company_id": "manual-co",
                    "pool": "EU",
                    "priority": "2",
                    "aliases": "Manual\nManual Labs",
                    "remote_eligibility": "custom_remote_policy",
                    "notes": "Needs custom parser",
                    "check_status": "unverified",
                },
                headers=HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
            self.assertEqual(resp.headers["location"], "/companies/manual-co/edit")
            self.assertIn("custom_remote_policy", client.get("/companies/manual-co/edit").text)

            token = _csrf(client, "/companies/manual-co/edit")
            resp = client.post(
                "/companies/save",
                data={
                    "csrf_token": token,
                    "_original_company_id": "manual-co",
                    "name": "Manual Co",
                    "company_id": "manual-co",
                    "career_url": "https://manual.example.test/jobs",
                    "pool": "EU",
                    "priority": "3",
                    "aliases": "Manual",
                    "remote_eligibility": "hybrid",
                    "notes": "Updated",
                    "check_status": "unverified",
                },
                headers=HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)

            exported = client.get("/companies/export")
            self.assertEqual(exported.status_code, 200)
            self.assertIn("career-companies.json", exported.headers["content-disposition"])
            rows = exported.json()
            self.assertEqual(rows[0]["name"], "Manual Co")
            self.assertEqual(rows[0]["priority"], 3)
            self.assertEqual(rows[0]["notes"], "Updated")
            self.assertEqual(rows[0]["remote_eligibility"], "hybrid")

    def test_companies_page_escapes_company_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "web.db"
            app = create_app(db_path, profile_path="sample_data/active_profile.json")
            CompanyRegistry(DatabaseManager(db_path)).save(
                _company_payload(
                    name="<script>alert(1)</script>",
                    company_id="script-co",
                    career_url="https://example.test/careers",
                    collection_enabled=False,
                    aliases=["<b>Alias</b>"],
                )
            )
            client = TestClient(app, base_url=BASE)
            resp = client.get("/companies")
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("<script>alert(1)</script>", resp.text)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", resp.text)
            self.assertIn("&lt;b&gt;Alias&lt;/b&gt;", resp.text)

    def test_company_mutations_require_csrf_and_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/companies/new")
            resp = client.post(
                "/companies/save",
                data={"csrf_token": token, "name": "No Origin", "career_url": "https://example.test/jobs"},
            )
            self.assertEqual(resp.status_code, 403)

            resp = client.post(
                "/companies/save",
                data={"csrf_token": "wrong", "name": "Bad Token", "career_url": "https://example.test/jobs"},
                headers=HEADERS,
            )
            self.assertEqual(resp.status_code, 403)

    def test_collect_careers_uses_common_job_runner_with_full_collection_defaults(self) -> None:
        captured: dict = {}

        def fake_run_batch(args, cfg=None, control=None) -> int:
            captured["sources"] = args.sources
            captured["keywords"] = args.keywords
            captured["daily_run"] = args.daily_run
            captured["infinite"] = args.infinite
            captured["limit"] = args.limit
            captured["max_pages"] = args.max_pages
            captured["prune_threshold"] = args.prune_threshold
            print("careers done")
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            token = _csrf(client, "/companies")
            with patch("cvbankas_tracker.web.run_batch", fake_run_batch):
                resp = client.post(
                    "/companies/collect",
                    data={"csrf_token": token},
                    headers=HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)
                job_id = int(resp.headers["location"].rsplit("/", 1)[-1])
                snap = _wait_done(client, job_id)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(captured["sources"], "careers")
        self.assertTrue(captured["keywords"])
        self.assertFalse(captured["daily_run"])
        self.assertTrue(captured["infinite"])
        self.assertEqual(captured["limit"], 5000)
        self.assertEqual(captured["max_pages"], 100)
        self.assertIsNone(captured["prune_threshold"])


if __name__ == "__main__":
    unittest.main()
