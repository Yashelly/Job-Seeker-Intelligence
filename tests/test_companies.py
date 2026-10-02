from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from cvbankas_tracker.companies import MAX_REGISTRY_BYTES, CompanyRegistry, detect_ats, read_registry
from cvbankas_tracker.storage import DatabaseManager, bootstrap_database


def company(name: str = "Example", **fields) -> dict:
    return {"name": name, "career_url": "https://jobs.ashbyhq.com/example", **fields}


class CompanyRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = DatabaseManager(Path(self.directory.name) / "registry.db")
        self.database.initialize(create_backup=False)
        self.registry = CompanyRegistry(self.database)

    def test_add_detects_ats_and_searches_aliases(self) -> None:
        item = self.registry.save(company(aliases=["Original Brand"], collection_enabled=True))
        self.assertEqual((item["ats_type"], item["ats_token"]), ("ashby", "example"))
        self.assertEqual(len(self.registry.list_companies(query="original brand", pool="LT")), 1)
        self.assertEqual(self.registry.list_companies(pool="EU-watch"), [])
        self.assertIsNone(item["last_scan_status"])

    def test_duplicate_names_and_ids_are_rejected(self) -> None:
        self.registry.save(company())
        for payload in (company("  EXAMPLE ", company_id="other"), company("Other", company_id="example")):
            with self.assertRaisesRegex(ValueError, "already exists"):
                self.registry.save(payload)

    def test_rename_cannot_take_another_company_name(self) -> None:
        self.registry.save(company())
        self.registry.save(company("Other"))
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.registry.save({"name": "Other"}, company_id="example")

    def test_company_id_is_immutable(self) -> None:
        self.registry.save(company())
        with self.assertRaisesRegex(ValueError, "cannot be changed"):
            self.registry.save({"company_id": "renamed"}, company_id="example")

    def test_import_is_atomic_and_reimport_preserves_edits(self) -> None:
        data = json.dumps([company(evidence_ref="literal-source-id"), company("Other")]).encode()
        self.assertEqual(self.registry.import_bytes(data, "companies.json"), {"inserted": 2, "skipped": 0})
        self.registry.save({"notes": "My manual note", "collection_enabled": False}, company_id="example")
        self.assertEqual(self.registry.import_bytes(data, "companies.json"), {"inserted": 0, "skipped": 2})
        self.assertEqual(self.registry.get("example")["notes"], "My manual note")
        self.assertEqual(self.registry.get("example")["evidence_ref"], "literal-source-id")
        with self.assertRaises(ValueError):
            self.registry.import_bytes(json.dumps([company("New"), {"name": ""}]).encode(), "companies.json")
        self.assertIsNone(self.registry.get("new"))

    def test_export_can_be_imported_without_claiming_local_scan(self) -> None:
        self.registry.save(company())
        self.registry.record_scan("example", "completed", jobs_count=0)
        payload = self.registry.export()
        self.assertEqual(payload[0]["last_scan_jobs_count"], 0)
        other_db = DatabaseManager(Path(self.directory.name) / "other.db")
        other_db.initialize(create_backup=False)
        other = CompanyRegistry(other_db)
        other.import_bytes(json.dumps(payload).encode(), "companies.json")
        self.assertIsNone(other.get("example")["last_scan_jobs_count"])

    def test_source_change_resets_scan_and_uses_new_board(self) -> None:
        self.registry.save(company(ats_url="https://jobs.ashbyhq.com/example", collection_enabled=True))
        self.registry.record_scan("example", "completed", jobs_count=4)
        item = self.registry.save({"career_url": "https://jobs.lever.co/other"}, company_id="example")
        self.assertEqual((item["ats_type"], item["ats_token"]), ("lever", "other"))
        self.assertIsNone(item["last_scan_at"])
        self.assertEqual(item["check_status"], "unverified")

    def test_failed_scan_does_not_become_zero_jobs(self) -> None:
        self.registry.save(company())
        self.registry.record_scan("example", "failed", error="Timeout")
        item = self.registry.get("example")
        self.assertIsNone(item["last_scan_jobs_count"])
        self.assertEqual(item["last_scan_error"], "Timeout")

    def test_saving_blank_optional_urls_preserves_review_and_scan(self) -> None:
        self.registry.save(company(checked_on="2026-10-01", check_status="html_readable"))
        self.registry.record_scan("example", "completed", jobs_count=4)
        item = self.registry.save({"ats_url": "", "ats_token": "example"}, company_id="example")
        self.assertEqual(item["last_scan_jobs_count"], 4)
        self.assertEqual(item["checked_on"], "2026-10-01")

    def test_editing_ats_link_does_not_reuse_old_auto_detected_fields(self) -> None:
        old_url = "https://jobs.ashbyhq.com/example"
        self.registry.save(company(ats_url=old_url, collection_enabled=True))
        item = self.registry.save({
            "career_url": "https://jobs.lever.co/other", "ats_url": old_url,
            "ats_type": "ashby", "ats_token": "example",
        }, company_id="example")
        self.assertEqual((item["ats_type"], item["ats_token"]), ("lever", "other"))

    def test_zip_reads_only_registry_data(self) -> None:
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("career_registry/companies.json", json.dumps([company()]))
            archive.writestr("career_registry/scanner.py", "raise RuntimeError('do not execute')")
            archive.writestr("../../outside.txt", "do not extract")
            archive.writestr("career_registry/applications.private.json", "private application")
        self.assertEqual(self.registry.import_bytes(data.getvalue(), "registry.zip")["inserted"], 1)
        with self.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 0)

    def test_zip_rejects_duplicate_registry_and_large_uncompressed_data(self) -> None:
        for oversized in (False, True):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("companies.json", " " * (MAX_REGISTRY_BYTES + 1) if oversized else "[]")
                if not oversized:
                    archive.writestr("career_registry/companies.json", "[]")
            with self.assertRaises(ValueError):
                read_registry(data.getvalue(), "registry.zip")

    def test_unsafe_urls_and_ats_tokens_are_rejected(self) -> None:
        for url in ("javascript:alert(1)", "https://user:password@example.com", "https://example.com:invalid"):
            with self.assertRaises(ValueError):
                self.registry.save(company(career_url=url))
        with self.assertRaises(ValueError):
            self.registry.save(company(ats_token="../../other"))
        saved = self.registry.save(company(career_url="https://example.com/careers", collection_enabled=True))
        self.assertEqual(saved["ats_type"], "html")
        self.assertEqual(detect_ats("https://jobs.lever.co.evil.test/example"), ("unknown", None))

    def test_migration_backs_up_existing_database_before_adding_registry(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DROP TABLE career_companies")
            connection.execute("INSERT INTO settings VALUES ('sentinel', '\"keep\"')")
        result = bootstrap_database(self.database.db_path)
        self.assertTrue(result.migrated)
        self.assertIsNotNone(result.backup_path)
        self.assertTrue(result.backup_path.exists())
        with self.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT value_json FROM settings WHERE key='sentinel'").fetchone()[0], '"keep"')
        second = bootstrap_database(self.database.db_path)
        self.assertFalse(second.migrated)
        self.assertIsNone(second.backup_path)


if __name__ == "__main__":
    unittest.main()
