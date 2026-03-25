from __future__ import annotations

from typing import Any

from .openai_match_evaluator import evaluate_job_match_with_openai
from .scorer import score_job


OFF_TARGET_WORDS = {
    "pardav", "sandėlio", "vairuotoj", "darbinink", "asfalt",
    "operator", "kasinink", "padavėj", "komplektuotoj",
    "krautuvo", "surinkė", "atrinkė", "buhalter", "statyb",
    "virėj", "valytoj", "apsaug",
}

OFF_TARGET_CATEGORIES = {
    "statyba", "gamyba", "sandėli", "prekyba", "maitin", "apskait", "transport"
}


def _norm(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(x) for x in value if x).lower()
    return str(value).lower()


def hard_off_target_filter(job: dict[str, Any]) -> dict[str, Any] | None:
    title = _norm(job.get("role_title"))
    category = _norm(job.get("category"))
    text = " ".join(
        [
            title,
            category,
            _norm(job.get("domain")),
            _norm(job.get("responsibilities")),
            _norm(job.get("required_skills")),
            _norm(job.get("raw_text")),
        ]
    )

    if any(word in title for word in OFF_TARGET_WORDS):
        return {
            "target_domain": "off_target",
            "relevance_score": 0,
            "fit_score": 0,
            "score": 0,
            "decision": "skip",
            "confidence": 95,
            "positive_signals": [],
            "negative_signals": [f"Hard off-target title signal: {word}" for word in OFF_TARGET_WORDS if word in title][:3],
            "reasoning_summary": "Role is clearly outside target domain based on title.",
            "breakdown": {"relevance_score": 0, "fit_score": 0, "confidence": 95},
            "top_strengths": [],
            "top_gaps": ["Clearly off-target role"],
            "blockers": ["Clearly off-target role"],
        }

    if any(word in category for word in OFF_TARGET_CATEGORIES):
        return {
            "target_domain": "off_target",
            "relevance_score": 0,
            "fit_score": 0,
            "score": 0,
            "decision": "skip",
            "confidence": 90,
            "positive_signals": [],
            "negative_signals": [f"Hard off-target category signal: {word}" for word in OFF_TARGET_CATEGORIES if word in category][:3],
            "reasoning_summary": "Role is clearly outside target domain based on vacancy category.",
            "breakdown": {"relevance_score": 0, "fit_score": 0, "confidence": 90},
            "top_strengths": [],
            "top_gaps": ["Clearly off-target vacancy category"],
            "blockers": ["Clearly off-target vacancy category"],
        }

    if any(word in text for word in {"asfalt", "forklift", "krautuvo", "warehouse", "sandėlio", "cashier"}):
        return {
            "target_domain": "off_target",
            "relevance_score": 0,
            "fit_score": 0,
            "score": 0,
            "decision": "skip",
            "confidence": 85,
            "positive_signals": [],
            "negative_signals": ["Manual labor / retail / warehouse context detected"],
            "reasoning_summary": "Role appears to be manual labor / retail / warehouse, which is off-target.",
            "breakdown": {"relevance_score": 0, "fit_score": 0, "confidence": 85},
            "top_strengths": [],
            "top_gaps": ["Manual labor / retail / warehouse context"],
            "blockers": ["Manual labor / retail / warehouse context"],
        }

    return None


def evaluate_job_match(
    job: dict[str, Any],
    profile: dict[str, Any],
    strategy: str = "openai",
    openai_model: str | None = None,
) -> dict[str, Any]:
    hard_result = hard_off_target_filter(job)
    if hard_result is not None:
        return hard_result

    if strategy == "openai":
        return evaluate_job_match_with_openai(job=job, profile=profile, model=openai_model)

    return score_job(job, profile)