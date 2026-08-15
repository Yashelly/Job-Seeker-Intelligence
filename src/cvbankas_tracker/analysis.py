from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

from openai import OpenAI

from .ai_cli import (
    coerce_score,
    coerce_str_list,
    parse_json_response,
    run_claude_cli,
    run_codex_cli,
)
from .models import AnalysisMethod, FitLabel, UserProfile, Vacancy, VacancyAnalysis

ANALYSIS_SYSTEM_PROMPT = (
    "You analyze job vacancies against one user profile. "
    "Use role fit, must-have skills, nice-to-have skills, excluded keywords, "
    "experience fit, location fit, and salary fit when deciding the score. "
    "Strong technical matches should score clearly higher than loosely related roles. "
    "Return JSON with keys: score, fit_label, explanation, "
    "matched_points, missing_points, notes. "
    "score is an integer from 0 to 100. "
    'fit_label MUST be exactly one of "High", "Medium", or "Low" '
    "(High for score >= 75, Medium for 45-74, Low below 45)."
)


def build_analysis_prompt_payload(vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
    return {
        "vacancy": {
            "source": vacancy.source_name,
            "title": vacancy.title,
            "company": vacancy.company,
            "location": vacancy.location,
            "salary_text": vacancy.salary_text,
            "requirements": vacancy.requirements,
            "responsibilities": vacancy.responsibilities,
        },
        "profile": {
            "name": profile.name,
            "target_roles": profile.target_roles,
            "skills": profile.skills,
            "must_have_skills": profile.must_have_skills,
            "nice_to_have_skills": profile.nice_to_have_skills,
            "excluded_keywords": profile.excluded_keywords,
            "preferred_locations": profile.preferred_locations,
            "experience_level": profile.experience_level,
            "years_of_experience": profile.years_of_experience,
            "salary_expectation": profile.salary_expectation,
            "additional_keywords": profile.additional_keywords,
        },
    }


def _coerce_fit_label(raw_label: object, score: int) -> str:
    """Map a model-provided label to a valid FitLabel, falling back to the score band."""
    candidate = str(raw_label).strip().lower()
    for label in FitLabel:
        if candidate == label.value.lower():
            return label.value
    return _label_for_score(score).value


def normalize_analysis_result(data: dict[str, object]) -> dict[str, object]:
    """Coerce raw model output into a schema-valid analysis dict.

    Model responses are untrusted: the score may be a string or out of range,
    the label may be absent or bogus, and the point lists may be scalars or
    contain non-strings. Every field is coerced defensively so a malformed
    response degrades gracefully instead of raising downstream.
    """
    if not isinstance(data, dict):
        data = {}
    score = coerce_score(data.get("score", 0))
    return {
        "score": score,
        "fit_label": _coerce_fit_label(data.get("fit_label", ""), score),
        "explanation": str(data.get("explanation", "")).strip(),
        "matched_points": coerce_str_list(data.get("matched_points")),
        "missing_points": coerce_str_list(data.get("missing_points")),
        "notes": str(data.get("notes", "")).strip(),
    }


class AIAnalysisClient(Protocol):
    def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
        """Return a structured vacancy analysis payload."""


class VacancyAnalysisBuilder:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._vacancy_source_url: str | None = None
        self._analysis_method: AnalysisMethod | None = None
        self._score: int | None = None
        self._fit_label: FitLabel | None = None
        self._explanation: str | None = None
        self._matched_points: list[str] = []
        self._missing_points: list[str] = []
        self._notes: str = ""

    def with_vacancy_source_url(self, source_url: str) -> VacancyAnalysisBuilder:
        self._vacancy_source_url = source_url
        return self

    def with_analysis_method(self, method: AnalysisMethod) -> VacancyAnalysisBuilder:
        self._analysis_method = method
        return self

    def with_score(self, score: int) -> VacancyAnalysisBuilder:
        self._score = max(0, min(100, score))
        return self

    def with_fit_label(self, label: FitLabel) -> VacancyAnalysisBuilder:
        self._fit_label = label
        return self

    def with_explanation(self, explanation: str) -> VacancyAnalysisBuilder:
        self._explanation = explanation.strip()
        return self

    def with_matched_points(self, points: list[str]) -> VacancyAnalysisBuilder:
        self._matched_points = [point.strip() for point in points if point.strip()]
        return self

    def with_missing_points(self, points: list[str]) -> VacancyAnalysisBuilder:
        self._missing_points = [point.strip() for point in points if point.strip()]
        return self

    def with_notes(self, notes: str) -> VacancyAnalysisBuilder:
        self._notes = notes.strip()
        return self

    def build(self) -> VacancyAnalysis:
        missing_fields: list[str] = []
        if not self._vacancy_source_url:
            missing_fields.append("vacancy_source_url")
        if self._analysis_method is None:
            missing_fields.append("analysis_method")
        if self._score is None:
            missing_fields.append("score")
        if self._fit_label is None:
            missing_fields.append("fit_label")
        if not self._explanation:
            missing_fields.append("explanation")

        if missing_fields:
            joined = ", ".join(missing_fields)
            raise ValueError(f"VacancyAnalysis is incomplete: missing {joined}.")

        analysis = VacancyAnalysis(
            vacancy_source_url=self._vacancy_source_url,
            analysis_method=self._analysis_method,
            score=self._score,
            fit_label=self._fit_label,
            explanation=self._explanation,
            matched_points=tuple(self._matched_points),
            missing_points=tuple(self._missing_points),
            notes=self._notes,
        )
        self.reset()
        return analysis


class AnalysisStrategy(ABC):
    method: AnalysisMethod

    @abstractmethod
    def populate_builder(
        self,
        builder: VacancyAnalysisBuilder,
        vacancy: Vacancy,
        profile: UserProfile,
    ) -> None:
        """Populate the builder with analysis parts."""


def _calculate_rule_based_components(
    vacancy: Vacancy,
    profile: UserProfile,
) -> tuple[int, list[str], list[str]]:
    searchable = vacancy.searchable_text
    title_text = vacancy.title.lower()
    matched: list[str] = []
    missing: list[str] = []
    score = 0

    matched_skills = _match_terms(profile.skills, searchable)
    skill_ratio = len(matched_skills) / max(1, len(profile.skills))
    score += int(skill_ratio * 25)

    if matched_skills:
        matched.append(f"Matched skills: {', '.join(matched_skills)}")
    else:
        missing.append("No core profile skills matched directly in the vacancy text.")

    matched_must_have = _match_terms(profile.must_have_skills, searchable)
    if profile.must_have_skills:
        must_have_ratio = len(matched_must_have) / max(1, len(profile.must_have_skills))
        score += int(must_have_ratio * 30)
        if matched_must_have:
            matched.append(f"Matched must-have skills: {', '.join(matched_must_have)}")
        else:
            missing.append("No must-have profile skills were found.")

    matched_nice_to_have = _match_terms(profile.nice_to_have_skills, searchable)
    if profile.nice_to_have_skills:
        score += min(10, len(matched_nice_to_have) * 3)
        if matched_nice_to_have:
            matched.append(f"Nice-to-have skills found: {', '.join(matched_nice_to_have)}")

    role_score, role_note = _role_match_score(profile.target_roles, title_text, searchable)
    if role_score > 0:
        score += role_score
        matched.append(role_note)
    else:
        missing.append("The vacancy title does not closely match the target role.")

    location_match = any(
        location.lower() in searchable for location in profile.preferred_locations
    )
    if location_match:
        score += 10
        matched.append("The vacancy location matches the preferred location.")
    else:
        missing.append("The vacancy location is outside the preferred locations.")

    keyword_matches = _match_terms(profile.additional_keywords, searchable)
    score += min(10, len(keyword_matches) * 2)
    if keyword_matches:
        matched.append(f"Extra profile keywords found: {', '.join(keyword_matches)}")
    else:
        missing.append("No extra profile keywords were found.")

    experience_score, experience_note = _experience_match_score(vacancy, profile)
    score += experience_score
    if experience_note:
        if experience_score >= 0:
            matched.append(experience_note)
        else:
            missing.append(experience_note)

    excluded_matches = _match_terms(profile.excluded_keywords, searchable)
    if excluded_matches:
        score -= 40
        missing.append(f"Excluded profile keywords matched: {', '.join(excluded_matches)}")

    return max(0, min(100, score)), matched, missing


def _match_terms(terms: list[str], searchable: str) -> list[str]:
    matches: list[str] = []
    for term in terms:
        normalized = term.lower().strip()
        if normalized and normalized in searchable and term not in matches:
            matches.append(term)
    return matches


def _role_match_score(
    target_roles: list[str],
    title_text: str,
    searchable: str,
) -> tuple[int, str]:
    best_score = 0
    best_note = ""

    for role in target_roles:
        normalized = role.lower().strip()
        if not normalized:
            continue
        if normalized in title_text:
            return 20, f"The vacancy title directly matches the target role: {role}."
        if normalized in searchable:
            best_score = max(best_score, 15)
            best_note = f"The vacancy text strongly aligns with the target role: {role}."
            continue

        role_words = [word for word in re.split(r"[^a-z0-9]+", normalized) if len(word) > 2]
        if not role_words:
            continue
        overlap = sum(1 for word in role_words if word in title_text)
        if overlap >= 2 or (overlap == 1 and len(role_words) == 1):
            best_score = max(best_score, 10)
            best_note = f"The vacancy title partially aligns with the target role: {role}."

    return best_score, best_note


def _experience_match_score(vacancy: Vacancy, profile: UserProfile) -> tuple[int, str]:
    if profile.years_of_experience is None:
        return 0, ""

    searchable = vacancy.searchable_text
    years_patterns = [
        r"(\d+)\+?\s*(?:years|year)\b",
        r"(\d+)\+?\s*(?:met(?:ų|u)?|m\.)\b",
    ]
    required_years: list[int] = []
    for pattern in years_patterns:
        required_years.extend(int(value) for value in re.findall(pattern, searchable))

    inferred_seniority = _infer_vacancy_seniority(searchable)
    if required_years:
        required = max(required_years)
        if profile.years_of_experience >= required:
            return 10, f"Experience level matches the vacancy expectation ({required}+ years)."
        return -10, f"Vacancy appears to expect about {required}+ years of experience."

    if not inferred_seniority:
        return 0, ""

    profile_level = _infer_profile_seniority(profile)
    if profile_level >= inferred_seniority:
        return 8, "Profile seniority appears compatible with the vacancy level."
    return -8, "Vacancy seniority appears higher than the current profile level."


def _infer_profile_seniority(profile: UserProfile) -> int:
    if profile.years_of_experience is not None:
        if profile.years_of_experience >= 5:
            return 3
        if profile.years_of_experience >= 2:
            return 2
        return 1

    level = profile.experience_level.lower()
    if "senior" in level or "lead" in level:
        return 3
    if "mid" in level:
        return 2
    return 1


def _infer_vacancy_seniority(searchable: str) -> int:
    if any(term in searchable for term in ("senior", "lead", "head", "principal")):
        return 3
    if any(term in searchable for term in ("mid", "middle")):
        return 2
    if any(term in searchable for term in ("junior", "entry-level", "entry level")):
        return 1
    return 0


def _label_for_score(score: int) -> FitLabel:
    if score >= 75:
        return FitLabel.HIGH
    if score >= 45:
        return FitLabel.MEDIUM
    return FitLabel.LOW


class RuleBasedAnalysisStrategy(AnalysisStrategy):
    method = AnalysisMethod.RULE_BASED

    def populate_builder(
        self,
        builder: VacancyAnalysisBuilder,
        vacancy: Vacancy,
        profile: UserProfile,
    ) -> None:
        score, matched, missing = _calculate_rule_based_components(vacancy, profile)
        label = _label_for_score(score)
        explanation = (
            f"Rule-based analysis marked this vacancy as a {label.value.lower()} fit "
            f"with a score of {score}."
        )

        builder.with_vacancy_source_url(vacancy.source_url)
        builder.with_analysis_method(self.method)
        builder.with_score(score)
        builder.with_fit_label(label)
        builder.with_explanation(explanation)
        builder.with_matched_points(matched)
        builder.with_missing_points(missing)
        builder.with_notes("Used deterministic profile-to-vacancy keyword matching.")


class DemoAIAnalysisClient:
    def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
        score, matched, missing = _calculate_rule_based_components(vacancy, profile)
        boosted_score = min(100, score + 10)
        label = _label_for_score(boosted_score)
        explanation = (
            f"AI-assisted review suggests a {label.value.lower()} fit because the vacancy "
            f"matches several profile preferences and core skill keywords."
        )
        return {
            "score": boosted_score,
            "fit_label": label.value,
            "explanation": explanation,
            "matched_points": matched or ["The vacancy has general alignment with the active profile."],
            "missing_points": missing,
            "notes": "Generated by the offline demo AI client.",
        }


class OpenAIAnalysisClient:
    def __init__(
        self,
        model: str = "gpt-4.1-mini",
        api_key: str | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self._model = model

    def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
        prompt = build_analysis_prompt_payload(vacancy, profile)

        completion = self._client.chat.completions.create(
            model=self._model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": ANALYSIS_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False),
                },
            ],
        )
        raw_content = completion.choices[0].message.content or "{}"
        return normalize_analysis_result(parse_json_response(raw_content))


class _CLIAnalysisClient:
    """Base for CLI-subscription-backed analysis clients (Claude Code / Codex)."""

    def __init__(self, *, command: str, model: str | None) -> None:
        self._command = command
        self._model = model

    def _run_cli(self, prompt: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
        payload = build_analysis_prompt_payload(vacancy, profile)
        prompt = (
            f"{ANALYSIS_SYSTEM_PROMPT}\n\n"
            "INPUT (job vacancy and user profile as JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Respond with ONLY the JSON object, no prose and no markdown fences."
        )
        raw = self._run_cli(prompt)
        data = parse_json_response(raw)
        return normalize_analysis_result(data)


class ClaudeCLIAnalysisClient(_CLIAnalysisClient):
    def __init__(self, *, command: str = "claude", model: str | None = None) -> None:
        super().__init__(command=command, model=model)

    def _run_cli(self, prompt: str) -> str:
        return run_claude_cli(prompt, command=self._command, model=self._model)


class CodexCLIAnalysisClient(_CLIAnalysisClient):
    def __init__(self, *, command: str = "codex", model: str | None = None) -> None:
        super().__init__(command=command, model=model)

    def _run_cli(self, prompt: str) -> str:
        return run_codex_cli(prompt, command=self._command, model=self._model)


class AIBasedAnalysisStrategy(AnalysisStrategy):
    method = AnalysisMethod.AI_BASED

    def __init__(self, client: AIAnalysisClient) -> None:
        self._client = client

    def populate_builder(
        self,
        builder: VacancyAnalysisBuilder,
        vacancy: Vacancy,
        profile: UserProfile,
    ) -> None:
        result = self._client.analyze(vacancy, profile)
        score = int(result["score"])
        fit_label = FitLabel(str(result["fit_label"]))

        builder.with_vacancy_source_url(vacancy.source_url)
        builder.with_analysis_method(self.method)
        builder.with_score(score)
        builder.with_fit_label(fit_label)
        builder.with_explanation(str(result["explanation"]))
        builder.with_matched_points(list(result.get("matched_points", [])))
        builder.with_missing_points(list(result.get("missing_points", [])))
        builder.with_notes(str(result.get("notes", "")))


@dataclass(slots=True)
class VacancyAnalysisService:
    primary_strategy: AnalysisStrategy
    fallback_strategy: AnalysisStrategy | None = None

    def analyze(self, vacancy: Vacancy, profile: UserProfile) -> VacancyAnalysis:
        builder = VacancyAnalysisBuilder()
        try:
            self.primary_strategy.populate_builder(builder, vacancy, profile)
            # build() is inside the try on purpose: a primary strategy can
            # populate an *incomplete* builder (e.g. an AI backend that returns
            # an empty explanation) and only fail at build time. That must fall
            # back to the deterministic strategy too, not surface to the caller.
            return builder.build()
        except Exception:
            if self.fallback_strategy is None:
                raise
            builder.reset()
            self.fallback_strategy.populate_builder(builder, vacancy, profile)
            return builder.build()
