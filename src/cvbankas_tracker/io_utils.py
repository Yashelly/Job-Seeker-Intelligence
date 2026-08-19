from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .models import (
    ApplicationRecord,
    UserProfile,
    Vacancy,
    VacancyAnalysis,
    normalize_cefr_level,
)


class ProfileFileReader:
    def read(self, profile_path: str | Path) -> UserProfile:
        path = Path(profile_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return UserProfile(
            name=data["name"],
            target_roles=list(data["target_roles"]),
            skills=list(data["skills"]),
            preferred_locations=list(data["preferred_locations"]),
            experience_level=data["experience_level"],
            years_of_experience=data.get("years_of_experience"),
            salary_expectation=data.get("salary_expectation"),
            additional_keywords=list(data.get("additional_keywords", [])),
            must_have_skills=list(data.get("must_have_skills", [])),
            nice_to_have_skills=list(data.get("nice_to_have_skills", [])),
            excluded_keywords=list(data.get("excluded_keywords", [])),
            max_english_level=normalize_cefr_level(data.get("max_english_level")),
        )


class ReportFileWriter:
    RAW_EXCERPT_LIMIT = 1400

    def write_report(
        self,
        output_path: str | Path,
        rows: list[tuple[Vacancy, VacancyAnalysis, ApplicationRecord | None]],
    ) -> Path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# Job Seeker Vacancy Report",
            "",
            *self._ai_reanalysis_prompt(),
        ]

        for vacancy, analysis, application in rows:
            lines.extend(
                [
                    f"## {vacancy.title}",
                    f"- Source: {vacancy.source_name}",
                    f"- Source ID: {vacancy.source_id}",
                    f"- Company: {vacancy.company}",
                    f"- Location: {vacancy.location}",
                    f"- Salary: {vacancy.salary_text or 'Not specified'}",
                    f"- Vacancy URL: {self._markdown_link(vacancy.source_url)}",
                    "",
                    "### Extracted Vacancy Data",
                    "- Requirements / skills:",
                    *self._format_bullets(vacancy.requirements),
                    "- Responsibilities / description:",
                    *self._format_bullets(vacancy.responsibilities),
                    "- Raw text excerpt:",
                    f"  {self._raw_excerpt(vacancy.raw_text)}",
                    "",
                    "### Preliminary Software Analysis",
                    "Use this only as a weak hint. Re-score independently from the vacancy data above.",
                    f"- Analysis method: {analysis.analysis_method.value}",
                    f"- Score: {analysis.score}",
                    f"- Fit label: {analysis.fit_label.value}",
                    f"- Explanation: {analysis.explanation}",
                    f"- Matched points: {', '.join(analysis.matched_points) or 'None'}",
                    f"- Missing points: {', '.join(analysis.missing_points) or 'None'}",
                    f"- Application status: {application.status.value if application else 'Not tracked'}",
                    "",
                ]
            )

        output.write_text("\n".join(lines), encoding="utf-8")
        return output

    def write_tracked_applications_report(
        self,
        output_path: str | Path,
        rows: list[tuple[Vacancy, VacancyAnalysis | None, ApplicationRecord]],
    ) -> Path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# Job Seeker Tracked Applications",
            "",
        ]

        for vacancy, analysis, application in rows:
            lines.extend(
                [
                    f"## {vacancy.title}",
                    f"- Source: {vacancy.source_name}",
                    f"- Source ID: {vacancy.source_id}",
                    f"- Company: {vacancy.company}",
                    f"- Location: {vacancy.location or 'Not specified'}",
                    f"- Application status: {application.status.value}",
                    f"- Notes: {application.notes or 'None'}",
                    f"- Latest score: {analysis.score if analysis else 'Not available'}",
                    f"- Latest fit label: {analysis.fit_label.value if analysis else 'Not available'}",
                    f"- Vacancy URL: {self._markdown_link(vacancy.source_url)}",
                    "",
                ]
            )

        output.write_text("\n".join(lines), encoding="utf-8")
        return output

    def _ai_reanalysis_prompt(self) -> list[str]:
        return [
            "## AI Re-analysis Prompt",
            "",
            "You are an independent job-search analyst. This Markdown file contains structured vacancy data collected by software. Your task is to re-analyze the vacancies from scratch using the search intent below and each vacancy's Extracted Vacancy Data section.",
            "",
            "Search intent: find strong roles around automation, AI automation, AI specialist work, workflow automation, RPA, no-code/low-code, API/system integrations, internal tools, AI agents, and practical AI tooling.",
            "",
            "Important rules:",
            "- Do not trust the existing software score, fit label, matched points, or missing points. Treat them only as a weak hint after your own review.",
            "- Base your judgment primarily on title, company, location, salary, URL, requirements, responsibilities, and raw text excerpt.",
            "- Do not require or infer a candidate profile. Evaluate whether each vacancy matches the search intent itself.",
            "- Every ranked/recommended/rejected vacancy must include the exact Vacancy URL from the report.",
            "- Prioritize automation, AI automation, AI specialist, workflow automation, RPA, no-code/low-code, integrations, and practical AI tooling roles.",
            "- Penalize vacancies that are clearly unrelated, too senior/junior, not remote when remote is required, located in excluded countries, or focused on generic QA/dev work without automation/AI relevance.",
            "- If data is incomplete, say what is missing instead of inventing details.",
            "",
            "Return the result in this format:",
            "1. A ranked table with columns: Rank, Fit 0-100, Priority, Vacancy, Company, Location, Why it fits, Risks, URL.",
            "2. A shortlist of the top 5 vacancies with concrete next actions.",
            "3. A reject list for poor-fit vacancies with one-sentence reasons.",
            "4. Keyword/source recommendations to improve the next search run.",
            "",
        ]

    def _format_bullets(self, values: list[str]) -> list[str]:
        if not values:
            return ["  - Not extracted"]
        return [f"  - {self._clean_inline(value)}" for value in values]

    def _raw_excerpt(self, raw_text: str) -> str:
        cleaned = self._clean_inline(raw_text)
        if not cleaned:
            return "Not extracted"
        if len(cleaned) <= self.RAW_EXCERPT_LIMIT:
            return cleaned
        return f"{cleaned[: self.RAW_EXCERPT_LIMIT].rstrip()}..."

    def _clean_inline(self, value: str) -> str:
        value = html.unescape(value or "")
        value = re.sub(r"<script.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<style.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def _markdown_link(self, url: str) -> str:
        cleaned = self._clean_inline(url)
        if not cleaned:
            return "Not specified"
        return f"[{cleaned}]({cleaned})"
