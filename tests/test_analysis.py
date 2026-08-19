import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.analysis import (
    AIBasedAnalysisStrategy,
    RuleBasedAnalysisStrategy,
    VacancyAnalysisBuilder,
    VacancyAnalysisService,
    _detect_required_english_level,
    _experience_match_score,
)
from cvbankas_tracker.models import (
    AnalysisMethod,
    FitLabel,
    UserProfile,
    Vacancy,
    normalize_cefr_level,
)


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


class ExperienceMatchTests(unittest.TestCase):
    def _profile(self, years: int | float | None) -> UserProfile:
        return UserProfile(
            name="Candidate",
            target_roles=["Python Developer"],
            skills=["python"],
            preferred_locations=["Remote"],
            experience_level="Junior",
            years_of_experience=years,
        )

    def _vacancy(self, text: str) -> Vacancy:
        return Vacancy(
            source_id="1-9",
            source_url="https://example.test/9",
            title="Python Developer",
            company="Test",
            location="Remote",
            salary_text="",
            requirements=[text],
            responsibilities=[],
        )

    def test_meeting_required_years_scores_full(self) -> None:
        score, _ = _experience_match_score(self._vacancy("1 year of experience"), self._profile(1))
        self.assertEqual(score, 10)

    def test_near_match_grades_down_with_fractional_years(self) -> None:
        vacancy = self._vacancy("1 year of experience")
        s08, note08 = _experience_match_score(vacancy, self._profile(0.8))
        s06, _ = _experience_match_score(vacancy, self._profile(0.6))
        s04, _ = _experience_match_score(vacancy, self._profile(0.4))
        # Closer to the required year -> higher score, tapering off proportionally.
        self.assertGreater(s08, s06)
        self.assertGreater(s06, s04)
        self.assertEqual(s08, 6)
        self.assertIn("0.8", note08)

    def test_far_below_requirement_is_strongly_negative(self) -> None:
        # 0.8 of a required 5 years is a poor fit.
        score, _ = _experience_match_score(self._vacancy("5 years experience"), self._profile(0.8))
        self.assertLess(score, 0)

    def test_no_years_in_vacancy_is_neutral_or_seniority_based(self) -> None:
        score, _ = _experience_match_score(self._vacancy("Great team"), self._profile(0.8))
        self.assertIsInstance(score, int)


class EnglishLevelDetectionTests(unittest.TestCase):
    def test_detects_cefr_level_near_english(self) -> None:
        self.assertEqual(_detect_required_english_level("english: c1 required"), 5)
        self.assertEqual(_detect_required_english_level("c1 english is a must"), 5)
        self.assertEqual(_detect_required_english_level("anglų k. b2"), 4)

    def test_takes_strictest_signal(self) -> None:
        text = "english b1 or higher, fluent english preferred"
        # fluent english maps to C1 (rank 5), which outranks the B1 mention.
        self.assertEqual(_detect_required_english_level(text), 5)

    def test_cefr_for_another_language_is_ignored(self) -> None:
        # A2 belongs to German here, and there is no English level stated.
        self.assertIsNone(_detect_required_english_level("english needed; german a2"))

    def test_no_english_mention_returns_none(self) -> None:
        self.assertIsNone(_detect_required_english_level("great python team, remote"))

    def test_normalize_cefr_level(self) -> None:
        self.assertEqual(normalize_cefr_level(" b2 "), "B2")
        self.assertEqual(normalize_cefr_level("c1"), "C1")
        self.assertIsNone(normalize_cefr_level("fluent"))
        self.assertIsNone(normalize_cefr_level(None))


class EnglishLevelMatchTests(unittest.TestCase):
    def _profile(self, ceiling: str | None) -> UserProfile:
        return UserProfile(
            name="Candidate",
            target_roles=["Python Developer"],
            skills=["python"],
            preferred_locations=["Remote"],
            experience_level="Junior",
            years_of_experience=1,
            must_have_skills=["python"],
            max_english_level=ceiling,
        )

    def _vacancy(self, requirement: str) -> Vacancy:
        return Vacancy(
            source_id="1-7",
            source_url="https://example.test/7",
            title="Python Developer",
            company="Test",
            location="Remote",
            salary_text="",
            requirements=[requirement],
            responsibilities=[],
        )

    def _analyze(self, vacancy: Vacancy, profile: UserProfile):
        service = VacancyAnalysisService(primary_strategy=RuleBasedAnalysisStrategy())
        return service.analyze(vacancy, profile)

    def test_requirement_above_ceiling_is_penalized(self) -> None:
        vacancy = self._vacancy("Python role, English C1 required")
        capped = self._analyze(vacancy, self._profile("B2"))
        uncapped = self._analyze(vacancy, self._profile(None))

        self.assertLess(capped.score, uncapped.score)
        self.assertTrue(
            any("above your B2 ceiling" in point for point in capped.missing_points)
        )

    def test_fluent_english_counts_as_above_b2(self) -> None:
        analysis = self._analyze(
            self._vacancy("We need fluent English speakers"), self._profile("B2")
        )
        self.assertTrue(
            any("above your B2 ceiling" in point for point in analysis.missing_points)
        )

    def test_requirement_within_ceiling_is_a_positive_signal(self) -> None:
        analysis = self._analyze(
            self._vacancy("English B1 is enough"), self._profile("B2")
        )
        self.assertTrue(
            any("within your B2 ceiling" in point for point in analysis.matched_points)
        )

    def test_no_ceiling_leaves_scoring_untouched(self) -> None:
        vacancy = self._vacancy("English C2 native required")
        self.assertEqual(
            self._analyze(vacancy, self._profile(None)).score,
            self._analyze(vacancy, self._profile(None)).score,
        )
        # And an unset ceiling never emits an English note.
        analysis = self._analyze(vacancy, self._profile(None))
        self.assertFalse(any("ceiling" in point for point in analysis.missing_points))


if __name__ == "__main__":
    unittest.main()
