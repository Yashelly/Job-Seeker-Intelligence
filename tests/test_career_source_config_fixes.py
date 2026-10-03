import tempfile
import unittest
from pathlib import Path

from cvbankas_tracker.companies import CompanyRegistry
from cvbankas_tracker.storage import DatabaseManager
from scripts.apply_career_source_fixes import SOURCE_FIXES, apply_source_fixes


class CareerSourceConfigFixesTests(unittest.TestCase):
    def test_source_fixes_preserve_owner_settings_skip_missing_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(Path(directory) / "registry.db")
            database.initialize(create_backup=False)
            registry = CompanyRegistry(database)
            registry.save({
                "company_id": "retool",
                "name": "Retool",
                "career_url": "https://retool.com/old-careers",
                "ats_type": "html",
                "ats_url": "https://retool.com/old-careers",
                "collection_enabled": False,
                "notes": "Owner-managed note",
            })
            registry.save({"company_id": "owner-example", "name": "Owner Example", "notes": "Preserve this record"})
            unrelated = registry.get("owner-example")
            updated, missing = apply_source_fixes(registry)
            self.assertEqual(updated, ["retool"])
            self.assertEqual(set(missing), set(SOURCE_FIXES) - {"retool"})
            company = registry.get("retool")
            self.assertEqual(company["ats_url"], "https://retool.com/careers")
            self.assertEqual(company["ats_type"], "html")
            self.assertFalse(company["collection_enabled"])
            self.assertEqual(company["notes"], "Owner-managed note")
            self.assertEqual(registry.get("owner-example"), unrelated)
            self.assertEqual(len(registry.list_companies()), 2)
            self.assertEqual(apply_source_fixes(registry), ([], missing))
            self.assertEqual(registry.get("retool"), company)


if __name__ == "__main__":
    unittest.main()
