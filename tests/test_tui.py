import io
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.models import ApplicationStatus
from cvbankas_tracker.storage import resolve_database_path
from cvbankas_tracker.tui import (
    JobSeekerTui,
    LiveLogWriter,
    apply_tui_state_to_config,
    build_initial_state,
    build_runner_args,
    build_runner_config,
    clean_terminal_text,
    configure_terminal_encoding,
    parse_status_choice,
    save_tui_config,
)


class TuiHelperTests(unittest.TestCase):
    def test_builds_state_from_config_and_runner_args(self) -> None:
        args = Namespace(
            profile="sample_data/active_profile.json",
            db="fallback.db",
            export="fallback.md",
            openai_model="gpt-test",
            analysis_strategy="rule",
            enabled_sources=["sample"],
            limit=2,
            max_pages=1,
        )
        cfg = {
            "profile": "profile.json",
            "db": "jobs.db",
            "export": "jobs.md",
            "analysis_strategy": "ai",
            "openai_model": "gpt-4.1-mini",
            "sources": {"enabled": ["cvbankas", "hh"]},
            "search": {"keywords": ["AI automation"], "limit": 10, "max_pages": 3},
        }

        state = build_initial_state(args, cfg)
        runner_args = build_runner_args(state, import_urls="https://example.com/job")

        self.assertEqual(state.selected_preset, "Quick start")
        self.assertEqual(state.enabled_sources, ["cvbankas", "hh", "justjoin"])
        self.assertIn("dirbtinio intelekto", state.source_keywords["cvbankas"])
        self.assertEqual(state.limit, 10)
        self.assertEqual(runner_args.enabled_sources, ["cvbankas", "hh", "justjoin"])
        self.assertEqual(runner_args.import_urls, "https://example.com/job")

    def test_build_initial_state_preserves_resolved_absolute_db_across_cwds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_dir = Path(tmp_dir) / "config"
            other_dir = Path(tmp_dir) / "other"
            config_dir.mkdir()
            other_dir.mkdir()
            config_path = config_dir / "cvbankas.yaml"
            config_path.write_text("db: data/jobs.db\n", encoding="utf-8")
            resolved_db = resolve_database_path("data/jobs.db", config_path=config_path)
            args = Namespace(
                profile="sample_data/active_profile.json",
                db=str(resolved_db),
                export="fallback.md",
                openai_model="gpt-test",
                analysis_strategy="rule",
                enabled_sources=["sample"],
                limit=2,
                max_pages=1,
            )
            cfg = {"db": "data/jobs.db"}

            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(other_dir)
                state = build_initial_state(args, cfg)
            finally:
                os.chdir(original_cwd)

        self.assertEqual(Path(state.db), resolved_db)

    def test_custom_keywords_are_applied_per_source(self) -> None:
        args = Namespace(
            profile="profile.json",
            db="jobs.db",
            export="jobs.md",
            openai_model="gpt-test",
            analysis_strategy="rule",
            enabled_sources=["hh"],
            limit=2,
            max_pages=1,
        )
        state = build_initial_state(args, {})
        state.enabled_sources = ["hh", "justjoin"]
        state.search_keywords = ["n8n", "AI automation"]
        state.source_keywords = {}
        state.use_config_keywords = False

        run_cfg = build_runner_config({}, state)

        self.assertEqual(run_cfg["sources"]["keywords"]["hh"], ["n8n", "AI automation"])
        self.assertEqual(run_cfg["sources"]["keywords"]["justjoin"], ["n8n", "AI automation"])

    def test_quick_start_uses_source_specific_keywords(self) -> None:
        args = Namespace(
            profile="profile.json",
            db="jobs.db",
            export="jobs.md",
            openai_model="gpt-test",
            analysis_strategy="rule",
            enabled_sources=["sample"],
            limit=2,
            max_pages=1,
        )
        state = build_initial_state(args, {})
        run_cfg = build_runner_config({}, state)

        self.assertIn("dirbtinio intelekto", run_cfg["sources"]["keywords"]["cvbankas"])
        self.assertIn("специалист ИИ", run_cfg["sources"]["keywords"]["hh"])
        self.assertIn("AI automation", run_cfg["sources"]["keywords"]["justjoin"])

    def test_tui_state_is_applied_to_config_without_overwriting_source_keywords(self) -> None:
        args = Namespace(
            profile="profile.json",
            db="jobs.db",
            export="jobs.md",
            openai_model="gpt-test",
            analysis_strategy="rule",
            enabled_sources=["sample"],
            limit=2,
            max_pages=1,
        )
        state = build_initial_state(args, {})
        state.selected_preset = "Custom keywords"
        state.enabled_sources = ["hh", "justjoin"]
        state.search_keywords = ["n8n", "AI automation"]
        state.source_keywords = {}
        state.limit = 7
        state.max_pages = 3
        state.refresh = True
        cfg = {"sources": {"keywords": {"hh": ["old config keyword"]}}}

        saved_cfg = apply_tui_state_to_config(cfg, state)

        self.assertEqual(saved_cfg["sources"]["enabled"], ["hh", "justjoin"])
        self.assertEqual(saved_cfg["sources"]["keywords"]["hh"], ["old config keyword"])
        self.assertEqual(saved_cfg["search"]["keywords"], ["n8n", "AI automation"])
        self.assertEqual(saved_cfg["search"]["limit"], 7)
        self.assertEqual(saved_cfg["search"]["max_pages"], 3)
        self.assertEqual(saved_cfg["tui"]["selected_preset"], "Custom keywords")
        self.assertTrue(saved_cfg["tui"]["refresh"])

    def test_saved_tui_config_is_restored_on_next_launch(self) -> None:
        args = Namespace(
            profile="profile.json",
            db="jobs.db",
            export="jobs.md",
            openai_model="gpt-test",
            analysis_strategy="rule",
            enabled_sources=["sample"],
            limit=2,
            max_pages=1,
        )
        state = build_initial_state(args, {})
        state.selected_preset = "Custom keywords"
        state.enabled_sources = ["hh"]
        state.search_keywords = ["Power Automate"]
        state.limit = 4
        state.max_pages = 2

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yaml"
            saved_cfg = save_tui_config(path, {}, state)
            loaded_cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            restored = build_initial_state(args, loaded_cfg)

        self.assertEqual(saved_cfg["tui"]["enabled_sources"], ["hh"])
        self.assertEqual(restored.selected_preset, "Custom keywords")
        self.assertEqual(restored.enabled_sources, ["hh"])
        self.assertEqual(restored.search_keywords, ["Power Automate"])
        self.assertEqual(restored.limit, 4)
        self.assertEqual(restored.max_pages, 2)

    def test_tui_short_url_keeps_host_and_builds_clickable_text(self) -> None:
        url = "https://justjoin.it/job-offer/company-senior-ai-devops-engineer-warszawa-ai"

        short_url = JobSeekerTui._short_url(url, max_length=32)
        link_text = JobSeekerTui._link_text(url, short_url)

        self.assertIn("justjoin.it", short_url)
        self.assertLessEqual(len(short_url), 32)
        self.assertEqual(link_text.plain, short_url)
        self.assertTrue(link_text.spans)

    def test_parse_status_choice_limits_tui_statuses(self) -> None:
        self.assertEqual(parse_status_choice("applied"), ApplicationStatus.APPLIED)
        self.assertEqual(parse_status_choice("offer"), ApplicationStatus.OFFER)
        with self.assertRaises(ValueError):
            parse_status_choice("unknown")

    def test_live_log_writer_strips_ansi_sequences(self) -> None:
        stream = io.StringIO()
        writer = LiveLogWriter(stream)

        writer.write("\x1b[32mhello\x1b[0m\n")
        writer.flush()

        self.assertEqual(stream.getvalue(), "hello\n")
        self.assertEqual(writer.getvalue(), "hello\n")

    def test_live_log_writer_accepts_concurrent_source_output(self) -> None:
        stream = io.StringIO()
        writer = LiveLogWriter(stream)
        thread_count = 4
        lines_per_thread = 25

        def write_source_lines(source_index: int) -> None:
            for line_index in range(lines_per_thread):
                writer.write(f"source-{source_index}-{line_index}\n")

        threads = [
            threading.Thread(target=write_source_lines, args=(source_index,))
            for source_index in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        writer.flush()

        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), thread_count * lines_per_thread)
        self.assertEqual(len(set(lines)), thread_count * lines_per_thread)

    def test_clean_terminal_text_strips_common_control_sequences(self) -> None:
        dirty = "\x1b[1mhello\x1b[0m\r\n\x1b]0;title\x07world\x08!"

        self.assertEqual(clean_terminal_text(dirty), "hello\n\nworld!")

    def test_configure_terminal_encoding_is_safe_to_call(self) -> None:
        configure_terminal_encoding()


if __name__ == "__main__":
    unittest.main()
