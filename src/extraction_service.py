from __future__ import annotations

from typing import Any, Dict

from .extractor import extract_job_structured
from .openai_extractor import (
    OpenAIExtractionError,
    OpenAIExtractorUnavailable,
    extract_job_structured_openai,
)


VALID_STRATEGIES = {"auto", "heuristic", "openai"}


def extract_job_with_strategy(
    text: str,
    *,
    strategy: str = "auto",
    openai_model: str | None = None,
) -> Dict[str, Any]:
    strategy = strategy.strip().lower()
    if strategy not in VALID_STRATEGIES:
        raise ValueError(f"Unsupported extraction strategy: {strategy}")

    if strategy == "heuristic":
        return extract_job_structured(text)

    if strategy == "openai":
        return extract_job_structured_openai(text, model=openai_model)

    try:
        return extract_job_structured_openai(text, model=openai_model)
    except (OpenAIExtractorUnavailable, OpenAIExtractionError):
        return extract_job_structured(text)
