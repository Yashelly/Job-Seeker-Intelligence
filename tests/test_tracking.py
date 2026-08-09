from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.models import ApplicationRecord, ApplicationStatus, Vacancy
from cvbankas_tracker.storage import DatabaseManager
from cvbankas_tracker.tracking import ApplicationTracker


class ApplicationRecordTests(unittest.TestCase):
    def test_valid_status_transition(self) -> None:
        record = ApplicationRecord("https://www.cvbankas.lt/job/1")
        record.update_status(ApplicationStatus.APPLIED)
        self.assertEqual(record.status, ApplicationStatus.APPLIED)

    def test_invalid_status_transition_raises(self) -> None:
        record = ApplicationRecord("https://www.cvbankas.lt/job/1")
        with self.assertRaises(ValueError):
            record.update_status(ApplicationStatus.OFFER)

    def test_tracker_requires_reason_for_corrective_status_reassignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = DatabaseManager(Path(tmp_dir) / "tracking.db")
            database.initialize()
            vacancy = Vacancy(
                source_name="sample",
                source_id="1",
                source_url="https://example.com/job/1",
                title="Automation Specialist",
                company="Example",
                location="Remote",
                salary_text="",
            )
            database.save_vacancy(vacancy)

            tracker = ApplicationTracker(database)
            tracker.ensure_record(vacancy.source_url)
            tracker.update_status(vacancy.source_url, ApplicationStatus.APPLIED)
            tracker.update_status(vacancy.source_url, ApplicationStatus.REJECTED)
            with self.assertRaises(ValueError):
                tracker.set_status(vacancy.source_url, ApplicationStatus.SAVED)
            tracker.set_status(
                vacancy.source_url,
                ApplicationStatus.SAVED,
                reason="Correcting an accidental rejected mark.",
            )
            record = database.get_application_record(vacancy.source_url)
            database.close()

        self.assertEqual(record.status, ApplicationStatus.SAVED)


if __name__ == "__main__":
    unittest.main()
