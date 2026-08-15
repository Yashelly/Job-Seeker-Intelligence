import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.analysis import (
    AIBasedAnalysisStrategy,
    RuleBasedAnalysisStrategy,
    VacancyAnalysisBuilder,
    VacancyAnalysisService,
)
from cvbankas_tracker.models import AnalysisMethod, FitLabel, UserProfile, Vacancy


class StubAIClient:
    def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
        return {
            "score": 88,
            "fit_label": "High",
            "explanation": "The vacancy strongly matches the profile's Python and SQL focus.",
            "matched_points": ["Python match", "SQL match"],
            "missing_points": ["Remote is not explicitly mentioned"],
            "notes": "Stubbed AI response",
        }


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vacancy = Vacancy(
            source_id="1-1",
            source_url="https://www.cvbankas.lt/python-role/1-1",
            title="Python Developer",
            company="Test Company",
            location="Vilnius",
            salary_text="2500 EUR",
            requirements=["Python", "SQL", "Git"],
            responsibilities=["Build APIs"],
        )
        self.profile = UserProfile(
            name="Student",
            target_roles=["Python Developer"],
            skills=["python", "sql", "testing"],
            preferred_locations=["Vilnius"],
            experience_level="Junior",
            years_of_experience=3,
            additional_keywords=["api"],
            must_have_skills=["python", "sql"],
            nice_to_have_skills=["docker"],
            excluded_keywords=["warehouse"],
        )

    def test_builder_requires_complete_analysis(self) -> None:
        builder = VacancyAnalysisBuilder()
        with self.assertRaises(ValueError):
            builder.build()

    def test_ai_strategy_builds_analysis(self) -> None:
        service = VacancyAnalysisService(
            primary_strategy=AIBasedAnalysisStrategy(StubAIClient()),
            fallback_strategy=RuleBasedAnalysisStrategy(),
        )

        analysis = service.analyze(self.vacancy, self.profile)

        self.assertEqual(analysis.analysis_method, AnalysisMethod.AI_BASED)
        self.assertEqual(analysis.fit_label, FitLabel.HIGH)
        self.assertIn("Python match", analysis.matched_points)

    def test_rule_based_strategy_stays_available_as_fallback(self) -> None:
        service = VacancyAnalysisService(primary_strategy=RuleBasedAnalysisStrategy())
        analysis = service.analyze(self.vacancy, self.profile)

        self.assertEqual(analysis.analysis_method, AnalysisMethod.RULE_BASED)
        self.assertGreaterEqual(analysis.score, 60)

    def test_rule_based_strategy_penalizes_excluded_roles(self) -> None:
        excluded_vacancy = Vacancy(
            source_id="1-2",
            source_url="https://www.cvbankas.lt/warehouse-role/1-2",
            title="Warehouse Worker",
            company="Storage Co",
            location="Vilnius",
            salary_text="1200 EUR",
            requirements=["Warehouse handling"],
            responsibilities=["Move pallets"],
        )

        service = VacancyAnalysisService(primary_strategy=RuleBasedAnalysisStrategy())
        analysis = service.analyze(excluded_vacancy, self.profile)

        self.assertEqual(analysis.analysis_method, AnalysisMethod.RULE_BASED)
        self.assertEqual(analysis.fit_label, FitLabel.LOW)
        self.assertTrue(
            any("Excluded profile keywords matched" in point for point in analysis.missing_points)
        )


if __name__ == "__main__":
    unittest.main()
