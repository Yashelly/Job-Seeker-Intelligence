"""Offline demo mode: a reviewer must be able to seed real data with no keys.

The test runs in an isolated working directory (a copy of the bundled
fixtures) so it never touches the developer's real config, database, or the
repo's demo_data/ scratch folder.
"""

import argparse
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.main import run_demo
from cvbankas_tracker.storage import DatabaseManager

_REPO = Path(__file__).resolve().parents[1]


def _demo_args(web: bool = False) -> argparse.Namespace:
    return argparse.Namespace(web=web, web_host="127.0.0.1", web_port=8000)


class DemoModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_cwd = Path.cwd()
        self._tmp = Path(self.enterContext(_temp_dir()))
        shutil.copytree(_REPO / "sample_data", self._tmp / "sample_data")
        os.chdir(self._tmp)

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)

    def test_demo_seeds_database_with_no_credentials(self) -> None:
        # Explicitly strip any AI configuration to prove zero-config operation.
        for key in ("AI_BACKEND", "OPENAI_API_KEY"):
            os.environ.pop(key, None)

        exit_code = run_demo(_demo_args())
        self.assertEqual(exit_code, 0)

        db_path = self._tmp / "demo_data" / "demo.db"
        self.assertTrue(db_path.exists())
        vacancies = DatabaseManager(db_path).list_vacancies_with_latest_scores()
        self.assertGreaterEqual(len(vacancies), 2)

    def test_demo_is_idempotent(self) -> None:
        self.assertEqual(run_demo(_demo_args()), 0)
        self.assertEqual(run_demo(_demo_args()), 0)

    def test_demo_requires_fixtures(self) -> None:
        shutil.rmtree(self._tmp / "sample_data")
        self.assertEqual(run_demo(_demo_args()), 1)


def _temp_dir():
    import tempfile

    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
