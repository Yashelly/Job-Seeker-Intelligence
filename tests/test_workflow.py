from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["OPENAI_API_KEY"] = " "

    def test_sample_cli_runs_end_to_end(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "workflow_demo.db"
            export_path = Path(tmp_dir) / "workflow_report.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--source",
                    "sample",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertIn("Vacancy batch finished.", completed.stdout)
        self.assertIn("saved=2", completed.stdout)

    def test_module_cli_accepts_yaml_config(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "settings.yaml"
            db_path = Path(tmp_dir) / "config.db"
            export_path = Path(tmp_dir) / "config_report.md"
            config_path.write_text(
                "\n".join(
                    [
                        "profile: sample_data/active_profile.json",
                        f"db: {db_path.as_posix()}",
                        f"export: {export_path.as_posix()}",
                        "analysis_strategy: ai",
                        "sources:",
                        "  enabled:",
                        "    - sample",
                        "  keywords:",
                        "    sample:",
                        "      - python",
                        "      - automation",
                        "search:",
                        "  keywords:",
                        "    - fallback",
                        "  limit: 2",
                        "  max_pages: 1",
                    ]
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, "-m", "src.main", "--config", str(config_path)],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertIn("Vacancy batch finished.", completed.stdout)
        self.assertIn("keywords=2", completed.stdout)
        self.assertIn("saved=2", completed.stdout)

    def test_root_main_uses_default_config(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "root_main.db"
            export_path = Path(tmp_dir) / "root_main_report.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--source",
                    "sample",
                    "--limit",
                    "2",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertIn("Vacancy batch finished.", completed.stdout)
        self.assertIn("saved=2", completed.stdout)

    def test_root_main_without_arguments_opens_tui(self) -> None:
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "main.py"],
            cwd=root,
            input="q\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

        self.assertIn("Job Seeker TUI", completed.stdout)
        self.assertIn("Start search", completed.stdout)

    def test_rerun_skips_already_processed_vacancies_by_default(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "rerun.db"
            export_path = Path(tmp_dir) / "rerun_report.md"

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.main",
                    "--source",
                    "sample",
                    "--limit",
                    "2",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.main",
                    "--source",
                    "sample",
                    "--limit",
                    "2",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertIn("SKIP | already processed", completed.stdout)
        self.assertTrue(completed.returncode in (0, 2))

    def test_module_cli_runs_on_sample_data(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "module.db"
            export_path = Path(tmp_dir) / "module_report.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.main",
                    "--source",
                    "sample",
                    "--limit",
                    "2",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertIn("Vacancy batch finished.", completed.stdout)
        self.assertIn("saved=2", completed.stdout)

    def test_cli_imports_multiple_pasted_urls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        first_url = "https://www.cvbankas.lt/python-backend-developer-vilniuje/1-1234567"
        second_url = "https://www.cvbankas.lt/data-analyst-kaune/1-7654321"
        pasted_urls = f"{first_url},\n{second_url} {first_url}"

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "import_urls.db"
            export_path = Path(tmp_dir) / "import_urls_report.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--sources",
                    "sample",
                    "--import-urls",
                    pasted_urls,
                    "--limit",
                    "10",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertIn("URL import finished.", completed.stdout)
        self.assertIn("processed=2", completed.stdout)
        self.assertIn("saved=2", completed.stdout)

    def test_cli_accepts_hh_source_name(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "hh_empty.db"
            export_path = Path(tmp_dir) / "hh_empty_report.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--sources",
                    "hh",
                    "--import-urls",
                    "https://example.com/not-hh",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertIn("No enabled source can handle URL", completed.stdout)

    def test_cli_lists_saved_vacancies_with_scores(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "list.db"
            export_path = Path(tmp_dir) / "list_report.md"

            subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--source",
                    "sample",
                    "--limit",
                    "2",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--db",
                    str(db_path),
                    "--list-vacancies",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertIn("Saved Vacancies", completed.stdout)
        self.assertIn("Python Backend Developer", completed.stdout)
        self.assertIn("Score     :", completed.stdout)

    def test_cli_updates_tracking_status_and_lists_tracked_applications(self) -> None:
        root = Path(__file__).resolve().parents[1]
        vacancy_url = "https://www.cvbankas.lt/python-backend-developer-vilniuje/1-1234567"
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "tracking.db"
            export_path = Path(tmp_dir) / "tracking_report.md"

            subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--source",
                    "sample",
                    "--limit",
                    "2",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            update_completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--db",
                    str(db_path),
                    "--vacancy-url",
                    vacancy_url,
                    "--status",
                    "interview",
                    "--note",
                    "Interview invited.",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            list_completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--db",
                    str(db_path),
                    "--list-tracked",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertIn("Application Updated", update_completed.stdout)
        self.assertIn("Status     : Interview", update_completed.stdout)
        self.assertIn("Tracked Applications", list_completed.stdout)
        self.assertIn("Interview invited.", list_completed.stdout)
        self.assertIn("status=Interview", list_completed.stdout)

    def test_cli_can_show_one_vacancy_with_raw_preview(self) -> None:
        root = Path(__file__).resolve().parents[1]
        vacancy_url = "https://www.cvbankas.lt/python-backend-developer-vilniuje/1-1234567"
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "show.db"
            export_path = Path(tmp_dir) / "show_report.md"

            subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--source",
                    "sample",
                    "--limit",
                    "2",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--db",
                    str(db_path),
                    "--vacancy-url",
                    vacancy_url,
                    "--show-vacancy",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertIn("Vacancy Details", completed.stdout)
        self.assertIn("Source ID  : 1-1234567", completed.stdout)
        self.assertIn("Raw Preview", completed.stdout)

    def test_cli_can_update_by_source_id_and_export_tracked_report(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "track_by_id.db"
            export_path = Path(tmp_dir) / "track_by_id_report.md"
            tracked_export = Path(tmp_dir) / "tracked.md"

            subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--source",
                    "sample",
                    "--limit",
                    "2",
                    "--db",
                    str(db_path),
                    "--export",
                    str(export_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            update_completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--db",
                    str(db_path),
                    "--vacancy-id",
                    "1-1234567",
                    "--status",
                    "interview",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            export_completed = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--db",
                    str(db_path),
                    "--export-tracked",
                    str(tracked_export),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertIn("Source ID  : 1-1234567", update_completed.stdout)
            self.assertIn("Status     : Interview", update_completed.stdout)
            self.assertIn("Tracked applications report written to:", export_completed.stdout)
            self.assertTrue(tracked_export.exists())
            self.assertIn("Python Backend Developer", tracked_export.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
