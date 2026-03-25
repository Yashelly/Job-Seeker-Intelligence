from __future__ import annotations

from typing import Dict, List, Tuple


ALLOWED_WORK_MODES = {"remote", "hybrid", "on-site", "unknown"}


def validate_job_payload(job: Dict[str, object]) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    role_title = str(job.get("role_title", "")).strip()
    company = str(job.get("company", "")).strip()
    work_mode = str(job.get("work_mode", "")).strip()

    if not role_title:
        errors.append("role_title is missing")
    if not company:
        errors.append("company is missing")
    if work_mode and work_mode not in ALLOWED_WORK_MODES:
        errors.append(f"work_mode '{work_mode}' is invalid")

    responsibilities = job.get("responsibilities", [])
    required_skills = job.get("required_skills", [])
    tools_and_platforms = job.get("tools_and_platforms", [])

    if not any([responsibilities, required_skills, tools_and_platforms]):
        errors.append("at least one of responsibilities / required_skills / tools_and_platforms must exist")

    salary = job.get("salary", {}) or {}
    if isinstance(salary, dict):
        min_v = salary.get("min")
        max_v = salary.get("max")
        if min_v is not None and max_v is not None and int(min_v) > int(max_v):
            errors.append("salary min is greater than salary max")

    return len(errors) == 0, errors
