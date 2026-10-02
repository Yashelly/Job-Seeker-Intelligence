from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.companies import CompanyRegistry
from cvbankas_tracker.storage import DatabaseManager, bootstrap_database


def _legacy_company_payload() -> str:
    return json.dumps(
        {
            "company_id": "example",
            "name": "Example",
            "aliases": [],
            "pool": "LT",
            "priority": 2,
            "notes": "Keep this metadata",
            "check_status": "verified",
            "remote_eligibility": "check_each_job",
            "checked_on": None,
            "career_url": "https://jobs.ashbyhq.com/example",
            "source_url": None,
            "ats_url": None,
            "fallback_url": None,
            "evidence_url": None,
            "api_url": None,
            "greenhouse_api_url": None,
            "ats_type": "ashby",
            "ats_token": "example",
            "collection_enabled": True,
        }
    )


class CareerCompanySchemaMigrationTests(unittest.TestCase):
    def test_current_career_company_schema_does_not_force_named_identity_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "current_registry.db"
            database = DatabaseManager(db_path)
            first = database.initialize(create_backup=False)
            second = bootstrap_database(db_path)
            with database.connection() as connection:
                indexes = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }

        self.assertTrue(first.migrated)
        self.assertFalse(second.migrated)
        self.assertIsNone(second.backup_path)
        self.assertNotIn("idx_career_companies_company_id_unique", indexes)
        self.assertNotIn("idx_career_companies_name_key_unique", indexes)

    def test_bootstrap_repairs_legacy_career_company_scan_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "legacy_registry.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE career_companies (
                    company_id TEXT,
                    name_key TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO career_companies
                    (company_id, name_key, data_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "example",
                    "example",
                    _legacy_company_payload(),
                    "2026-09-30T12:00:00Z",
                    "2026-09-30T12:00:00Z",
                ),
            )
            connection.commit()
            connection.close()

            result = bootstrap_database(db_path)
            backup_exists = result.backup_path is not None and result.backup_path.exists()
            database = DatabaseManager(db_path)
            registry = CompanyRegistry(database)
            before_scan = registry.get("example")
            registry.record_scan("example", "completed", jobs_count=3)
            after_scan = registry.get("example")

            with database.connection() as connection:
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(career_companies)")
                }
                indexes = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
                stored = connection.execute(
                    "SELECT created_at, updated_at, data_json FROM career_companies WHERE company_id = ?",
                    ("example",),
                ).fetchone()

        self.assertTrue(result.migrated)
        self.assertIsNotNone(result.backup_path)
        self.assertTrue(backup_exists)
        self.assertEqual(before_scan["notes"], "Keep this metadata")
        self.assertIsNone(before_scan["last_scan_at"])
        self.assertIsNone(before_scan["last_scan_status"])
        self.assertEqual(before_scan["last_scan_error"], "")
        self.assertIsNone(before_scan["last_scan_jobs_count"])
        self.assertEqual(after_scan["last_scan_status"], "completed")
        self.assertEqual(after_scan["last_scan_jobs_count"], 3)
        self.assertIn("last_scan_at", columns)
        self.assertIn("last_scan_status", columns)
        self.assertIn("last_scan_error", columns)
        self.assertIn("last_scan_jobs_count", columns)
        self.assertIn("idx_career_companies_company_id_unique", indexes)
        self.assertIn("idx_career_companies_name_key_unique", indexes)
        self.assertEqual(stored["created_at"], "2026-09-30T12:00:00Z")
        self.assertEqual(stored["updated_at"], "2026-09-30T12:00:00Z")
        self.assertEqual(json.loads(stored["data_json"])["notes"], "Keep this metadata")

    def test_repaired_career_company_identity_indexes_reject_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "legacy_registry.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE career_companies (
                    company_id TEXT,
                    name_key TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO career_companies
                    (company_id, name_key, data_json, created_at, updated_at)
                VALUES
                    ('example', 'example', '{}', '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z');
                """
            )
            connection.commit()
            connection.close()

            bootstrap_database(db_path)
            connection = sqlite3.connect(db_path)
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO career_companies
                            (company_id, name_key, data_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("other", "example", "{}", "2026-09-30T12:00:00Z", "2026-09-30T12:00:00Z"),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO career_companies
                            (company_id, name_key, data_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("example", "other", "{}", "2026-09-30T12:00:00Z", "2026-09-30T12:00:00Z"),
                    )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
