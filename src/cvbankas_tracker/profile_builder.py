from __future__ import annotations

import json
import os
from pathlib import Path

from .ai_cli import AICLIError, parse_json_response, run_claude_cli, run_codex_cli

CV_TEXT_LIMIT = 24000
# Upper bound for a CV upload accepted over the web dashboard. Real resumes are
# far smaller; the cap stops a huge file from being buffered into memory before
# the prompt itself is truncated to CV_TEXT_LIMIT.
MAX_CV_UPLOAD_BYTES = 5 * 1024 * 1024
SUPPORTED_CV_SUFFIXES = (".txt", ".md", ".pdf", ".docx")
AI_PROFILE_BACKENDS = ("openai", "claude_cli", "codex_cli")

PROFILE_SYSTEM_PROMPT = (
    "You build a structured job-seeker profile from a candidate's CV / resume text. "
    "Infer the fields from the CV; do not invent skills the CV does not support. "
    "Return a single JSON object with exactly these keys: "
    "name (string), target_roles (list of job titles the candidate should apply for), "
    "skills (list of concrete technical/professional skills, lowercase), "
    "preferred_locations (list; include 'Remote' if the CV suggests remote work), "
    "experience_level (one of 'Junior', 'Mid', 'Senior', or 'Lead'), "
    "years_of_experience (integer or null), "
    "salary_expectation (string or null), "
    "must_have_skills (list; the candidate's strongest core skills, lowercase), "
    "nice_to_have_skills (list, lowercase), "
    "excluded_keywords (list of unrelated job types the candidate should avoid, lowercase), "
    "additional_keywords (list of extra search keywords/domains, lowercase). "
    "Keep list items short. Use empty lists or null when unknown."
)

_LIST_FIELDS = (
    "target_roles",
    "skills",
    "preferred_locations",
    "must_have_skills",
    "nice_to_have_skills",
    "excluded_keywords",
    "additional_keywords",
)


class ProfileGenerationError(RuntimeError):
    """Raised when a profile cannot be generated from a CV."""


def extract_cv_text(path: str | Path) -> str:
    """Read a CV file (.txt/.md/.pdf/.docx) and return its plain text."""
    cv_path = Path(path)
    if not cv_path.exists():
        raise ProfileGenerationError(f"CV file not found: {cv_path}")

    suffix = cv_path.suffix.lower()
    if suffix in {".txt", ".md"}:
        text = cv_path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".pdf":
        text = _extract_pdf_text(cv_path)
    elif suffix == ".docx":
        text = _extract_docx_text(cv_path)
    else:
        supported = ", ".join(SUPPORTED_CV_SUFFIXES)
        raise ProfileGenerationError(
            f"Unsupported CV format '{suffix or '(none)'}'. Supported: {supported}."
        )

    text = text.strip()
    if not text:
        raise ProfileGenerationError(f"No readable text found in CV file: {cv_path}")
    return text


def _extract_pdf_text(cv_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ProfileGenerationError(
            "Reading PDF CVs requires the 'pypdf' package (pip install pypdf)."
        ) from exc

    try:
        reader = PdfReader(str(cv_path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        raise ProfileGenerationError(f"Failed to read PDF CV: {exc}") from exc


def _extract_docx_text(cv_path: Path) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ProfileGenerationError(
            "Reading DOCX CVs requires the 'python-docx' package (pip install python-docx)."
        ) from exc

    try:
        document = docx.Document(str(cv_path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception as exc:
        raise ProfileGenerationError(f"Failed to read DOCX CV: {exc}") from exc


def build_profile_prompt(cv_text: str) -> str:
    return (
        f"{PROFILE_SYSTEM_PROMPT}\n\n"
        "CV TEXT:\n"
        f"{cv_text[:CV_TEXT_LIMIT]}\n\n"
        "Respond with ONLY the JSON object, no prose and no markdown fences."
    )


def _clean_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        cleaned.append(text)
    return cleaned


def _clean_years(value: object) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        years = float(value)
    except (TypeError, ValueError):
        return None
    years = max(0.0, years)
    # Keep whole years as int (so the UI shows "6" not "6.0"); preserve
    # sub-year experience as a rounded fraction (e.g. 0.8) for near-match scoring.
    return int(years) if years.is_integer() else round(years, 1)


def normalize_profile_data(data: dict[str, object]) -> dict[str, object]:
    """Coerce raw model output into a complete, schema-valid profile dict."""
    name = str(data.get("name", "")).strip() or "Candidate"
    experience_level = str(data.get("experience_level", "")).strip() or "Mid"

    salary_raw = data.get("salary_expectation")
    salary = str(salary_raw).strip() if salary_raw not in (None, "") else None

    profile: dict[str, object] = {
        "name": name,
        "experience_level": experience_level,
        "years_of_experience": _clean_years(data.get("years_of_experience")),
        "salary_expectation": salary,
    }
    for field_name in _LIST_FIELDS:
        profile[field_name] = _clean_list(data.get(field_name))
    return profile


def _run_openai_profile(cv_text: str, model: str | None) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    completion = client.chat.completions.create(
        model=model or "gpt-4.1-mini",
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": PROFILE_SYSTEM_PROMPT},
            {"role": "user", "content": f"CV TEXT:\n{cv_text[:CV_TEXT_LIMIT]}"},
        ],
    )
    return completion.choices[0].message.content or "{}"


def generate_profile_dict(
    cv_text: str,
    *,
    backend: str,
    openai_model: str | None = None,
    claude_model: str | None = None,
    codex_model: str | None = None,
) -> dict[str, object]:
    """Generate a normalized profile dict from CV text using an AI backend."""
    if backend not in AI_PROFILE_BACKENDS:
        supported = ", ".join(AI_PROFILE_BACKENDS)
        raise ProfileGenerationError(
            "Building a profile from a CV requires an AI backend "
            f"({supported}). Current backend: '{backend}'. "
            "Set AI_BACKEND or OPENAI_API_KEY and try again."
        )

    # Convert backend-specific failures (missing key, CLI not on PATH, timeout,
    # unparseable output) into ProfileGenerationError so callers — including the
    # web dashboard — can show a clear message instead of a 500.
    if backend == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise ProfileGenerationError(
                "The OpenAI backend needs an API key. Set OPENAI_API_KEY, or "
                "switch the backend to claude_cli or codex_cli in Settings."
            )
        try:
            raw = _run_openai_profile(cv_text, openai_model)
        except Exception as exc:  # openai SDK / network / auth errors
            raise ProfileGenerationError(f"OpenAI request failed: {exc}") from exc
    elif backend == "claude_cli":
        try:
            raw = run_claude_cli(build_profile_prompt(cv_text), model=claude_model)
        except AICLIError as exc:
            raise ProfileGenerationError(str(exc)) from exc
    else:
        try:
            raw = run_codex_cli(build_profile_prompt(cv_text), model=codex_model)
        except AICLIError as exc:
            raise ProfileGenerationError(str(exc)) from exc

    try:
        parsed = parse_json_response(raw)
    except AICLIError as exc:
        raise ProfileGenerationError(str(exc)) from exc
    return normalize_profile_data(parsed)


def write_profile_json(path: str | Path, profile: dict[str, object]) -> Path:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def build_profile_from_cv(
    cv_path: str | Path,
    *,
    backend: str,
    openai_model: str | None = None,
    claude_model: str | None = None,
    codex_model: str | None = None,
) -> dict[str, object]:
    """Convenience: read a CV file and return a normalized profile dict."""
    cv_text = extract_cv_text(cv_path)
    return generate_profile_dict(
        cv_text,
        backend=backend,
        openai_model=openai_model,
        claude_model=claude_model,
        codex_model=codex_model,
    )
