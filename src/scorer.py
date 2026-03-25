from __future__ import annotations

import re
from typing import Any, Dict, List, Set


SKILL_ALIASES = {
    "api integration": {"api", "rest api"},
    "rest api": {"api"},
    "entra id": {"azure ad"},
    "microsoft 365": {"m365", "office 365"},
    "azure": {"azure cloud"},
    "intune": {"microsoft intune"},
    "helpdesk": {"service desk", "technical support"},
}


TARGET_TITLE_KEYWORDS = {
    "it",
    "support",
    "administrator",
    "administration",
    "system administrator",
    "systems administrator",
    "admin",
    "cloud",
    "azure",
    "microsoft",
    "m365",
    "office 365",
    "intune",
    "entra",
    "defender",
    "windows",
    "linux",
    "network",
    "infrastructure",
    "infrastrukt",
    "service desk",
    "helpdesk",
    "technical support",
    "automation",
    "automatiz",
    "dirbtinio intelekto",
    "ai",
    "data",
    "analyst",
    "security",
    "devops",
    "platform",
    "process automation",
    "workflow",
    "integrations",
    "integration",
    "api",
}

ADJACENT_TITLE_KEYWORDS = {
    "operations",
    "project coordinator",
    "coordinator",
    "business systems",
    "erp",
    "crm",
    "application support",
    "platform",
    "digital transformation",
    "transformation",
    "process",
    "projekt",
    "manager",
    "vadov",
    "lead",
}

OFF_TARGET_TITLE_KEYWORDS = {
    "pardav",
    "sales",
    "sandėlio",
    "sandelio",
    "vairuotoj",
    "driver",
    "darbinink",
    "worker",
    "asfalt",
    "operator",
    "kasinink",
    "cashier",
    "padavėj",
    "padavej",
    "komplektuotoj",
    "picker",
    "krautuvo",
    "forklift",
    "statyb",
    "construction",
    "buhalter",
    "accountant",
    "virėj",
    "virej",
    "cook",
    "valytoj",
    "cleaner",
    "apsaug",
    "security guard",
    "warehouse",
    "logist",
    "retail",
    "nurse",
    "slaug",
}

TARGET_CATEGORY_KEYWORDS = {
    "informacin",
    "technolog",
    "it",
    "program",
    "telekom",
}

OFF_TARGET_CATEGORY_KEYWORDS = {
    "statyb",
    "gamyb",
    "sandėli",
    "sandeli",
    "prekyb",
    "maitin",
    "apskait",
    "transport",
    "logistik",
    "retail",
    "restoran",
    "medicin",
    "sveikat",
}

TARGET_TEXT_KEYWORDS = {
    "automation",
    "automatiz",
    "dirbtinio intelekto",
    "ai",
    "api",
    "integration",
    "integrations",
    "microsoft 365",
    "office 365",
    "m365",
    "intune",
    "entra",
    "azure",
    "cloud",
    "support",
    "incident",
    "documentation",
    "process",
    "workflow",
    "system",
    "infrastructure",
    "security",
    "endpoint",
}

MANUAL_LABOR_HINTS = {
    "asfalt",
    "krautuvo",
    "sandėlio",
    "sandelio",
    "statyb",
    "darbinink",
    "vairuotoj",
    "picker",
    "warehouse",
    "production line",
    "gamybos",
    "cash register",
    "pardavėj",
    "pardavej",
}


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(x) for x in value if x)
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _as_set(items: List[str]) -> Set[str]:
    base = {item.strip().lower() for item in items if item and item.strip()}
    expanded = set(base)
    for item in list(base):
        expanded.update(SKILL_ALIASES.get(item, set()))
    return expanded


def _profile_skill_sets(profile: Dict[str, Any]) -> Dict[str, Set[str]]:
    skills = profile.get("skills", {})
    return {
        "strong": _as_set(skills.get("strong", [])),
        "medium": _as_set(skills.get("medium", [])),
        "weak": _as_set(skills.get("weak_or_limited", [])),
    }


def _profile_target_tracks(profile: Dict[str, Any]) -> Set[str]:
    return {str(x).strip().lower() for x in profile.get("target_tracks", []) if str(x).strip()}


def _contains_any(text: str, keywords: Set[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _count_hits(text: str, keywords: Set[str]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def _collect_job_text(job: Dict[str, Any]) -> str:
    parts = [
        job.get("role_title", ""),
        job.get("company", ""),
        job.get("domain", ""),
        job.get("category", ""),
        job.get("location", ""),
        job.get("work_mode", ""),
        job.get("experience_requirements", ""),
        " ".join(job.get("responsibilities", []) or []),
        " ".join(job.get("required_skills", []) or []),
        " ".join(job.get("preferred_skills", []) or []),
        " ".join(job.get("tools_and_platforms", []) or []),
        " ".join(job.get("language_requirements", []) or []),
        " ".join(job.get("seniority_hints", []) or []),
        " ".join(job.get("red_flags", []) or []),
        job.get("raw_text", ""),
    ]
    return _normalize_text(" ".join(parts))


def classify_target_domain(job: Dict[str, Any], profile: Dict[str, Any]) -> str:
    title = _normalize_text(job.get("role_title"))
    category = _normalize_text(job.get("category"))
    domain = _normalize_text(job.get("domain"))
    text = _collect_job_text(job)
    target_tracks = _profile_target_tracks(profile)

    strong_target = (
        _contains_any(title, TARGET_TITLE_KEYWORDS)
        or _contains_any(category, TARGET_CATEGORY_KEYWORDS)
        or domain in target_tracks
        or _count_hits(text, TARGET_TEXT_KEYWORDS) >= 3
    )
    strong_off_target = (
        _contains_any(title, OFF_TARGET_TITLE_KEYWORDS)
        or _contains_any(category, OFF_TARGET_CATEGORY_KEYWORDS)
        or _count_hits(text, MANUAL_LABOR_HINTS) >= 2
    )

    if strong_target:
        if any(keyword in title for keyword in ("sales", "pardav")) and not _contains_any(title, {"it", "technology", "microsoft", "automation", "ai"}):
            return "adjacent"
        return "target"

    if strong_off_target:
        return "off_target"

    if _contains_any(title, ADJACENT_TITLE_KEYWORDS) or _count_hits(text, TARGET_TEXT_KEYWORDS) >= 1:
        return "adjacent"

    return "off_target"


def compute_relevance_score(job: Dict[str, Any], profile: Dict[str, Any], target_domain: str) -> int:
    title = _normalize_text(job.get("role_title"))
    category = _normalize_text(job.get("category"))
    domain = _normalize_text(job.get("domain"))
    text = _collect_job_text(job)
    target_tracks = _profile_target_tracks(profile)

    score = 0

    if target_domain == "target":
        score += 45
    elif target_domain == "adjacent":
        score += 25
    else:
        score += 5

    score += min(25, _count_hits(title, TARGET_TITLE_KEYWORDS) * 12)
    score += min(15, _count_hits(category, TARGET_CATEGORY_KEYWORDS) * 10)
    score += min(18, _count_hits(text, TARGET_TEXT_KEYWORDS) * 3)

    if domain in target_tracks:
        score += 12
    elif domain == "general_it":
        score += 8

    score -= min(60, _count_hits(title, OFF_TARGET_TITLE_KEYWORDS) * 25)
    score -= min(30, _count_hits(category, OFF_TARGET_CATEGORY_KEYWORDS) * 18)
    score -= min(25, _count_hits(text, MANUAL_LABOR_HINTS) * 10)

    raw_text = _normalize_text(job.get("raw_text"))
    if len(raw_text) < 220 and target_domain != "target":
        score -= 15
    elif len(raw_text) < 220 and target_domain == "target":
        score -= 5

    return max(0, min(100, score))


def _skill_overlap(required_skills: Set[str], preferred_skills: Set[str], tools: Set[str], skill_sets: Dict[str, Set[str]]) -> int:
    strong = skill_sets["strong"]
    medium = skill_sets["medium"]
    weak = skill_sets["weak"]

    score = 15
    for skill in required_skills:
        if skill in strong:
            score += 10
        elif skill in medium:
            score += 6
        elif skill in weak:
            score += 2

    for skill in preferred_skills | tools:
        if skill in strong:
            score += 4
        elif skill in medium:
            score += 2
        elif skill in weak:
            score += 1

    required_total = max(len(required_skills), 1)
    matched_required = len(required_skills & (strong | medium | weak))
    proportional = round((matched_required / required_total) * 25)
    score += proportional

    return max(0, min(100, score))


def compute_fit_score(job: Dict[str, Any], profile: Dict[str, Any], target_domain: str) -> tuple[int, Dict[str, int], List[str], List[str], List[str]]:
    skill_sets = _profile_skill_sets(profile)
    all_profile_skills = skill_sets["strong"] | skill_sets["medium"] | skill_sets["weak"]

    required_skills = _as_set(job.get("required_skills", []))
    preferred_skills = _as_set(job.get("preferred_skills", []))
    tools = _as_set(job.get("tools_and_platforms", []))

    title = _normalize_text(job.get("role_title"))
    location = _normalize_text(job.get("location"))
    work_mode = _normalize_text(job.get("work_mode") or "unknown")
    experience_requirements = _normalize_text(job.get("experience_requirements"))
    seniority_hints = _normalize_text(job.get("seniority_hints"))
    text = _collect_job_text(job)
    domain = _normalize_text(job.get("domain"))

    blockers: List[str] = []
    strengths: List[str] = []
    gaps: List[str] = []

    skill_overlap_score = _skill_overlap(required_skills, preferred_skills, tools, skill_sets)

    responsibility_fit_score = 20
    for keyword, bonus in {
        "documentation": 10,
        "process": 10,
        "automation": 14,
        "api": 12,
        "integration": 12,
        "support": 8,
        "incident": 8,
        "microsoft 365": 8,
        "intune": 8,
        "entra": 8,
        "azure": 8,
    }.items():
        if keyword in text:
            responsibility_fit_score += bonus
    responsibility_fit_score = min(responsibility_fit_score, 100)

    seniority_fit_score = 80
    blocker_penalty = 0

    years_match = re.search(r"(\d)\s*(?:\+|–|-)?\s*(\d+)?\s*(?:years?|met)", experience_requirements)
    if years_match:
        years = int(years_match.group(1))
        if years >= 4:
            seniority_fit_score -= 28
            blocker_penalty += 12
            blockers.append("4+ years requirement")
        elif years >= 3:
            seniority_fit_score -= 22
            blocker_penalty += 10
            blockers.append("3+ years requirement")
        elif years >= 2:
            seniority_fit_score -= 12
            blocker_penalty += 5
            gaps.append(experience_requirements)

    heavy_seniority = {
        "architecture": 10,
        "ownership": 10,
        "lead": 10,
        "production": 6,
        "vendor": 4,
        "head": 12,
        "vadovas": 14,
        "manager": 8,
        "strategy": 6,
        "stakeholder": 6,
        "project management": 6,
    }
    for heavy, penalty in heavy_seniority.items():
        if heavy in seniority_hints or heavy in title or heavy in text:
            seniority_fit_score -= penalty
            blocker_penalty += max(2, penalty // 4)

    preferred_modes = {str(x).strip().lower() for x in profile.get("constraints", {}).get("preferred_work_mode", [])}
    if work_mode == "on-site" and "on-site" not in preferred_modes:
        blocker_penalty += 8
        blockers.append("on-site work mode")
    if "kaunas" in location and work_mode == "on-site":
        blocker_penalty += 6
        blockers.append("on-site Kaunas")

    red_flags = [_normalize_text(x) for x in job.get("red_flags", [])]
    for rf in red_flags:
        if "php_laravel" in rf or "laravel" in rf:
            blocker_penalty += 10
            blockers.append("PHP/Laravel relevance")
        if "salesforce" in rf:
            blocker_penalty += 6
            blockers.append("Salesforce relevance")

    differentiator_bonus = 0
    differentiators: List[str] = []
    for keyword, label, bonus in [
        ("automation", "automation relevance", 6),
        ("api", "API relevance", 5),
        ("process", "process analysis relevance", 4),
        ("documentation", "documentation relevance", 3),
        ("microsoft 365", "Microsoft 365 background", 3),
        ("intune", "Intune background", 3),
        ("entra", "Entra ID background", 3),
        ("azure", "Azure background", 3),
        ("zapier", "workflow tools exposure", 3),
        ("make", "workflow tools exposure", 3),
    ]:
        if keyword in text:
            differentiator_bonus += bonus
            differentiators.append(label)
    differentiator_bonus = min(differentiator_bonus, 18)

    if target_domain == "off_target":
        seniority_fit_score = min(seniority_fit_score, 45)

    fit_score = round(
        skill_overlap_score * 0.45
        + responsibility_fit_score * 0.20
        + max(seniority_fit_score, 0) * 0.20
        + differentiator_bonus
        - blocker_penalty
    )
    fit_score = max(0, min(100, fit_score))

    matched_required = len(required_skills & all_profile_skills)
    if matched_required > 0:
        strengths.append(f"Matched {matched_required}/{len(required_skills) or 1} required skills")
    if domain in _profile_target_tracks(profile):
        strengths.append(f"Domain aligns with target track: {domain}")
    for label in sorted(set(differentiators))[:2]:
        strengths.append(label)

    if target_domain == "target":
        strengths.append("Title/category align with target domain")

    if "Salesforce relevance" in blockers:
        gaps.append("Salesforce-related requirement")
    if "PHP/Laravel relevance" in blockers:
        gaps.append("PHP/Laravel-related requirement")
    if work_mode == "on-site":
        gaps.append("On-site requirement")
    for skill in sorted(required_skills - all_profile_skills):
        gaps.append(f"Missing required skill: {skill}")
        if len(gaps) >= 3:
            break

    breakdown = {
        "skill_overlap_score": skill_overlap_score,
        "responsibility_fit_score": responsibility_fit_score,
        "seniority_fit_score": max(seniority_fit_score, 0),
        "differentiator_bonus": differentiator_bonus,
        "blocker_penalty": blocker_penalty,
    }

    return fit_score, breakdown, strengths[:3], gaps[:3], list(dict.fromkeys(blockers))


def build_decision(target_domain: str, relevance_score: int, fit_score: int) -> tuple[int, str]:
    if target_domain == "off_target":
        final_score = min(35, round(relevance_score * 0.7 + fit_score * 0.3))
        return final_score, "skip"

    if target_domain == "adjacent":
        final_score = round(relevance_score * 0.58 + fit_score * 0.42)
    else:
        final_score = round(relevance_score * 0.65 + fit_score * 0.35)

    if relevance_score >= 78 and fit_score >= 58:
        decision = "apply"
    elif relevance_score >= 62 and fit_score >= 30:
        decision = "stretch"
    else:
        decision = "skip"

    return max(0, min(100, final_score)), decision


def score_job(job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    target_domain = classify_target_domain(job, profile)
    relevance_score = compute_relevance_score(job, profile, target_domain)
    fit_score, fit_breakdown, strengths, gaps, blockers = compute_fit_score(job, profile, target_domain)
    final_score, decision = build_decision(target_domain, relevance_score, fit_score)

    title = _normalize_text(job.get("role_title"))
    category = _normalize_text(job.get("category"))
    if (
        _contains_any(title, OFF_TARGET_TITLE_KEYWORDS)
        or _contains_any(category, OFF_TARGET_CATEGORY_KEYWORDS)
        or (_count_hits(_collect_job_text(job), MANUAL_LABOR_HINTS) >= 2 and target_domain != "target")
    ):
        decision = "skip"
        final_score = min(final_score, 35)

    breakdown = {
        "target_domain": target_domain,
        "relevance_score": relevance_score,
        "fit_score": fit_score,
        "domain_relevance_score": relevance_score,
        **fit_breakdown,
    }

    return {
        "target_domain": target_domain,
        "relevance_score": relevance_score,
        "fit_score": fit_score,
        "score": max(0, min(100, final_score)),
        "decision": decision,
        "breakdown": breakdown,
        "top_strengths": strengths[:3],
        "top_gaps": gaps[:3],
        "blockers": blockers,
    }
