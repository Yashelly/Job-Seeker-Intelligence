import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.analysis import (
    AIBasedAnalysisStrategy,
    CodexCLIAnalysisClient,
    DemoAIAnalysisClient,
    OpenAIAnalysisClient,
    RuleBasedAnalysisStrategy,
    VacancyAnalysisBuilder,
    VacancyAnalysisService,
    _country_match_tokens,
    _detect_required_english_level,
    _detect_vacancy_work_mode,
    _english_level_match,
    _experience_match_score,
    _infer_profile_seniority,
    _infer_vacancy_seniority,
    _remote_country_restriction_is_compatible,
    _role_match_score,
    _work_mode_match_score,
)
from cvbankas_tracker.models import (
    AnalysisMethod,
    FitLabel,
    UserProfile,
    Vacancy,
    WorkMode,
    normalize_cefr_level,
    normalize_work_modes,
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


class WorkModeDetectionTests(unittest.TestCase):
    def test_detects_modes_from_text(self) -> None:
        self.assertEqual(_detect_vacancy_work_mode("fully remote python role"), "remote")
        self.assertEqual(_detect_vacancy_work_mode("nuotolinis darbas"), "remote")
        self.assertEqual(_detect_vacancy_work_mode("hybrid work in vilnius"), "hybrid")
        # Hybrid wins over a co-occurring "remote" mention.
        self.assertEqual(_detect_vacancy_work_mode("2 days remote, hybrid model"), "hybrid")
        # No signal -> assumed on-site.
        self.assertEqual(_detect_vacancy_work_mode("great team in vilnius"), "office")

    def test_detects_negation_and_common_remote_phrases(self) -> None:
        self.assertEqual(
            _detect_vacancy_work_mode("Remote work is not available; on-site only"), "office"
        )
        self.assertEqual(
            _detect_vacancy_work_mode("Not a hybrid role; work from our office"), "office"
        )
        self.assertEqual(_detect_vacancy_work_mode("Distributed team; location independent"), "remote")
        self.assertEqual(_detect_vacancy_work_mode("Home-based contract"), "remote")

    def test_normalize_work_modes(self) -> None:
        raw = [
            {"mode": "Remote", "country": "Lithuania"},  # country cleared for remote
            {"mode": "hybrid", "country": " Lithuania "},
            "office",
            {"mode": "hybrid", "country": "Poland"},  # duplicate mode dropped
            {"mode": "bogus"},
        ]
        self.assertEqual(
            normalize_work_modes(raw),
            [
                {"mode": "remote", "country": ""},
                {"mode": "hybrid", "country": "Lithuania"},
                {"mode": "office", "country": ""},
            ],
        )
        self.assertEqual(normalize_work_modes("nonsense"), [])


class WorkModeMatchTests(unittest.TestCase):
    def _profile(self, modes: list[WorkMode]) -> UserProfile:
        return UserProfile(
            name="Candidate",
            target_roles=["Python Developer"],
            skills=["python"],
            preferred_locations=["Remote"],
            experience_level="Junior",
            years_of_experience=1,
            must_have_skills=["python"],
            work_modes=modes,
        )

    def _vacancy(self, requirement: str) -> Vacancy:
        return Vacancy(
            source_id="1-8",
            source_url="https://example.test/8",
            title="Python Developer",
            company="Test",
            location="",
            salary_text="",
            requirements=[requirement],
            responsibilities=[],
        )

    def _analyze(self, vacancy: Vacancy, profile: UserProfile):
        service = VacancyAnalysisService(primary_strategy=RuleBasedAnalysisStrategy())
        return service.analyze(vacancy, profile)

    def test_onsite_penalized_when_only_remote_wanted(self) -> None:
        vacancy = self._vacancy("On-site Python role in Vilnius")
        capped = self._analyze(vacancy, self._profile([WorkMode("remote")]))
        uncapped = self._analyze(vacancy, self._profile([]))
        self.assertLess(capped.score, uncapped.score)
        self.assertTrue(
            any("outside your preferences" in point for point in capped.missing_points)
        )

    def test_office_country_matches_via_city_alias(self) -> None:
        # Profile names the country; vacancy names only a city in it.
        analysis = self._analyze(
            self._vacancy("Office job in Kaunas"),
            self._profile([WorkMode("office", "Lithuania")]),
        )
        self.assertTrue(
            any("matches your work-mode preference" in point for point in analysis.matched_points)
        )

    def test_office_in_other_country_is_penalized(self) -> None:
        analysis = self._analyze(
            self._vacancy("Office job in Warsaw"),
            self._profile([WorkMode("office", "Lithuania")]),
        )
        self.assertTrue(
            any("outside your preferences" in point for point in analysis.missing_points)
        )

    def test_no_preference_leaves_work_mode_unscored(self) -> None:
        analysis = self._analyze(
            self._vacancy("On-site Python role in Vilnius"), self._profile([])
        )
        self.assertFalse(
            any("work-mode" in point or "preferences" in point for point in analysis.missing_points)
        )

    def test_rejects_explicit_remote_country_restriction(self) -> None:
        analysis = self._analyze(
            self._vacancy("Remote role; United States residents only"),
            self._profile([WorkMode("remote"), WorkMode("hybrid", "Lithuania")]),
        )
        self.assertLess(analysis.score, 45)
        self.assertTrue(any("country-residency" in point for point in analysis.missing_points))

        residence_required = self._analyze(
            self._vacancy("Remote role; must reside in the US"),
            self._profile([WorkMode("remote"), WorkMode("hybrid", "Lithuania")]),
        )
        self.assertLess(residence_required.score, 45)


class SeniorityInferenceTests(unittest.TestCase):
    def test_distinguishes_staff_principal_and_contradictory_signals(self) -> None:
        self.assertEqual(_infer_vacancy_seniority("Software Engineering Intern"), -1)
        self.assertEqual(_infer_vacancy_seniority("Staff Software Engineer"), 4)
        self.assertEqual(_infer_vacancy_seniority("Principal Data Engineer"), 4)
        self.assertEqual(_infer_vacancy_seniority("Graduate Software Engineer"), 1)
        self.assertEqual(_infer_vacancy_seniority("Not a senior role; junior applicants welcome"), 1)
        self.assertEqual(_infer_vacancy_seniority("Senior or junior level depending on experience"), 0)


class AnalysisClientCoverageTests(unittest.TestCase):
    def test_demo_client_returns_normalized_boosted_analysis(self) -> None:
        vacancy = Vacancy(
            source_id="demo",
            source_url="https://example.test/demo",
            title="Python Developer",
            company="Example",
            location="Remote",
            salary_text="",
            requirements=["Python"],
        )
        profile = UserProfile(
            name="Candidate",
            target_roles=["Python Developer"],
            skills=["Python"],
            preferred_locations=["Remote"],
            experience_level="mid",
        )
        result = DemoAIAnalysisClient().analyze(vacancy, profile)
        self.assertGreaterEqual(result["score"], 10)
        self.assertIn(result["fit_label"], {"Low", "Medium", "High"})
        self.assertIn("AI-assisted", result["explanation"])

    def test_openai_client_sends_structured_prompt_and_normalizes_response(self) -> None:
        completion = MagicMock()
        completion.choices[0].message.content = '{"score": 81, "fit_label": "High"}'
        sdk = MagicMock()
        sdk.chat.completions.create.return_value = completion
        with patch("cvbankas_tracker.analysis.OpenAI", return_value=sdk):
            client = OpenAIAnalysisClient(model="test-model", api_key="test-key")
        result = client.analyze(
            Vacancy(
                source_id="openai",
                source_url="https://example.test/openai",
                title="Python Developer",
                company="Example",
                location="Remote",
                salary_text="",
            ),
            UserProfile(
                name="Candidate",
                target_roles=["Python Developer"],
                skills=["Python"],
                preferred_locations=["Remote"],
                experience_level="mid",
            ),
        )
        self.assertEqual(result["score"], 81)
        call = sdk.chat.completions.create.call_args.kwargs
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["response_format"], {"type": "json_object"})

    def test_codex_client_delegates_and_normalizes_response(self) -> None:
        with patch(
            "cvbankas_tracker.analysis.run_codex_cli",
            return_value='{"score": 64, "fit_label": "Medium"}',
        ) as run_codex:
            result = CodexCLIAnalysisClient(command="codex-test", model="test-model").analyze(
                Vacancy(
                    source_id="codex",
                    source_url="https://example.test/codex",
                    title="Python Developer",
                    company="Example",
                    location="Remote",
                    salary_text="",
                ),
                UserProfile(
                    name="Candidate",
                    target_roles=["Python Developer"],
                    skills=["Python"],
                    preferred_locations=["Remote"],
                    experience_level="mid",
                ),
            )

        self.assertEqual(result["score"], 64)
        run_codex.assert_called_once()
        self.assertEqual(run_codex.call_args.kwargs["command"], "codex-test")
        self.assertEqual(run_codex.call_args.kwargs["model"], "test-model")


class AnalysisDecisionBranchTests(unittest.TestCase):
    @staticmethod
    def profile(*, years=None, level="junior", locations=None, modes=None) -> UserProfile:
        return UserProfile(
            name="Candidate",
            target_roles=["Python Developer"],
            skills=["Python"],
            preferred_locations=locations or [],
            experience_level=level,
            years_of_experience=years,
            work_modes=modes or [],
        )

    def test_profile_seniority_covers_year_and_label_boundaries(self) -> None:
        self.assertEqual(_infer_profile_seniority(self.profile(years=6)), 3)
        self.assertEqual(_infer_profile_seniority(self.profile(years=2)), 2)
        self.assertEqual(_infer_profile_seniority(self.profile(years=1)), 1)
        self.assertEqual(_infer_profile_seniority(self.profile(level="senior")), 3)
        self.assertEqual(_infer_profile_seniority(self.profile(level="mid-level")), 2)
        self.assertEqual(_infer_profile_seniority(self.profile(level="junior")), 1)

    def test_role_matching_covers_text_only_empty_and_symbolic_targets(self) -> None:
        score, _ = _role_match_score(["AI Architect"], "python developer", "ai architect duties")
        self.assertEqual(score, 15)
        self.assertEqual(_role_match_score(["", "++"], "developer", "python role")[0], 0)

    def test_experience_seniority_fallback_accepts_and_rejects(self) -> None:
        vacancy = Vacancy(
            source_id="senior",
            source_url="https://example.test/senior",
            title="Senior Python Developer",
            company="Example",
            location="",
            salary_text="",
        )
        self.assertEqual(_experience_match_score(vacancy, self.profile(years=6))[0], 8)
        self.assertEqual(_experience_match_score(vacancy, self.profile(years=1))[0], -8)

    def test_rule_scoring_places_experience_notes_in_the_correct_bucket(self) -> None:
        service = VacancyAnalysisService(primary_strategy=RuleBasedAnalysisStrategy())
        positive = Vacancy(
            source_id="positive",
            source_url="https://example.test/positive",
            title="Python Developer",
            company="Example",
            location="Remote",
            salary_text="",
            requirements=["1 year of experience"],
        )
        negative = Vacancy(
            source_id="negative",
            source_url="https://example.test/negative",
            title="Python Developer",
            company="Example",
            location="Remote",
            salary_text="",
            requirements=["5 years of experience"],
        )

        matched = service.analyze(positive, self.profile(years=2))
        missing = service.analyze(negative, self.profile(years=1))

        self.assertTrue(any("experience" in point.lower() for point in matched.matched_points))
        self.assertTrue(any("5+ year expectation" in point for point in missing.missing_points))

    def test_neutral_english_and_matching_remote_helpers(self) -> None:
        vacancy = Vacancy(
            source_id="remote",
            source_url="https://example.test/remote",
            title="Python Developer",
            company="Example",
            location="Remote",
            salary_text="",
            requirements=["Python"],
        )
        profile = self.profile(years=2, modes=[WorkMode("remote")])
        profile.max_english_level = "B2"

        self.assertEqual(_english_level_match(vacancy, profile), (0, "", True))
        self.assertEqual(_work_mode_match_score(vacancy, profile)[0], 10)

    def test_country_and_mixed_schedule_boundaries(self) -> None:
        self.assertEqual(_country_match_tokens(""), ())
        self.assertEqual(
            _detect_vacancy_work_mode("2 days remote and 3 days on-site"), "hybrid"
        )
        lithuania = self.profile(
            locations=["Vilnius"], modes=[WorkMode("remote"), WorkMode("office", "Lithuania")]
        )
        self.assertTrue(_remote_country_restriction_is_compatible("remote across europe", lithuania))
        unknown_location = self.profile(modes=[WorkMode("remote")])
        self.assertTrue(
            _remote_country_restriction_is_compatible(
                "remote; united states residents only", unknown_location
            )
        )
        self.assertTrue(
            _remote_country_restriction_is_compatible(
                "remote; residents only in an unspecified country", lithuania
            )
        )

    def test_additional_seniority_phrase_boundaries(self) -> None:
        self.assertEqual(_infer_vacancy_seniority("Seniority is not required"), 0)
        self.assertEqual(_infer_vacancy_seniority("Middle Python developer"), 2)
        self.assertEqual(_infer_vacancy_seniority("Entry-level Python developer"), 1)


if __name__ == "__main__":
    unittest.main()
