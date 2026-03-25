from __future__ import annotations

import re
from typing import Dict, List

from .utils import dedupe_preserve_order, normalize_spaces

HEADING_RESP = [
    "responsibilities",
    "your responsibilities",
    "what you will do",
    "pareigos",
    "atsakomybės",
    "darbo pobūdis",
    "ką darysite",
]
HEADING_REQ = [
    "requirements",
    "must have",
    "we expect",
    "what you bring",
    "reikalavimai",
    "lūkesčiai",
    "ko tikimės",
]
HEADING_PREF = [
    "nice to have",
    "would be a plus",
    "bonus",
    "privalumas",
    "būtų privalumas",
]
KNOWN_SKILLS = [
    "python", "powershell", "sql", "rest api", "api", "webhooks", "oauth2", "jwt",
    "n8n", "zapier", "make", "github", "git", "javascript", "java", "php", "laravel",
    "salesforce", "servicenow", "jira", "intune", "entra id", "microsoft 365",
    "windows server", "active directory", "dns", "dhcp", "linux", "docker", "excel",
]
LANG_KEYWORDS = {
    "english": ["english", "anglų", "anglu"],
    "lithuanian": ["lithuanian", "lietuvių", "lietuviu"],
    "russian": ["russian", "rusų", "rusu"],
}


def _split_lines(text: str) -> List[str]:
    return [line.strip(" -•\t") for line in normalize_spaces(text).splitlines() if line.strip()]


def _find_value_after_prefix(lines: List[str], prefixes: List[str]) -> str:
    for line in lines:
        lowered = line.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                value = line.split(":", 1)[1].strip() if ":" in line else line[len(prefix):].strip()
                if value:
                    return value
    return ""


def _find_role_title(lines: List[str]) -> str:
    blacklist = ("company", "įmonė", "location", "salary", "atlyginimas")
    for line in lines[:8]:
        lowered = line.lower()
        if len(line) > 100:
            continue
        if any(lowered.startswith(x) for x in blacklist):
            continue
        if any(token in lowered for token in ["vilnius", "kaunas", "remote", "hybrid", "gross", "net"]):
            continue
        return line
    return ""


def _extract_salary(text: str) -> Dict[str, object]:
    patterns = [
        r"(?P<min>\d[\d\s]{2,})\s*[-–]\s*(?P<max>\d[\d\s]{2,})\s*€?\s*(?P<type>gross|net|bruto|į rankas|i rankas)?",
        r"€\s*(?P<min>\d[\d\s]{2,})\s*[-–]\s*€?\s*(?P<max>\d[\d\s]{2,})\s*(?P<type>gross|net|bruto|į rankas|i rankas)?",
    ]
    lowered = text.lower()
    for pattern in patterns:
        m = re.search(pattern, lowered, re.IGNORECASE)
        if m:
            min_v = int(re.sub(r"\s+", "", m.group("min")))
            max_v = int(re.sub(r"\s+", "", m.group("max")))
            salary_type = m.group("type") or ""
            salary_type = {"bruto": "gross", "į rankas": "net", "i rankas": "net"}.get(salary_type, salary_type)
            return {
                "min": min_v,
                "max": max_v,
                "currency": "EUR",
                "gross_or_net": salary_type,
            }
    return {"min": None, "max": None, "currency": "", "gross_or_net": ""}


def _extract_work_mode(text: str) -> str:
    lowered = text.lower()
    if any(x in lowered for x in ["hybrid", "hibrid", "3 dienas", "2 days in office"]):
        return "hybrid"
    if any(x in lowered for x in ["remote", "nuotol", "work from home"]):
        return "remote"
    if any(x in lowered for x in ["on-site", "onsite", "office only", "biure", "ofise"]):
        return "on-site"
    return "unknown"


def _extract_location(text: str, lines: List[str]) -> str:
    direct = _find_value_after_prefix(lines, ["location", "miestas", "vieta"])
    if direct:
        return direct
    for city in ["Vilnius", "Kaunas", "Klaipėda", "Klaipeda", "Remote"]:
        if city.lower() in text.lower():
            return city
    return ""


def _parse_section(lines: List[str], heading_keywords: List[str]) -> List[str]:
    results: List[str] = []
    active = False
    for line in lines:
        lowered = line.lower()
        if any(lowered == h or lowered.startswith(h + ":") for h in heading_keywords):
            active = True
            continue
        if active and any(lowered == h or lowered.startswith(h + ":") for h in HEADING_RESP + HEADING_REQ + HEADING_PREF):
            break
        if active:
            if len(line) < 2:
                continue
            results.append(line)
    return results


def _extract_skills(text: str, section_items: List[str]) -> List[str]:
    if section_items:
        blob = " \n ".join(section_items).lower()
    else:
        blob = text.lower()
    found = []
    for skill in KNOWN_SKILLS:
        if skill in blob:
            found.append(skill)
    return dedupe_preserve_order(found)


def _extract_languages(text: str) -> List[str]:
    lowered = text.lower()
    result = []
    for lang, variants in LANG_KEYWORDS.items():
        if any(v in lowered for v in variants):
            result.append(lang)
    return result


def _extract_experience_requirements(text: str) -> str:
    patterns = [
        r"\d\+?\s+years? of experience",
        r"mažiausiai \d\+?\s+met",
        r"minimum \d\+?\s+year",
        r"\d\+?\s+metų patirt",
    ]
    lowered = text.lower()
    for pattern in patterns:
        m = re.search(pattern, lowered)
        if m:
            return m.group(0)
    return ""


def _extract_domain(text: str) -> str:
    lowered = text.lower()
    if "automation" in lowered or "automatiz" in lowered:
        if "ai" in lowered or "dirbtinio intelekto" in lowered or "llm" in lowered:
            return "ai_process_automation"
        return "automation"
    if "integration" in lowered or "integrac" in lowered or "api" in lowered:
        return "integration"
    if "cloud" in lowered:
        return "cloud_ops"
    if "support" in lowered or "incident" in lowered:
        return "support_ops"
    return "general_it"


def _extract_seniority_hints(text: str) -> List[str]:
    lowered = text.lower()
    hints = []
    for keyword in ["architecture", "architectural", "ownership", "own", "lead", "senior", "production", "vendor", "requirements"]:
        if keyword in lowered:
            hints.append(keyword)
    return dedupe_preserve_order(hints)


def extract_job_structured(text: str) -> Dict[str, object]:
    text = normalize_spaces(text)
    lines = _split_lines(text)

    role_title = _find_role_title(lines)
    company = _find_value_after_prefix(lines, ["company", "įmonė", "imone"])
    location = _extract_location(text, lines)
    work_mode = _extract_work_mode(text)
    salary = _extract_salary(text)

    responsibilities = _parse_section(lines, HEADING_RESP)
    required = _parse_section(lines, HEADING_REQ)
    preferred = _parse_section(lines, HEADING_PREF)

    required_skills = _extract_skills(text, required)
    preferred_skills = _extract_skills(text, preferred)
    tools_and_platforms = _extract_skills(text, responsibilities + required + preferred)

    red_flags = []
    lowered = text.lower()
    if "php" in lowered and "laravel" in lowered:
        red_flags.append("php_laravel_required_or_relevant")
    if "salesforce" in lowered:
        red_flags.append("salesforce_present")

    return {
        "company": company,
        "role_title": role_title,
        "location": location,
        "work_mode": work_mode,
        "salary": salary,
        "employment_type": "full-time" if "full-time" in lowered or "full time" in lowered else "",
        "responsibilities": responsibilities,
        "required_skills": required_skills,
        "preferred_skills": preferred_skills,
        "tools_and_platforms": tools_and_platforms,
        "experience_requirements": _extract_experience_requirements(text),
        "language_requirements": _extract_languages(text),
        "education_requirements": "",
        "domain": _extract_domain(text),
        "seniority_hints": _extract_seniority_hints(text),
        "red_flags": red_flags,
        "notes": "",
        "raw_text_excerpt": text[:2000],
    }
