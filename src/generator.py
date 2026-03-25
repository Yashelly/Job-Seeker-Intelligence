from __future__ import annotations

from typing import Any, Dict


def build_summary(job: Dict[str, Any], score_result: Dict[str, Any]) -> str:
    role = job.get("role_title", "Unknown role")
    company = job.get("company", "Unknown company")
    score = score_result["score"]
    decision = score_result["decision"]

    strengths = "; ".join(score_result.get("top_strengths", [])) or "No clear strengths detected."
    gaps = "; ".join(score_result.get("top_gaps", [])) or "No major gaps detected."

    return (
        f"Role: {role} at {company}\n"
        f"Decision: {decision.upper()} ({score}/100)\n"
        f"Strengths: {strengths}\n"
        f"Gaps: {gaps}"
    )


def build_cover_letter(job: Dict[str, Any], score_result: Dict[str, Any], profile: Dict[str, Any]) -> str:
    role = job.get("role_title", "the role")
    company = job.get("company", "your company")
    strengths = score_result.get("top_strengths", [])
    gaps = score_result.get("top_gaps", [])

    intro = f"I am interested in the {role} position at {company}."
    background = (
        "My background combines practical IT operations, automation, and structured process work, "
        "including Python, PowerShell, workflow tooling, and documentation-oriented troubleshooting."
    )
    relevance = "Relevant strengths for this role include " + ", ".join(strengths[:3]) + "." if strengths else (
        "I see a practical fit between the role requirements and my current automation and IT operations background."
    )
    close = (
        "I would welcome the opportunity to discuss how I could contribute and where my current profile aligns best with your needs."
    )

    if gaps:
        relevance += " I am also aware of the current gaps, especially " + ", ".join(gaps[:2]) + ", and would address them directly during the process."

    return "\n\n".join([intro, background, relevance, close])
