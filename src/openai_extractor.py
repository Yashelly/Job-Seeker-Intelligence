from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .openai_models import JobExtractionModel
from .utils import normalize_spaces


class OpenAIExtractorUnavailable(RuntimeError):
    pass


class OpenAIExtractionError(RuntimeError):
    pass


SYSTEM_PROMPT = """You extract structured job data from vacancy text.
Return a strict structured response matching the provided schema.
Do not invent missing facts.
Use only these work_mode values: remote, hybrid, on-site, unknown.
Use only these domains when possible: ai_process_automation, automation, integration, cloud_ops, support_ops, general_it.
If a value is missing, use empty string, null, unknown, or [] depending on the field.
Keep raw_text_excerpt short and based on the input text.
"""


def _build_client(client: Any | None = None) -> Any:
    if client is not None:
        return client

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIExtractorUnavailable("OPENAI_API_KEY is not set")

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - depends on environment
        raise OpenAIExtractorUnavailable(
            "openai SDK is not installed; run 'pip install -r requirements.txt'"
        ) from exc

    return OpenAI(api_key=api_key)


def extract_job_structured_openai(
    text: str,
    *,
    model: Optional[str] = None,
    client: Any | None = None,
) -> Dict[str, Any]:
    resolved_model = model or os.getenv("JOB_AGENT_OPENAI_MODEL", "gpt-4o-mini")
    llm_client = _build_client(client)
    cleaned = normalize_spaces(text)

    try:
        response = llm_client.responses.parse(
            model=resolved_model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Extract structured vacancy data from the following text. "
                        "Do not guess missing values.\n\n"
                        f"VACANCY TEXT:\n{cleaned}"
                    ),
                },
            ],
            text_format=JobExtractionModel,
        )
    except Exception as exc:  # pragma: no cover - network/live SDK dependent
        raise OpenAIExtractionError(f"OpenAI extraction failed: {exc}") from exc

    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        raise OpenAIExtractionError("OpenAI response did not contain output_parsed")

    if isinstance(parsed, JobExtractionModel):
        payload = parsed.model_dump()
    elif hasattr(parsed, "model_dump"):
        payload = parsed.model_dump()
    elif isinstance(parsed, dict):
        payload = parsed
    else:
        raise OpenAIExtractionError("Unsupported parsed output type")

    if not payload.get("raw_text_excerpt"):
        payload["raw_text_excerpt"] = cleaned[:2000]

    return payload
