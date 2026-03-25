from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from .match_models import MatchEvaluationModel


SYSTEM_PROMPT = """
You evaluate job-vacancy match quality for a specific candidate.

Your job is NOT to decide only whether the candidate is fully qualified right now.
You must separately evaluate:
1. target_domain: is this vacancy in the candidate's target domain at all?
2. relevance_score: how relevant this role is to the candidate's target direction
3. fit_score: how well the candidate currently fits the requirements
4. decision:
   - apply: relevant and realistically suitable now
   - stretch: relevant but partially above current level / not a strong fit yet
   - skip: off-target or poor fit

Important rules:
- Do NOT use naive keyword matching.
- Understand role meaning from title, category, and vacancy content.
- Manual labor, retail, warehouse, driving, cashier, waiter, construction-worker, forklift, picker/packer roles are off_target.
- Roles related to IT support, systems administration, Microsoft 365, cloud, Azure, automation, infrastructure, AI/automation, process automation are target or adjacent depending on context.
- Manager/lead/head roles in the target field may still be target with low or medium fit.
- "Biuro administratorė" or general office admin is not automatically IT target.
- Sparse vacancy text should lower confidence, but should not override obvious title/category signals.

Return only structured JSON following the schema.
""".strip()


def evaluate_job_match_with_openai(
    job: dict[str, Any],
    profile: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=api_key)
    model_name = model or os.getenv("JOB_AGENT_OPENAI_MODEL") or "gpt-4o-mini"

    payload = {
        "candidate_profile": profile,
        "vacancy": job,
    }

    response = client.responses.parse(
        model=model_name,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Evaluate this vacancy match:\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}",
            },
        ],
        text_format=MatchEvaluationModel,
    )

    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("OpenAI returned no parsed match result")

    result = parsed.model_dump()

    final_score = int(round(result["relevance_score"] * 0.65 + result["fit_score"] * 0.35))
    result["score"] = max(0, min(100, final_score))

    result["breakdown"] = {
        "relevance_score": result["relevance_score"],
        "fit_score": result["fit_score"],
        "confidence": result["confidence"],
    }
    result["top_strengths"] = result.get("positive_signals", [])[:5]
    result["top_gaps"] = result.get("negative_signals", [])[:5]
    result["blockers"] = [x for x in result.get("negative_signals", []) if "off-target" in x.lower()]

    return result