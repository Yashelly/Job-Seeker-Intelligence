import os
import tempfile
import unittest

from src.storage import get_run, init_db, save_run


class StorageTests(unittest.TestCase):
    def test_insert_and_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            init_db(db_path)
            run_id = save_run(
                db_path=db_path,
                source_type="text",
                source_ref="inline",
                job={
                    "company": "Example",
                    "role_title": "Automation Specialist",
                    "location": "Vilnius",
                    "work_mode": "hybrid",
                    "salary": {"min": 1000, "max": 2000, "currency": "EUR", "gross_or_net": "gross"},
                },
                score_result={"score": 70, "decision": "stretch"},
                summary="summary",
                cover_letter="letter",
            )
            row = get_run(db_path, run_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["company"], "Example")
            self.assertEqual(row["decision"], "stretch")


if __name__ == "__main__":
    unittest.main()
