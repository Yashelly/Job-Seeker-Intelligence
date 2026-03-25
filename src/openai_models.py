from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class SalaryModel(BaseModel):
    min: Optional[int] = None
    max: Optional[int] = None
    currency: str = ""
    gross_or_net: str = ""


class JobExtractionModel(BaseModel):
    company: str = ""
    role_title: str = ""
    location: str = ""
    work_mode: str = "unknown"
    salary: SalaryModel = Field(default_factory=SalaryModel)
    employment_type: str = ""
    responsibilities: List[str] = Field(default_factory=list)
    required_skills: List[str] = Field(default_factory=list)
    preferred_skills: List[str] = Field(default_factory=list)
    tools_and_platforms: List[str] = Field(default_factory=list)
    experience_requirements: str = ""
    language_requirements: List[str] = Field(default_factory=list)
    education_requirements: str = ""
    domain: str = "general_it"
    seniority_hints: List[str] = Field(default_factory=list)
    red_flags: List[str] = Field(default_factory=list)
    notes: str = ""
    raw_text_excerpt: str = ""
