import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker import ai_cli
from cvbankas_tracker.ai_cli import (
    AICLIError,
    parse_json_response,
    run_claude_cli,
    run_codex_cli,
)
from cvbankas_tracker.analysis import (
    ClaudeCLIAnalysisClient,
    CodexCLIAnalysisClient,
    RuleBasedAnalysisStrategy,
)
from cvbankas_tracker.extraction import (
    ClaudeCLIVacancyExtractionClient,
    CodexCLIVacancyExtractionClient,
)
from cvbankas_tracker.main import (
    build_analysis_service,
    build_extraction_service,
    resolve_ai_backend,
)
from cvbankas_tracker.models import FitLabel, UserProfile, Vacancy


def _vacancy() -> Vacancy:
    return Vacancy(
        source_id="1-1",
        source_url="https://www.cvbankas.lt/job/1-1",
        title="Automation Engineer",
        company="",
        location="",
        salary_text="",
        raw_text="<html><body>Python SQL</body></html>",
    )


def _profile() -> UserProfile:
    return UserProfile(
        name="Tester",
        target_roles=["Automation Engineer"],
        skills=["Python"],
        preferred_locations=["Vilnius"],
        experience_level="Mid",
    )


class ParseJsonResponseTests(unittest.TestCase):
    def test_parses_plain_json(self) -> None:
        self.assertEqual(parse_json_response('{"score": 42}'), {"score": 42})

    def test_strips_markdown_fences(self) -> None:
        text = "```json\n{\"score\": 7}\n```"
        self.assertEqual(parse_json_response(text), {"score": 7})

    def test_salvages_json_wrapped_in_prose(self) -> None:
        text = 'Here you go:\n{"score": 5, "notes": "ok"}\nHope that helps!'
        self.assertEqual(parse_json_response(text), {"score": 5, "notes": "ok"})

    def test_raises_on_non_json(self) -> None:
        with self.assertRaises(AICLIError):
            parse_json_response("no json here at all")


class RunClaudeCLITests(unittest.TestCase):
    def test_extracts_result_field_from_envelope(self) -> None:
        envelope = {"is_error": False, "result": '{"score": 88}'}
        completed = MagicMock(returncode=0, stdout=json.dumps(envelope), stderr="")
        with (
            patch.object(ai_cli.subprocess, "run", return_value=completed) as run,
            # Force the fallback branch so the bare command name is asserted
            # deterministically, regardless of what is installed on PATH.
            patch.object(ai_cli.shutil, "which", return_value=None),
        ):
            result = run_claude_cli("prompt", model="claude-opus-4-8")

        self.assertEqual(result, '{"score": 88}')
        args = run.call_args.args[0]
        self.assertEqual(args[0], "claude")
        self.assertIn("--output-format", args)
        self.assertIn("claude-opus-4-8", args)

    def test_raises_on_is_error_envelope(self) -> None:
        envelope = {"is_error": True, "result": "boom"}
        completed = MagicMock(returncode=0, stdout=json.dumps(envelope), stderr="")
        with patch.object(ai_cli.subprocess, "run", return_value=completed):
            with self.assertRaises(AICLIError):
                run_claude_cli("prompt")

    def test_raises_on_nonzero_exit(self) -> None:
        completed = MagicMock(returncode=1, stdout="", stderr="auth failed")
        with patch.object(ai_cli.subprocess, "run", return_value=completed):
            with self.assertRaises(AICLIError):
                run_claude_cli("prompt")


class RunCodexCLITests(unittest.TestCase):
    def test_reads_output_last_message_file(self) -> None:
        def fake_run(args, **kwargs):
            out_path = Path(args[args.index("-o") + 1])
            out_path.write_text('{"score": 61}', encoding="utf-8")
            return MagicMock(returncode=0, stdout="events", stderr="")

        with patch.object(ai_cli.subprocess, "run", side_effect=fake_run):
            result = run_codex_cli("prompt", model="gpt-5")

        self.assertEqual(result, '{"score": 61}')

    def test_raises_on_nonzero_exit(self) -> None:
        completed = MagicMock(returncode=2, stdout="", stderr="not logged in")
        with patch.object(ai_cli.subprocess, "run", return_value=completed):
            with self.assertRaises(AICLIError):
                run_codex_cli("prompt")


class CLIClientTests(unittest.TestCase):
    def test_claude_analysis_client_parses_response(self) -> None:
        payload = {
            "score": 90,
            "fit_label": FitLabel.HIGH.value,
            "explanation": "Strong match",
            "matched_points": ["Python"],
            "missing_points": [],
            "notes": "cli",
        }
        with patch(
            "cvbankas_tracker.analysis.run_claude_cli", return_value=json.dumps(payload)
        ) as run:
            result = ClaudeCLIAnalysisClient().analyze(_vacancy(), _profile())

        self.assertEqual(result["score"], 90)
        self.assertEqual(result["fit_label"], FitLabel.HIGH.value)
        run.assert_called_once()

    def test_codex_extraction_client_parses_response(self) -> None:
        payload = {
            "company": "Codex Co",
            "location": "Remote",
            "salary_text": "",
            "requirements": ["Python"],
            "responsibilities": ["Ship"],
            "notes": "cli",
        }
        with patch(
            "cvbankas_tracker.extraction.run_codex_cli", return_value=json.dumps(payload)
        ):
            result = CodexCLIVacancyExtractionClient().extract(_vacancy(), "Python role")

        self.assertEqual(result["company"], "Codex Co")
        self.assertEqual(result["requirements"], ["Python"])

    def test_claude_extraction_client_type(self) -> None:
        self.assertIsInstance(
            ClaudeCLIVacancyExtractionClient(), ClaudeCLIVacancyExtractionClient
        )

    def test_codex_analysis_client_type(self) -> None:
        self.assertIsInstance(CodexCLIAnalysisClient(), CodexCLIAnalysisClient)


class NormalizeAnalysisResultTests(unittest.TestCase):
    def test_keeps_valid_label(self) -> None:
        from cvbankas_tracker.analysis import normalize_analysis_result

        result = normalize_analysis_result({"score": 80, "fit_label": "High"})
        self.assertEqual(result["fit_label"], FitLabel.HIGH.value)

    def test_coerces_invalid_label_from_score(self) -> None:
        from cvbankas_tracker.analysis import normalize_analysis_result

        result = normalize_analysis_result({"score": 92, "fit_label": "Strong"})
        self.assertEqual(result["fit_label"], FitLabel.HIGH.value)

        low = normalize_analysis_result({"score": 20, "fit_label": "Weak"})
        self.assertEqual(low["fit_label"], FitLabel.LOW.value)


class BackendSelectionTests(unittest.TestCase):
    def test_resolve_backend_explicit(self) -> None:
        with patch.dict(os.environ, {"AI_BACKEND": "claude_cli"}, clear=False):
            self.assertEqual(resolve_ai_backend(), "claude_cli")

    def test_resolve_backend_defaults_to_openai_when_key_present(self) -> None:
        with patch.dict(os.environ, {"AI_BACKEND": "", "OPENAI_API_KEY": "k"}, clear=False):
            self.assertEqual(resolve_ai_backend(), "openai")

    def test_resolve_backend_defaults_to_rule_without_key(self) -> None:
        env = {"AI_BACKEND": "", "OPENAI_API_KEY": ""}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(resolve_ai_backend(), "rule")

    def test_explicit_demo_backend_still_available(self) -> None:
        with patch.dict(os.environ, {"AI_BACKEND": "demo"}, clear=False):
            self.assertEqual(resolve_ai_backend(), "demo")

    def test_no_key_no_cli_builds_pure_rule_service(self) -> None:
        env = {"AI_BACKEND": "", "OPENAI_API_KEY": ""}
        with patch.dict(os.environ, env, clear=False):
            service = build_analysis_service("ai", "gpt-4.1-mini")
        self.assertIsNone(service.fallback_strategy)
        self.assertIsInstance(service.primary_strategy, RuleBasedAnalysisStrategy)

    def test_build_analysis_service_uses_claude_cli(self) -> None:
        with patch.dict(os.environ, {"AI_BACKEND": "claude_cli"}, clear=False):
            with patch("cvbankas_tracker.main.ClaudeCLIAnalysisClient") as client:
                build_analysis_service("ai", "gpt-4.1-mini")
        client.assert_called_once()

    def test_build_extraction_service_uses_codex_cli(self) -> None:
        with patch.dict(os.environ, {"AI_BACKEND": "codex_cli"}, clear=False):
            with patch("cvbankas_tracker.main.CodexCLIVacancyExtractionClient") as client:
                build_extraction_service("gpt-4.1-mini")
        client.assert_called_once()


if __name__ == "__main__":
    unittest.main()
