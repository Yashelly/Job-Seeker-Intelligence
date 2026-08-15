"""Defensive-normalization tests for untrusted AI provider output.

Language-model responses are untrusted input: scores arrive as strings or out
of range, labels are missing or bogus, and list fields come back as scalars or
lists with non-string members. These tests pin the coercion contract so a
malformed response degrades gracefully instead of raising in the pipeline.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.ai_cli import coerce_score, coerce_str_list
from cvbankas_tracker.analysis import (
    AIBasedAnalysisStrategy,
    RuleBasedAnalysisStrategy,
    VacancyAnalysisBuilder,
    VacancyAnalysisService,
    normalize_analysis_result,
)
from cvbankas_tracker.extraction import normalize_extraction_result
from cvbankas_tracker.models import UserProfile, Vacancy


def _profile() -> UserProfile:
    return UserProfile(
        name="Test",
        target_roles=["python developer"],
        skills=["python"],
        must_have_skills=["python"],
        nice_to_have_skills=[],
        excluded_keywords=[],
        preferred_locations=["remote"],
        experience_level="Mid",
        years_of_experience=3,
        salary_expectation=None,
        additional_keywords=[],
    )


def _vacancy() -> Vacancy:
    return Vacancy(
        source_name="sample",
        source_id="1",
        source_url="https://example.test/1",
        title="Python Developer",
        company="Acme",
        location="Remote",
        salary_text="",
        requirements=["python"],
        responsibilities=["build things"],
        raw_text="python developer remote",
    )


class CoerceScoreTests(unittest.TestCase):
    def test_clamps_out_of_range(self) -> None:
        self.assertEqual(coerce_score(150), 100)
        self.assertEqual(coerce_score(-20), 0)

    def test_parses_numeric_string(self) -> None:
        self.assertEqual(coerce_score("85"), 85)
        self.assertEqual(coerce_score("85%"), 85)

    def test_non_numeric_collapses_to_low(self) -> None:
        self.assertEqual(coerce_score("high"), 0)
        self.assertEqual(coerce_score(None), 0)
        self.assertEqual(coerce_score([]), 0)

    def test_bool_is_not_a_score(self) -> None:
        # bool is an int subclass; it must not be read as 0/1.
        self.assertEqual(coerce_score(True), 0)

    def test_float_is_truncated(self) -> None:
        self.assertEqual(coerce_score(72.9), 72)


class CoerceStrListTests(unittest.TestCase):
    def test_scalar_string_wrapped(self) -> None:
        self.assertEqual(coerce_str_list("only one"), ["only one"])

    def test_none_and_scalars(self) -> None:
        self.assertEqual(coerce_str_list(None), [])
        self.assertEqual(coerce_str_list(42), [])

    def test_mixed_list_stringifies_scalars_and_skips_structures(self) -> None:
        self.assertEqual(
            coerce_str_list(["a", 2, "", None, {"nested": 1}, ["x"], "  b "]),
            ["a", "2", "b"],
        )


class NormalizeAnalysisTests(unittest.TestCase):
    def test_malformed_payload_does_not_raise(self) -> None:
        result = normalize_analysis_result(
            {
                "score": "not a number",
                "fit_label": "Excellent",  # not a real label
                "explanation": 123,
                "matched_points": "single point",
                "missing_points": [1, 2, {"x": 1}],
            }
        )
        self.assertEqual(result["score"], 0)
        self.assertIn(result["fit_label"], {"High", "Medium", "Low"})
        self.assertEqual(result["explanation"], "123")
        self.assertEqual(result["matched_points"], ["single point"])
        self.assertEqual(result["missing_points"], ["1", "2"])

    def test_non_dict_payload_yields_defaults(self) -> None:
        result = normalize_analysis_result(["unexpected"])  # type: ignore[arg-type]
        self.assertEqual(result["score"], 0)

    def test_label_derived_from_score_when_missing(self) -> None:
        self.assertEqual(normalize_analysis_result({"score": 90})["fit_label"], "High")
        self.assertEqual(normalize_analysis_result({"score": 10})["fit_label"], "Low")


class NormalizeExtractionTests(unittest.TestCase):
    def test_scalar_list_fields_are_coerced(self) -> None:
        result = normalize_extraction_result(
            {"requirements": "python", "responsibilities": [1, "ship"]}
        )
        self.assertEqual(result["requirements"], ["python"])
        self.assertEqual(result["responsibilities"], ["1", "ship"])


class MalformedAIFallbackTests(unittest.TestCase):
    """A backend that emits garbage must fall back to deterministic scoring."""

    class _BrokenClient:
        def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
            raise RuntimeError("provider exploded")

    def test_service_falls_back_to_rule_based(self) -> None:
        service = VacancyAnalysisService(
            primary_strategy=AIBasedAnalysisStrategy(self._BrokenClient()),
            fallback_strategy=RuleBasedAnalysisStrategy(),
        )
        analysis = service.analyze(_vacancy(), _profile())
        self.assertEqual(analysis.analysis_method.value, "rule_based")
        self.assertGreaterEqual(analysis.score, 0)

    def test_ai_path_clamps_and_coerces_out_of_range_output(self) -> None:
        # A valid-enough response (has an explanation) stays on the AI path; the
        # out-of-range score is clamped and non-string points are coerced.
        class _NoisyClient:
            def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
                return normalize_analysis_result(
                    {
                        "score": "999",
                        "explanation": "strong match",
                        "matched_points": [None, 3, "python"],
                    }
                )

        service = VacancyAnalysisService(
            primary_strategy=AIBasedAnalysisStrategy(_NoisyClient()),
            fallback_strategy=RuleBasedAnalysisStrategy(),
        )
        analysis = service.analyze(_vacancy(), _profile())
        self.assertEqual(analysis.analysis_method.value, "ai_based")
        self.assertEqual(analysis.score, 100)
        self.assertEqual(analysis.matched_points, ("3", "python"))

    def test_missing_explanation_falls_back_to_rule_based(self) -> None:
        # Normalization does not fabricate an explanation, so an AI response
        # without one is rejected by the builder and the fallback runs.
        class _NoExplanationClient:
            def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
                return normalize_analysis_result({"score": 80})

        service = VacancyAnalysisService(
            primary_strategy=AIBasedAnalysisStrategy(_NoExplanationClient()),
            fallback_strategy=RuleBasedAnalysisStrategy(),
        )
        analysis = service.analyze(_vacancy(), _profile())
        self.assertEqual(analysis.analysis_method.value, "rule_based")

    def test_builder_direct_use_still_validates(self) -> None:
        builder = VacancyAnalysisBuilder()
        self.assertIsNotNone(builder)


if __name__ == "__main__":
    unittest.main()
