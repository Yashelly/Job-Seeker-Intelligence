from __future__ import annotations

import html
import json
import os
import re
from typing import Protocol

from openai import OpenAI

from .ai_cli import parse_json_response, run_claude_cli, run_codex_cli
from .models import Vacancy

EXTRACTION_SYSTEM_PROMPT = (
    "You extract structured job-vacancy fields from one job vacancy. "
    "Prefer concrete facts from the provided vacancy text. "
    "Return JSON with keys: company, location, salary_text, "
    "requirements, responsibilities, notes. "
    "If a field is unavailable, return an empty string or empty list. "
    "Requirements and responsibilities must be concise lists of plain-text items."
)


def build_extraction_prompt_payload(vacancy: Vacancy, visible_text: str) -> dict[str, object]:
    return {
        "current_fields": {
            "source": vacancy.source_name,
            "title": vacancy.title,
            "company": vacancy.company,
            "location": vacancy.location,
            "salary_text": vacancy.salary_text,
            "requirements": vacancy.requirements,
            "responsibilities": vacancy.responsibilities,
        },
        "vacancy_text": visible_text,
    }


def normalize_extraction_result(data: dict[str, object]) -> dict[str, object]:
    return {
        "company": str(data.get("company", "")).strip(),
        "location": str(data.get("location", "")).strip(),
        "salary_text": str(data.get("salary_text", "")).strip(),
        "requirements": list(data.get("requirements", [])),
        "responsibilities": list(data.get("responsibilities", [])),
        "notes": str(data.get("notes", "")).strip(),
    }


class AIVacancyExtractionClient(Protocol):
    def extract(self, vacancy: Vacancy, visible_text: str) -> dict[str, object]:
        """Return structured vacancy field values derived from the raw vacancy text."""


class DemoAIVacancyExtractionClient:
    def extract(self, vacancy: Vacancy, visible_text: str) -> dict[str, object]:
        return {
            "company": "",
            "location": "",
            "salary_text": "",
            "requirements": [],
            "responsibilities": [],
            "notes": "No real AI extraction client configured; keeping parser-only fields.",
        }


class OpenAIVacancyExtractionClient:
    def __init__(
        self,
        model: str = "gpt-4.1-mini",
        api_key: str | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self._model = model

    def extract(self, vacancy: Vacancy, visible_text: str) -> dict[str, object]:
        prompt = build_extraction_prompt_payload(vacancy, visible_text)

        completion = self._client.chat.completions.create(
            model=self._model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": EXTRACTION_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False),
                },
            ],
        )
        raw_content = completion.choices[0].message.content or "{}"
        data = json.loads(raw_content)
        return normalize_extraction_result(data)


class _CLIVacancyExtractionClient:
    """Base for CLI-subscription-backed extraction clients (Claude Code / Codex)."""

    def __init__(self, *, command: str, model: str | None) -> None:
        self._command = command
        self._model = model

    def _run_cli(self, prompt: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def extract(self, vacancy: Vacancy, visible_text: str) -> dict[str, object]:
        payload = build_extraction_prompt_payload(vacancy, visible_text)
        prompt = (
            f"{EXTRACTION_SYSTEM_PROMPT}\n\n"
            "INPUT (current fields and raw vacancy text as JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Respond with ONLY the JSON object, no prose and no markdown fences."
        )
        raw = self._run_cli(prompt)
        data = parse_json_response(raw)
        return normalize_extraction_result(data)


class ClaudeCLIVacancyExtractionClient(_CLIVacancyExtractionClient):
    def __init__(self, *, command: str = "claude", model: str | None = None) -> None:
        super().__init__(command=command, model=model)

    def _run_cli(self, prompt: str) -> str:
        return run_claude_cli(prompt, command=self._command, model=self._model)


class CodexCLIVacancyExtractionClient(_CLIVacancyExtractionClient):
    def __init__(self, *, command: str = "codex", model: str | None = None) -> None:
        super().__init__(command=command, model=model)

    def _run_cli(self, prompt: str) -> str:
        return run_codex_cli(prompt, command=self._command, model=self._model)


class VacancyAIEnrichmentService:
    _KEYWORDS = (
        "reikalavimai",
        "atsakomybės",
        "darbo pobūdis",
        "atlyginimas",
        "requirements",
        "responsibilities",
        "qualification",
        "what we expect",
        "your job",
        "you will",
    )

    def __init__(self, client: AIVacancyExtractionClient) -> None:
        self._client = client

    def enrich(self, vacancy: Vacancy) -> Vacancy:
        if not self._needs_enrichment(vacancy):
            return vacancy

        visible_text = self._build_visible_text(vacancy.raw_text)
        if not visible_text:
            return vacancy

        extracted = self._client.extract(vacancy, visible_text)
        return Vacancy(
            source_name=vacancy.source_name,
            source_id=vacancy.source_id,
            source_url=vacancy.source_url,
            title=vacancy.title,
            company=vacancy.company or self._clean_string(extracted.get("company", "")),
            location=vacancy.location or self._clean_string(extracted.get("location", "")),
            salary_text=vacancy.salary_text or self._clean_string(extracted.get("salary_text", "")),
            requirements=vacancy.requirements or self._clean_list(extracted.get("requirements", [])),
            responsibilities=vacancy.responsibilities
            or self._clean_list(extracted.get("responsibilities", [])),
            raw_text=vacancy.raw_text,
        )

    def _needs_enrichment(self, vacancy: Vacancy) -> bool:
        return not (
            vacancy.company
            and vacancy.location
            and vacancy.salary_text
            and vacancy.requirements
            and vacancy.responsibilities
        )

    def _build_visible_text(self, raw_html: str) -> str:
        if not raw_html:
            return ""

        normalized = self._clean_html(raw_html)
        if not normalized:
            return ""

        chunks: list[str] = []
        self._append_chunk(chunks, normalized[:12000])

        lowered = normalized.lower()
        for keyword in self._KEYWORDS:
            start = 0
            found = 0
            while found < 2:
                index = lowered.find(keyword, start)
                if index == -1:
                    break
                snippet = normalized[max(0, index - 800) : index + 2800]
                self._append_chunk(chunks, snippet)
                start = index + len(keyword)
                found += 1

        self._append_chunk(chunks, normalized[-4000:])
        return "\n\n".join(chunks)[:22000]

    def _append_chunk(self, chunks: list[str], chunk: str) -> None:
        cleaned = " ".join(chunk.split()).strip()
        if not cleaned:
            return
        if cleaned in chunks:
            return
        chunks.append(cleaned)

    def _clean_html(self, value: str) -> str:
        value = html.unescape(value)
        value = re.sub(r"<script.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<style.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def _clean_string(self, value: object) -> str:
        return str(value).strip()

    def _clean_list(self, values: object) -> list[str]:
        if not isinstance(values, list):
            return []

        seen: set[str] = set()
        cleaned: list[str] = []
        for value in values:
            text = str(value).strip()
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return cleaned
