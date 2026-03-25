from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import sys
from datetime import datetime
from typing import Any, Dict, Tuple

from .config_loader import load_yaml_config
from .cvbankas import collect_listing_links, fetch_and_parse_job
from .extraction_service import extract_job_with_strategy
from .generator import build_cover_letter, build_summary
from .ingest import ingest
from .match_service import evaluate_job_match
from .profile_loader import load_profile
from .storage import init_db, save_run
from .utils import to_pretty_json
from .validator import validate_job_payload


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
GRAY = "\033[90m"


DEFAULT_PROFILE = "profile/user_profile.yaml"
DEFAULT_DB = "data/app.db"
DEFAULT_EXTRACTOR = "auto"
DEFAULT_CVBANKAS_START_URL = "https://www.cvbankas.lt/"
DEFAULT_CVBANKAS_MAX_PAGES = 3
DEFAULT_CVBANKAS_LIMIT = 20
DEFAULT_CVBANKAS_DELAY_SECONDS = 0.0
DEFAULT_MATCH_EVALUATOR = "openai"


def color_decision(decision: str) -> str:
    mapping = {
        "apply": GREEN,
        "stretch": YELLOW,
        "skip": RED,
    }
    return f"{mapping.get(decision, RESET)}{decision}{RESET}"


def color_domain(domain: str) -> str:
    mapping = {
        "target": CYAN,
        "adjacent": BLUE,
        "off_target": GRAY,
    }
    return f"{mapping.get(domain, RESET)}{domain}{RESET}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Job Application Agent Scaffold")

    parser.add_argument("--config", help="Path to YAML config")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--text", help="Inline vacancy text")
    group.add_argument("--text-file", help="Path to vacancy text file")
    group.add_argument("--url", help="Vacancy URL")
    group.add_argument(
        "--cvbankas",
        action="store_true",
        help="Collect CVBankas vacancy links and process them in batch mode",
    )

    parser.add_argument("--profile", default=None, help="Path to YAML profile")
    parser.add_argument("--db", default=None, help="Path to SQLite DB")
    parser.add_argument(
        "--extractor",
        default=None,
        choices=["auto", "heuristic", "openai"],
        help="Extraction strategy",
    )
    parser.add_argument(
        "--openai-model",
        default=None,
        help="OpenAI model for structured extraction and/or match evaluation, e.g. gpt-4o-mini",
    )
    parser.add_argument(
        "--cvbankas-start-url",
        default=None,
        help="CVBankas listing URL to start from",
    )
    parser.add_argument(
        "--cvbankas-max-pages",
        type=int,
        default=None,
        help="How many CVBankas listing pages to crawl",
    )
    parser.add_argument(
        "--cvbankas-limit",
        type=int,
        default=None,
        help="Maximum number of CVBankas vacancy detail pages to process",
    )
    parser.add_argument(
        "--cvbankas-delay-seconds",
        type=float,
        default=None,
        help="Delay between CVBankas listing page requests",
    )
    parser.add_argument(
        "--match-evaluator",
        choices=["openai", "fallback"],
        default=None,
        help="How to evaluate vacancy match",
    )
    return parser.parse_args()


def _cfg_get(cfg: dict, *keys, default=None):
    cur = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _pick_scalar(primary: Any, fallback: Any) -> Any:
    if primary is None:
        return fallback
    if isinstance(primary, str):
        return primary if primary.strip() else fallback
    return primary if primary else fallback


def _pick_list(primary: Any, fallback: Any) -> list[Any]:
    if isinstance(primary, list) and primary:
        return primary
    if isinstance(fallback, list):
        return fallback
    return []


def _merge_notes(*notes: str) -> str:
    parts = [part.strip() for part in notes if part and str(part).strip()]
    return " | ".join(parts)


def merge_cvbankas_job(cv_job: Dict[str, Any], extracted_job: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(extracted_job)

    for key in [
        "source",
        "source_url",
        "external_id",
        "company",
        "role_title",
        "location",
        "salary",
        "employment_type",
        "benefits",
        "raw_text_excerpt",
    ]:
        merged[key] = _pick_scalar(cv_job.get(key), merged.get(key))

    merged["work_mode"] = _pick_scalar(extracted_job.get("work_mode"), cv_job.get("work_mode"))
    merged["responsibilities"] = _pick_list(extracted_job.get("responsibilities"), cv_job.get("responsibilities"))
    merged["required_skills"] = _pick_list(extracted_job.get("required_skills"), cv_job.get("required_skills"))
    merged["preferred_skills"] = _pick_list(extracted_job.get("preferred_skills"), cv_job.get("preferred_skills"))
    merged["tools_and_platforms"] = _pick_list(extracted_job.get("tools_and_platforms"), cv_job.get("tools_and_platforms"))
    merged["language_requirements"] = _pick_list(
        extracted_job.get("language_requirements"), cv_job.get("language_requirements")
    )
    merged["seniority_hints"] = _pick_list(extracted_job.get("seniority_hints"), cv_job.get("seniority_hints"))
    merged["red_flags"] = _pick_list(extracted_job.get("red_flags"), cv_job.get("red_flags"))
    merged["notes"] = _merge_notes(str(extracted_job.get("notes", "")), str(cv_job.get("notes", "")))
    merged["raw_text"] = _pick_scalar(cv_job.get("raw_text"), extracted_job.get("raw_text"))

    for key in ["experience_requirements", "education_requirements", "domain", "category"]:
        merged[key] = _pick_scalar(extracted_job.get(key), cv_job.get(key))

    return merged


def process_single_job(
    *,
    job: Dict[str, Any],
    profile: Dict[str, Any],
    db_path: str,
    source_type: str,
    source_ref: str,
    match_evaluator: str = DEFAULT_MATCH_EVALUATOR,
    openai_model: str | None = None,
) -> Tuple[int, Dict[str, Any]]:
    job_to_save = dict(job)
    job_to_save.pop("raw_text", None)

    ok, errors = validate_job_payload(job_to_save)
    if not ok:
        raise ValueError("; ".join(errors))

    score_result = evaluate_job_match(
        job=job_to_save,
        profile=profile,
        strategy=match_evaluator,
        openai_model=openai_model,
    )
    summary = build_summary(job_to_save, score_result)
    cover_letter = build_cover_letter(job_to_save, score_result, profile)

    run_id = save_run(
        db_path=db_path,
        source_type=source_type,
        source_ref=source_ref,
        job=job_to_save,
        score_result=score_result,
        summary=summary,
        cover_letter=cover_letter,
    )
    return run_id, score_result


def build_cvbankas_job_for_scoring(
    *,
    cv_job: Dict[str, Any],
    extractor: str,
    openai_model: str | None,
) -> Dict[str, Any]:
    raw_text = str(cv_job.get("raw_text") or cv_job.get("raw_text_excerpt") or "").strip()
    if extractor == "heuristic" or not raw_text:
        return cv_job

    extracted_job = extract_job_with_strategy(raw_text, strategy=extractor, openai_model=openai_model)
    return merge_cvbankas_job(cv_job, extracted_job)


def run_single_input(args: argparse.Namespace) -> int:
    content = ingest(source_text=args.text, source_file=args.text_file, source_url=args.url)
    profile = load_profile(args.profile)

    job = extract_job_with_strategy(
        content.raw_text,
        strategy=args.extractor,
        openai_model=args.openai_model,
    )

    init_db(args.db)
    run_id, score_result = process_single_job(
        job=job,
        profile=profile,
        db_path=args.db,
        source_type=content.source_type,
        source_ref=content.source_ref,
        match_evaluator=args.match_evaluator,
        openai_model=args.openai_model,
    )

    print("=== EXTRACTED JOB ===")
    print(to_pretty_json({k: v for k, v in job.items() if k != "raw_text"}))
    print("\n=== SCORE RESULT ===")
    print(to_pretty_json(score_result))
    print("\n=== SUMMARY ===")
    print(build_summary({k: v for k, v in job.items() if k != "raw_text"}, score_result))
    print("\n=== COVER LETTER DRAFT ===")
    print(build_cover_letter({k: v for k, v in job.items() if k != "raw_text"}, score_result, profile))
    print(f"\nSaved run id: {run_id}")

    return 0


def run_cvbankas_batch(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    init_db(args.db)

    links = collect_listing_links(
        start_url=args.cvbankas_start_url,
        max_pages=args.cvbankas_max_pages,
        delay_seconds=args.cvbankas_delay_seconds,
    )
    if args.cvbankas_limit and args.cvbankas_limit > 0:
        links = links[: args.cvbankas_limit]

    if not links:
        print("No CVBankas vacancy links collected.", file=sys.stderr)
        return 2

    print(
        f"[{ts()}] CVBankas batch started | start_url={args.cvbankas_start_url} | "
        f"max_pages={args.cvbankas_max_pages} | limit={args.cvbankas_limit} | "
        f"extractor={args.extractor} | match_evaluator={args.match_evaluator}"
    )

    success_count = 0
    failed_count = 0

    for index, url in enumerate(links, start=1):
        try:
            cv_job = fetch_and_parse_job(url)
            job = build_cvbankas_job_for_scoring(
                cv_job=cv_job,
                extractor=args.extractor,
                openai_model=args.openai_model,
            )
            run_id, score_result = process_single_job(
                job=job,
                profile=profile,
                db_path=args.db,
                source_type="cvbankas",
                source_ref=url,
                match_evaluator=args.match_evaluator,
                openai_model=args.openai_model,
            )
            success_count += 1

            domain_plain = score_result.get("target_domain", "unknown")
            decision_plain = score_result.get("decision", "skip")

            print(
                f"[{ts()}] [{index}/{len(links)}] "
                f"[rel={score_result.get('relevance_score', 0):>3} "
                f"fit={score_result.get('fit_score', 0):>3} "
                f"final={score_result.get('score', 0):>3}] | "
                f"{color_domain(domain_plain)} | "
                f"{color_decision(decision_plain)} | "
                f"{job.get('role_title', '')} @ {job.get('company', '')} | run_id={run_id}"
            )

        except Exception as exc:  # noqa: BLE001 - batch mode should continue
            failed_count += 1
            print(f"[{ts()}] [{index}/{len(links)}] ERROR | {url} | {exc}", file=sys.stderr)

    print(
        f"\n[{ts()}] CVBankas batch finished. "
        f"processed={len(links)} saved={success_count} failed={failed_count} db={args.db}"
    )
    return 0 if success_count > 0 else 2


def main() -> int:
    args = parse_args()
    cfg = load_yaml_config(args.config) if getattr(args, "config", None) else {}

    args.profile = args.profile or cfg.get("profile") or DEFAULT_PROFILE
    args.db = args.db or cfg.get("db") or DEFAULT_DB
    args.extractor = args.extractor or cfg.get("extractor") or DEFAULT_EXTRACTOR
    args.openai_model = args.openai_model or cfg.get("openai_model") or None
    args.match_evaluator = args.match_evaluator or cfg.get("match_evaluator") or DEFAULT_MATCH_EVALUATOR

    source_type = _cfg_get(cfg, "source", "type")
    if args.cvbankas or source_type == "cvbankas":
        args.cvbankas = True
        args.cvbankas_start_url = args.cvbankas_start_url or _cfg_get(
            cfg, "source", "start_url", default=DEFAULT_CVBANKAS_START_URL
        )
        args.cvbankas_max_pages = args.cvbankas_max_pages or _cfg_get(
            cfg, "source", "max_pages", default=DEFAULT_CVBANKAS_MAX_PAGES
        )
        args.cvbankas_limit = args.cvbankas_limit or _cfg_get(
            cfg, "source", "limit", default=DEFAULT_CVBANKAS_LIMIT
        )
        args.cvbankas_delay_seconds = (
            args.cvbankas_delay_seconds
            if args.cvbankas_delay_seconds is not None
            else _cfg_get(cfg, "source", "delay_seconds", default=DEFAULT_CVBANKAS_DELAY_SECONDS)
        )
        return run_cvbankas_batch(args)

    args.text = args.text or _cfg_get(cfg, "input", "text")
    args.text_file = args.text_file or _cfg_get(cfg, "input", "text_file")
    args.url = args.url or _cfg_get(cfg, "input", "url")

    if not any([args.text, args.text_file, args.url]):
        raise SystemExit(
            "Provide one of --text/--text-file/--url, or use --config with source.type=cvbankas or input.*"
        )

    return run_single_input(args)


if __name__ == "__main__":
    raise SystemExit(main())
