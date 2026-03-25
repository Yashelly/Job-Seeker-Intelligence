from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal


class MatchEvaluationModel(BaseModel):
    target_domain: Literal["target", "adjacent", "off_target"] = Field(
        description="Whether the role is in the user's target domain, adjacent, or off-target."
    )
    relevance_score: int = Field(ge=0, le=100)
    fit_score: int = Field(ge=0, le=100)
    decision: Literal["apply", "stretch", "skip"]
    confidence: int = Field(ge=0, le=100)

    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    reasoning_summary: str = Field(
        description="Short explanation why the role is target/adjacent/off-target and why fit is high/medium/low."
    )