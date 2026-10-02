from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cvbankas_tracker.analysis import (  # noqa: E402
    AIBasedAnalysisStrategy,
    RuleBasedAnalysisStrategy,
    VacancyAnalysisService,
    _calculate_rule_based_components,
    _detect_vacancy_work_mode,
    _infer_vacancy_seniority,
    _work_mode_match_score,
    normalize_analysis_result,
)
from src.cvbankas_tracker.io_utils import ProfileFileReader  # noqa: E402
from src.cvbankas_tracker.main import (  # noqa: E402
    SourceBatchResult,
    _collection_terminal_status,
    _execute_source_batches,
)
from src.cvbankas_tracker.models import (  # noqa: E402
    AnalysisMethod,
    FitLabel,
    InboxPreferences,
    UserProfile,
    Vacancy,
    WorkMode,
)
from src.cvbankas_tracker.parser import VacancyParser  # noqa: E402
from src.cvbankas_tracker.sources import build_source_registry  # noqa: E402
from src.cvbankas_tracker.storage import (  # noqa: E402
    DatabaseManager,
    canonicalize_source_url,
)

SOURCE_METADATA = {
    "cvbankas": {"type": "HTTP HTML", "type_group": "api_feed", "external": True},
    "cvmarket": {"type": "HTTP HTML + JSON-LD", "type_group": "api_feed", "external": True},
    "cvonline": {"type": "HTTP embedded JSON feed", "type_group": "api_feed", "external": True},
    "hh": {"type": "browser (configured); HTTP fallback", "type_group": "browser", "external": True},
    "justjoin": {"type": "HTTP HTML", "type_group": "api_feed", "external": True},
    "startup_jobs": {"type": "browser (configured); HTTP fallback", "type_group": "browser", "external": True},
    "euremotejobs": {"type": "browser (configured); HTTP fallback", "type_group": "browser", "external": True},
    "sample": {"type": "local HTML fixtures", "type_group": "other", "external": False},
}

FAILURE_TEST_TERMS = (
    "bad",
    "blocked",
    "cancel",
    "concurrent",
    "conflict",
    "corrupt",
    "error",
    "exhaust",
    "fail",
    "invalid",
    "malformed",
    "missing",
    "orphan",
    "oversized",
    "rate_limit",
    "reject",
    "retry",
    "stranded",
    "timeout",
    "unavailable",
    "unsupported",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reproduce repository achievement metrics.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "benchmarks" / "achievement_metrics_cases.json",
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "cvbankas.local.yaml")
    parser.add_argument("--database", type=Path, default=ROOT / "config" / "job_seeker.db")
    parser.add_argument("--legacy-database", type=Path, default=ROOT / "job_seeker.db")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "achievement_metrics_results.json",
    )
    parser.add_argument("--performance-runs", type=int, default=7)
    parser.add_argument("--storage-runs", type=int, default=3)
    parser.add_argument("--storage-items", type=int, default=500)
    parser.add_argument(
        "--web-runs",
        type=int,
        default=5,
        help="Cold/warm repetitions per dashboard query class.",
    )
    parser.add_argument("--skip-performance", action="store_true")
    return parser.parse_args()


def load_cases(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def make_profile(data: dict[str, Any]) -> UserProfile:
    return UserProfile(
        name=data["name"],
        target_roles=list(data["target_roles"]),
        skills=list(data["skills"]),
        preferred_locations=list(data["preferred_locations"]),
        experience_level=data["experience_level"],
        years_of_experience=data["years_of_experience"],
        additional_keywords=list(data["additional_keywords"]),
        must_have_skills=list(data["must_have_skills"]),
        nice_to_have_skills=list(data["nice_to_have_skills"]),
        excluded_keywords=list(data["excluded_keywords"]),
        max_english_level=data["max_english_level"],
        work_modes=[WorkMode(**item) for item in data["work_modes"]],
    )


def make_vacancy(case: dict[str, Any], index: int = 0, url: str | None = None) -> Vacancy:
    text = case.get("text", "")
    return Vacancy(
        source_name=case.get("source_name", "audit"),
        source_id=case.get("id", str(index)),
        source_url=url or f"https://audit.example/jobs/{case.get('id', index)}",
        title=case.get("title", "Audit vacancy"),
        company=case.get("company", "Audit company"),
        location=case.get("location", ""),
        salary_text="",
        requirements=[text] if text else [],
        responsibilities=[],
        raw_text=text,
    )


def binary_metrics(expected: list[bool], predicted: list[bool]) -> dict[str, Any]:
    tp = sum(e and p for e, p in zip(expected, predicted, strict=True))
    fp = sum((not e) and p for e, p in zip(expected, predicted, strict=True))
    fn = sum(e and (not p) for e, p in zip(expected, predicted, strict=True))
    tn = sum((not e) and (not p) for e, p in zip(expected, predicted, strict=True))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "cases": len(expected),
        "correct": tp + tn,
        "accuracy": (tp + tn) / len(expected),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "false_positives": fp,
        "false_negatives": fn,
        "true_positives": tp,
        "true_negatives": tn,
    }


def evaluate_dedup(cases: list[dict[str, Any]]) -> dict[str, Any]:
    predicted_keys = {case["id"]: canonicalize_source_url(case["url"]) for case in cases}
    expected_pairs: set[tuple[str, str]] = set()
    predicted_pairs: set[tuple[str, str]] = set()
    for left_index, left in enumerate(cases):
        for right in cases[left_index + 1 :]:
            pair = (left["id"], right["id"])
            if left["expected_group"] == right["expected_group"]:
                expected_pairs.add(pair)
            if predicted_keys[left["id"]] == predicted_keys[right["id"]]:
                predicted_pairs.add(pair)
    tp = len(expected_pairs & predicted_pairs)
    fp = len(predicted_pairs - expected_pairs)
    fn = len(expected_pairs - predicted_pairs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "records": len(cases),
        "expected_unique_groups": len({case["expected_group"] for case in cases}),
        "predicted_unique_groups": len(set(predicted_keys.values())),
        "expected_duplicate_pairs": len(expected_pairs),
        "predicted_duplicate_pairs": len(predicted_pairs),
        "true_positive_pairs": tp,
        "false_positive_pairs": fp,
        "missed_duplicate_pairs": fn,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "raw_reduction": 1 - len(set(predicted_keys.values())) / len(cases),
        "incorrect_merges": sorted(predicted_pairs - expected_pairs),
        "missed_pairs": sorted(expected_pairs - predicted_pairs),
    }


def evaluate_relevance(cases: list[dict[str, Any]], profile: UserProfile) -> dict[str, Any]:
    results = []
    for index, case in enumerate(cases):
        vacancy = make_vacancy(case, index)
        score, _, _ = _calculate_rule_based_components(vacancy, profile)
        predicted = score >= 45
        results.append(
            {
                "id": case["id"],
                "expected": bool(case["expected_relevant"]),
                "predicted": predicted,
                "score": score,
            }
        )
    summary = binary_metrics(
        [row["expected"] for row in results], [row["predicted"] for row in results]
    )
    summary["decision_threshold"] = "score >= 45 (Medium or High fit)"
    summary["errors"] = [row for row in results if row["expected"] != row["predicted"]]
    summary["results"] = results
    return summary


def evaluate_work_modes(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = [
        {
            "id": case["id"],
            "expected": case["expected"],
            "predicted": _detect_vacancy_work_mode(case["text"].lower()),
        }
        for case in cases
    ]
    correct = sum(row["expected"] == row["predicted"] for row in results)
    labels = ("remote", "hybrid", "office")
    confusion = {
        expected: {
            predicted: sum(
                row["expected"] == expected and row["predicted"] == predicted for row in results
            )
            for predicted in labels
        }
        for expected in labels
    }
    return {
        "cases": len(results),
        "correct": correct,
        "accuracy": correct / len(results),
        "confusion_matrix": confusion,
        "errors": [row for row in results if row["expected"] != row["predicted"]],
    }


def evaluate_eligibility(cases: list[dict[str, Any]], profile: UserProfile) -> dict[str, Any]:
    results = []
    for index, case in enumerate(cases):
        vacancy = make_vacancy(case, index)
        _, _, predicted = _work_mode_match_score(vacancy, profile)
        results.append(
            {"id": case["id"], "expected": bool(case["expected"]), "predicted": predicted}
        )
    summary = binary_metrics(
        [row["expected"] for row in results], [row["predicted"] for row in results]
    )
    us_only = [row for row in results if row["id"] in {"elig-02", "elig-03"}]
    summary["country_restriction_rejections"] = {
        "correct": sum(row["expected"] == row["predicted"] for row in us_only),
        "cases": len(us_only),
    }
    summary["errors"] = [row for row in results if row["expected"] != row["predicted"]]
    return summary


def evaluate_seniority(cases: list[dict[str, Any]]) -> dict[str, Any]:
    predicted_labels = {
        -1: "intern",
        0: "unknown",
        1: "junior",
        2: "mid",
        3: "senior",
        4: "staff_principal",
    }
    results = [
        {
            "id": case["id"],
            "expected": case["expected"],
            "predicted": predicted_labels[_infer_vacancy_seniority(case["text"].lower())],
        }
        for case in cases
    ]
    correct = sum(row["expected"] == row["predicted"] for row in results)
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for row in results:
        confusion[row["expected"]][row["predicted"]] += 1
    return {
        "cases": len(results),
        "correct": correct,
        "exact_match_accuracy": correct / len(results),
        "supported_output_labels": list(predicted_labels.values()),
        "confusion_matrix": {key: dict(value) for key, value in sorted(confusion.items())},
        "errors": [row for row in results if row["expected"] != row["predicted"]],
    }


def repository_inventory(config_path: Path) -> dict[str, Any]:
    registry = build_source_registry(ROOT / "sample_data")
    sources = sorted({source.name for source in registry.values()})
    aliases = sorted(set(registry) - set(sources))
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    enabled = list(config.get("sources", {}).get("enabled", []))
    options = config.get("sources", {}).get("options", {})
    configured_browser = sorted(
        name for name, value in options.items() if str(value.get("fetch_mode", "")).lower() == "browser"
    )
    web_text = (ROOT / "src" / "cvbankas_tracker" / "web.py").read_text(encoding="utf-8")
    main_text = (ROOT / "src" / "cvbankas_tracker" / "main.py").read_text(encoding="utf-8")
    templates = sorted(
        path.name
        for path in (ROOT / "src" / "cvbankas_tracker" / "templates").glob("*.html")
        if path.name != "base.html" and not path.name.startswith("_")
    )
    external_type_counts = Counter(
        SOURCE_METADATA[name]["type_group"]
        for name in sources
        if SOURCE_METADATA[name]["external"]
    )
    scheduler_path = ROOT / "config" / "scheduler.json"
    scheduler: dict[str, Any] = {}
    if scheduler_path.exists():
        try:
            raw_scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
            scheduler = {
                key: raw_scheduler.get(key)
                for key in (
                    "enabled",
                    "time",
                    "last_run_date",
                    "last_status",
                    "attempts",
                    "sources",
                    "limit",
                    "max_pages",
                    "analysis_strategy",
                )
            }
        except (OSError, json.JSONDecodeError):
            scheduler = {"readable": False}
    return {
        "implemented_sources": sources,
        "implemented_source_count_including_sample": len(sources),
        "external_job_source_count": sum(SOURCE_METADATA[name]["external"] for name in sources),
        "registry_aliases": aliases,
        "configured_enabled_sources": enabled,
        "configured_enabled_source_count": len(enabled),
        "configured_browser_sources": configured_browser,
        "configured_browser_source_count": len(configured_browser),
        "external_source_type_counts": {
            "browser": external_type_counts["browser"],
            "api_or_feed": external_type_counts["api_feed"],
            "other": external_type_counts["other"],
        },
        "source_metadata": {name: SOURCE_METADATA[name] for name in sources},
        "normalized_source_schemas": 1,
        "model_providers": ["OpenAI API", "Claude CLI", "Codex CLI"],
        "model_provider_count": 3,
        "model_backed_operations": ["vacancy fit analysis", "vacancy field enrichment", "CV profile building"],
        "model_backed_operation_count": 3,
        "background_job_types": ["daily", "import", "search"],
        "background_job_type_count": 3,
        "web_routes": len(re.findall(r"@app\.(?:get|post|put|delete|patch)\(", web_text)),
        "web_get_routes": len(re.findall(r"@app\.get\(", web_text)),
        "web_post_routes": len(re.findall(r"@app\.post\(", web_text)),
        "rendered_dashboard_workflows": templates,
        "rendered_dashboard_workflow_count": len(templates),
        "cli_options": len(re.findall(r"parser\.add_argument\(", main_text)),
        "pipeline_stages": [
            "source retrieval",
            "parse and normalize",
            "canonical identity/deduplication",
            "optional AI enrichment",
            "AI or deterministic fit evaluation",
            "transactional persistence",
            "dashboard/report/notification presentation",
        ],
        "pipeline_stage_count": 7,
        "scheduler_state": scheduler,
    }


def test_inventory() -> dict[str, Any]:
    files = sorted((ROOT / "tests").glob("test_*.py"))
    methods: list[str] = []
    per_file: dict[str, int] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
        ]
        per_file[path.name] = len(names)
        methods.extend(names)
    failure_tests = sorted(
        name for name in methods if any(term in name.lower() for term in FAILURE_TEST_TERMS)
    )
    return {
        "test_files": len(files),
        "test_methods": len(methods),
        "failure_scenario_name_proxy": len(failure_tests),
        "failure_proxy_definition": "test method name contains one of the fixed failure terms in this script",
        "tests_per_file": per_file,
        "formal_unit_integration_e2e_markers": False,
    }


def readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def longest_streak(statuses: list[str], accepted: set[str]) -> int:
    best = current = 0
    for status in statuses:
        current = current + 1 if status in accepted else 0
        best = max(best, current)
    return best


def database_metrics(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "path": portable_path(path)}
    with contextlib.closing(readonly_connection(path)) as connection:
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        explicit_indexes = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        per_source = [
            dict(row)
            for row in connection.execute(
                "SELECT source_name, COUNT(*) vacancies, COUNT(DISTINCT source_id) distinct_source_ids "
                "FROM vacancies GROUP BY source_name ORDER BY vacancies DESC"
            )
        ]
        runs = list(connection.execute("SELECT * FROM collection_runs ORDER BY id"))
        statuses = [row["status"] for row in runs]
        run_source_totals: dict[str, Counter[str]] = defaultdict(Counter)
        run_source_entries: Counter[str] = Counter()
        clean_entries: Counter[str] = Counter()
        for row in runs:
            try:
                summary = json.loads(row["source_summary_json"] or "{}")
            except json.JSONDecodeError:
                summary = {}
            for source_name, values in summary.items():
                run_source_entries[source_name] += 1
                for key in ("attempted", "failed", "observed", "saved", "pages"):
                    run_source_totals[source_name][key] += int(values.get(key, 0) or 0)
                if int(values.get("failed", 0) or 0) == 0:
                    clean_entries[source_name] += 1
        source_history = {
            source: {
                **dict(values),
                "run_entries": run_source_entries[source],
                "clean_run_entries": clean_entries[source],
                "clean_run_entry_rate": clean_entries[source] / run_source_entries[source],
            }
            for source, values in sorted(run_source_totals.items())
        }
        latest_score_rows = list(
            connection.execute(
                "WITH latest AS (SELECT vacancy_source_url, MAX(id) id FROM analyses GROUP BY vacancy_source_url) "
                "SELECT a.analysis_method, a.fit_label, a.score FROM analyses a JOIN latest l ON a.id=l.id"
            )
        )
        score_bands = {
            "latest_analyses": len(latest_score_rows),
            "score_ge_40": sum(row["score"] >= 40 for row in latest_score_rows),
            "score_ge_45": sum(row["score"] >= 45 for row in latest_score_rows),
            "score_ge_75": sum(row["score"] >= 75 for row in latest_score_rows),
            "ai_based_latest": sum(row["analysis_method"] == "ai_based" for row in latest_score_rows),
            "rule_based_latest": sum(row["analysis_method"] == "rule_based" for row in latest_score_rows),
        }
        repeated_content = connection.execute(
            "WITH keyed AS (SELECT source_name, lower(trim(company)) || char(31) || "
            "lower(trim(title)) || char(31) || lower(trim(coalesce(location,''))) k FROM vacancies), "
            "groups AS (SELECT k, COUNT(*) listings, COUNT(DISTINCT source_name) sources FROM keyed GROUP BY k) "
            "SELECT COUNT(*) groups_count, COALESCE(SUM(listings),0) listings, "
            "COALESCE(SUM(CASE WHEN sources>1 THEN 1 ELSE 0 END),0) cross_source_groups "
            "FROM groups WHERE listings>1"
        ).fetchone()
        observations = counts.get("collection_run_observations", 0)
        vacancies = counts.get("vacancies", 0)
        finished = [row for row in runs if row["finished_at"]]
        vacancy_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(vacancies)")
        }
        completeness: dict[str, dict[str, Any]] = {}
        for field in (
            "source_name",
            "source_id",
            "source_url",
            "title",
            "company",
            "location",
            "salary_text",
            "raw_text",
            "requirements_json",
            "responsibilities_json",
        ):
            if field not in vacancy_columns:
                continue
            if field.endswith("_json"):
                expression = f"TRIM(COALESCE({field}, '')) NOT IN ('', '[]', 'null')"
            else:
                expression = f"TRIM(COALESCE({field}, '')) <> ''"
            complete = connection.execute(
                f"SELECT COUNT(*) FROM vacancies WHERE {expression}"
            ).fetchone()[0]
            completeness[field] = {
                "complete": complete,
                "total": vacancies,
                "percent": complete / vacancies if vacancies else 0.0,
            }
        return {
            "available": True,
            "path": portable_path(path),
            "size_bytes": path.stat().st_size,
            "tables": tables,
            "table_count": len(tables),
            "explicit_indexes": explicit_indexes,
            "explicit_index_count": len(explicit_indexes),
            "row_counts": counts,
            "vacancies_per_source": per_source,
            "run_statuses": dict(Counter(statuses)),
            "first_run": runs[0]["started_at"] if runs else None,
            "last_run": runs[-1]["started_at"] if runs else None,
            "finished_run_count": len(finished),
            "completed_run_streak": longest_streak(statuses, {"completed"}),
            "usable_run_streak": longest_streak(statuses, {"completed", "partial"}),
            "source_history": source_history,
            "latest_score_bands": score_bands,
            "repeat_observations_consolidated": max(0, observations - vacancies),
            "repeat_observation_reduction": (observations - vacancies) / observations if observations else 0.0,
            "exact_repeated_content_groups": dict(repeated_content),
            "field_completeness": completeness,
        }


SUMMARY_RE = re.compile(
    r"\[(?P<timestamp>[^]]+)\] Vacancy batch finished\. sources=(?P<sources>\S+) "
    r"processed=(?P<processed>\d+) saved=(?P<saved>\d+) failed=(?P<failed>\d+) "
    r"pages=(?P<pages>\d+) db=(?P<db>.+)$"
)


def log_metrics() -> dict[str, Any]:
    paths = [ROOT / "logs" / "daily.log", *sorted((ROOT / "config" / "job_logs").glob("*.log"))]
    result: dict[str, Any] = {}
    unique_summaries: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        summaries = []
        fallback_count = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "[AI fallback]" in line:
                fallback_count += 1
            match = SUMMARY_RE.search(line)
            if not match:
                continue
            row: dict[str, Any] = match.groupdict()
            for key in ("processed", "saved", "failed", "pages"):
                row[key] = int(row[key])
            summaries.append(row)
            unique_summaries[(row["timestamp"], row["db"])] = row
        result[str(path.relative_to(ROOT))] = {
            "summary_lines": len(summaries),
            "first_summary": summaries[0]["timestamp"] if summaries else None,
            "last_summary": summaries[-1]["timestamp"] if summaries else None,
            "processed": sum(row["processed"] for row in summaries),
            "saved": sum(row["saved"] for row in summaries),
            "failed": sum(row["failed"] for row in summaries),
            "ai_fallback_events": fallback_count,
        }
    unique = list(unique_summaries.values())
    return {
        "files": result,
        "deduplicated_summary_runs": len(unique),
        "deduplicated_processed": sum(row["processed"] for row in unique),
        "deduplicated_saved": sum(row["saved"] for row in unique),
        "deduplicated_failed": sum(row["failed"] for row in unique),
        "ai_fallback_events": sum(row["ai_fallback_events"] for row in result.values()),
    }


class FaultClient:
    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error

    def analyze(self, vacancy: Vacancy, profile: UserProfile) -> dict[str, object]:
        del vacancy, profile
        if self.error is not None:
            raise self.error
        return normalize_analysis_result(self.payload)


def ai_fault_metrics(profile: UserProfile) -> dict[str, Any]:
    scenarios = {
        "provider_unavailable": FaultClient(error=RuntimeError("provider unavailable")),
        "timeout": FaultClient(error=TimeoutError("provider timed out")),
        "malformed_json": FaultClient(error=ValueError("invalid JSON")),
        "empty_response": FaultClient({}),
        "non_object_response": FaultClient(None),
        "schema_invalid_values": FaultClient(
            {"score": "not-a-score", "fit_label": "alien", "explanation": 123, "matched_points": {"x": 1}}
        ),
        "unexpected_extreme_values": FaultClient(
            {"score": 999, "fit_label": "unexpected", "explanation": "normalized", "matched_points": ["x", None]}
        ),
        "rate_limit": FaultClient(error=RuntimeError("rate limit")),
    }
    rows = []
    with tempfile.TemporaryDirectory() as temp_dir, contextlib.redirect_stdout(io.StringIO()):
        database = DatabaseManager(Path(temp_dir) / "faults.db")
        database.initialize()
        for index, (name, client) in enumerate(scenarios.items()):
            vacancy = make_vacancy({"id": name, "text": "Python APIs n8n remote"}, index)
            service = VacancyAnalysisService(
                AIBasedAnalysisStrategy(client), RuleBasedAnalysisStrategy()
            )
            try:
                analysis = service.analyze(vacancy, profile)
                valid = (
                    isinstance(analysis.score, int)
                    and 0 <= analysis.score <= 100
                    and isinstance(analysis.fit_label, FitLabel)
                    and isinstance(analysis.analysis_method, AnalysisMethod)
                    and bool(analysis.explanation)
                )
                database.save_processed_vacancy(vacancy=vacancy, analysis=analysis)
                rows.append(
                    {
                        "scenario": name,
                        "contained": valid,
                        "fallback_used": analysis.analysis_method == AnalysisMethod.RULE_BASED,
                        "stored_score": analysis.score,
                        "stored_method": analysis.analysis_method.value,
                    }
                )
            except Exception as error:  # pragma: no cover - audit records any regression
                rows.append({"scenario": name, "contained": False, "error": repr(error)})
        with database.connection() as connection:
            stored = connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
            invalid_stored = connection.execute(
                "SELECT COUNT(*) FROM analyses WHERE score < 0 OR score > 100 "
                "OR fit_label NOT IN ('Low','Medium','High')"
            ).fetchone()[0]
    return {
        "scenarios": len(rows),
        "contained": sum(row["contained"] for row in rows),
        "fallbacks": sum(row.get("fallback_used", False) for row in rows),
        "stored_analyses": stored,
        "invalid_stored_rows": invalid_stored,
        "results": rows,
    }


class StubSource:
    def __init__(self, name: str) -> None:
        self.name = name


def source_failure_metrics() -> dict[str, Any]:
    names = [f"source_{index}" for index in range(7)]
    sources = [StubSource(name) for name in names]

    def run(failures: set[str]) -> tuple[list[SourceBatchResult], str]:
        def worker(source: StubSource) -> SourceBatchResult:
            if source.name in failures:
                raise RuntimeError("injected source failure")
            return SourceBatchResult(source.name, [], attempted_count=1, observed_count=1)

        with contextlib.redirect_stdout(io.StringIO()):
            results = _execute_source_batches(sources, worker)
        return results, _collection_terminal_status(results, [])

    single_rows = []
    for name in names:
        results, status = run({name})
        single_rows.append(
            {
                "failed_source": name,
                "unaffected_preserved": sum(row.observed_count == 1 for row in results) == 6,
                "failure_recorded": sum(row.failed_count == 1 for row in results) == 1,
                "status": status,
            }
        )
    multi_sets = [set(names[:2]), set(names[-2:]), set(names[::2]), set(names)]
    multi_rows = []
    for failures in multi_sets:
        results, status = run(failures)
        multi_rows.append(
            {
                "failures": len(failures),
                "expected_survivors": len(names) - len(failures),
                "preserved_survivors": sum(row.observed_count == 1 for row in results),
                "recorded_failures": sum(row.failed_count == 1 for row in results),
                "status": status,
            }
        )
    return {
        "single_source_scenarios": len(single_rows),
        "single_source_scenarios_contained": sum(
            row["unaffected_preserved"] and row["failure_recorded"] and row["status"] == "partial"
            for row in single_rows
        ),
        "multi_source_scenarios": len(multi_rows),
        "multi_source_scenarios_contained": sum(
            row["preserved_survivors"] == row["expected_survivors"]
            and row["recorded_failures"] == row["failures"]
            and row["status"] == ("failed" if row["expected_survivors"] == 0 else "partial")
            for row in multi_rows
        ),
        "single_results": single_rows,
        "multi_results": multi_rows,
    }


def concurrency_metrics(profile: UserProfile) -> dict[str, Any]:
    service = VacancyAnalysisService(RuleBasedAnalysisStrategy())
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "concurrent.db"
        database = DatabaseManager(path)
        database.initialize()
        urls = [f"https://jobs.example.com/roles/777?utm_source=thread-{index}" for index in range(16)]

        def save(index: int) -> bool:
            vacancy = make_vacancy(
                {"id": f"thread-{index}", "text": "Python APIs remote"}, index, urls[index]
            )
            analysis = service.analyze(vacancy, profile)
            DatabaseManager(path).save_processed_vacancy(vacancy=vacancy, analysis=analysis)
            return True

        with ThreadPoolExecutor(max_workers=8) as executor:
            completed = sum(executor.map(save, range(len(urls))))
        with database.connection() as connection:
            counts = {
                "vacancies": connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0],
                "analyses": connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],
                "applications": connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
                "application_events": connection.execute(
                    "SELECT COUNT(*) FROM application_status_events"
                ).fetchone()[0],
            }
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    return {
        "concurrent_attempts": len(urls),
        "completed_attempts": completed,
        "worker_count": 8,
        "canonical_vacancy_rows": counts["vacancies"],
        "analysis_rows": counts["analyses"],
        "application_rows": counts["applications"],
        "initial_status_event_rows": counts["application_events"],
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "limitation": (
            "Exact repeated analysis payloads reuse one row; changed analysis payloads are "
            "intentionally retained. Analysis is still performed before persistence."
        ),
    }


def median_timing(samples: list[float], operations: int) -> dict[str, Any]:
    median = statistics.median(samples)
    return {
        "runs": len(samples),
        "operations_per_run": operations,
        "median_seconds": median,
        "min_seconds": min(samples),
        "max_seconds": max(samples),
        "median_operations_per_second": operations / median,
        "all_seconds": samples,
    }


def percentile(samples: list[float], percentage: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""
    if not samples:
        raise ValueError("A percentile requires at least one sample.")
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentage
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def latency_distribution(samples: list[float]) -> dict[str, Any]:
    result = {
        "observations": len(samples),
        "p50_seconds": percentile(samples, 0.50),
        "p95_seconds": percentile(samples, 0.95),
        "minimum_seconds": min(samples),
        "maximum_seconds": max(samples),
        "all_seconds": samples,
    }
    if len(samples) >= 100:
        result["p99_seconds"] = percentile(samples, 0.99)
    return result


class SyntheticWebSource:
    """Offline source for timing the real dashboard-to-results workflow.

    Each query term yields ten raw URLs: two tracking variants of shared jobs
    and eight term-specific jobs. This exercises collection, canonical identity,
    real fixture parsing, rule analysis, SQLite persistence, the background job
    boundary, polling, and final HTML rendering without contacting job boards.
    """

    name = "sample"
    uses_search_keywords = True

    def __init__(self, data_dir: Path) -> None:
        self._parser = VacancyParser()
        self._fixtures = (
            (data_dir / "vacancy_python_backend.html").read_text(encoding="utf-8"),
            (data_dir / "vacancy_data_analyst.html").read_text(encoding="utf-8"),
        )
        self.keyword_calls = 0
        self.raw_urls_returned = 0
        self.collection_seconds = 0.0
        self.fetch_seconds = 0.0
        self.parse_seconds = 0.0
        self.parsed_vacancies: list[Vacancy] = []

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Any = None,
        stop_at_vacancy: Any = None,
    ) -> tuple[list[str], list[str]]:
        del listing_url, max_pages, stop_at_vacancy
        started = time.perf_counter()
        self.keyword_calls += 1
        call = self.keyword_calls
        keyword_tag = hashlib.sha256(str(keyword).encode("utf-8")).hexdigest()[:10]
        page = f"synthetic://listing/{call}/{keyword_tag}"
        if before_listing_fetch is not None:
            before_listing_fetch(page)
        urls = [
            f"https://benchmark.invalid/jobs/shared-{index}?utm_source={keyword_tag}"
            for index in range(2)
        ]
        urls.extend(
            f"https://benchmark.invalid/jobs/{keyword_tag}-{index}"
            for index in range(8)
        )
        self.raw_urls_returned += len(urls)
        self.collection_seconds += time.perf_counter() - started
        return urls, [page]

    def fetch_vacancy_page(self, url: str) -> str:
        started = time.perf_counter()
        fixture = self._fixtures[int(hashlib.sha256(url.encode()).hexdigest(), 16) % 2]
        self.fetch_seconds += time.perf_counter() - started
        return fixture

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        started = time.perf_counter()
        vacancy = self._parser.parse(html_text, source_url, source_name=self.name)
        vacancy.source_id = canonicalize_source_url(source_url).rsplit("/", 1)[-1]
        self.parsed_vacancies.append(vacancy)
        self.parse_seconds += time.perf_counter() - started
        return vacancy

    def can_handle_url(self, url: str) -> bool:
        return url.startswith("https://benchmark.invalid/")


def _web_database_counts(path: Path) -> dict[str, int]:
    with contextlib.closing(readonly_connection(path)) as connection:
        latest = (
            "WITH latest AS (SELECT vacancy_source_url, MAX(id) id "
            "FROM analyses GROUP BY vacancy_source_url) "
        )
        return {
            "vacancies": connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0],
            "analyses": connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],
            "relevant": connection.execute(
                latest
                + "SELECT COUNT(*) FROM analyses a JOIN latest l ON a.id=l.id WHERE a.score >= 45"
            ).fetchone()[0],
            "shortlisted": connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
        }


def web_search_performance_metrics(profile: UserProfile, repetitions: int) -> dict[str, Any]:
    """Time real dashboard search initiation through a rendered result page."""
    from fastapi.testclient import TestClient

    import src.cvbankas_tracker.main as main_module
    import src.cvbankas_tracker.web as web_module

    del profile
    runtime_profile = ProfileFileReader().read(ROOT / "sample_data" / "active_profile.json")
    repetitions = max(3, repetitions)
    query_classes: dict[str, list[str] | None] = {
        "simple": ["python"],
        "normal": ["python developer", "fastapi"],
        "multi_constraint": ["python", "fastapi", "sql", "remote"],
        "complex": [
            "python",
            "fastapi",
            "sql",
            "remote",
            "automation",
            "api integration",
            "docker",
            "english b2",
        ],
        "adversarial": [
            "python",
            "C++ / C#",
            "n8n & APIs",
            "remote (EU only)",
            "senior or junior",
            "5+ years",
            "not crypto",
            "quoted phrase",
            "vilnius/kaunas",
            "data—platform",
            "LLM",
            "webhook",
        ],
        "profile_driven": None,
    }
    observations: list[dict[str, Any]] = []
    representative_funnels: list[dict[str, Any]] = []
    original_analyze = VacancyAnalysisService.analyze
    original_save = DatabaseManager.save_processed_vacancy

    for class_name, terms in query_classes.items():
        for repeat in range(repetitions):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                db_path = temp_path / "web-search.db"
                app = web_module.create_app(
                    db_path,
                    profile_path=str(ROOT / "sample_data" / "active_profile.json"),
                )
                with TestClient(app, base_url="http://127.0.0.1") as client:
                    client.get("/search")
                    csrf = client.cookies.get("job_seeker_csrf")
                    if not csrf:
                        raise AssertionError("Dashboard benchmark did not receive a CSRF token.")
                    for temperature in ("cold", "warm"):
                        source = SyntheticWebSource(ROOT / "sample_data")
                        stage = {"analysis_seconds": 0.0, "persistence_seconds": 0.0}

                        def timed_analyze(
                            service: VacancyAnalysisService,
                            *args: Any,
                            _stage: dict[str, float] = stage,
                            **kwargs: Any,
                        ):
                            started = time.perf_counter()
                            try:
                                return original_analyze(service, *args, **kwargs)
                            finally:
                                _stage["analysis_seconds"] += time.perf_counter() - started

                        def timed_save(
                            database: DatabaseManager,
                            *args: Any,
                            _stage: dict[str, float] = stage,
                            **kwargs: Any,
                        ):
                            started = time.perf_counter()
                            try:
                                return original_save(database, *args, **kwargs)
                            finally:
                                _stage["persistence_seconds"] += time.perf_counter() - started

                        form = {
                            "csrf_token": csrf,
                            "source_sample": "on",
                            "limit": "500",
                            "max_pages": "1",
                            "analysis_strategy": "rule",
                            "auto_save": "on",
                            "auto_save_threshold": "45",
                            "prune_threshold": "0",
                        }
                        if terms is not None:
                            form["use_keywords"] = "on"
                            form["keywords"] = "\n".join(terms)
                        started = time.perf_counter()
                        with (
                            patch.object(main_module, "resolve_sources", return_value=[source]),
                            patch.object(web_module, "_DEFAULT_EXPORT", str(temp_path / "report.md")),
                            patch.object(VacancyAnalysisService, "analyze", timed_analyze),
                            patch.object(DatabaseManager, "save_processed_vacancy", timed_save),
                            patch.dict(os.environ, {"AI_BACKEND": "rule"}),
                        ):
                            response = client.post(
                                "/search/start",
                                data=form,
                                headers={"origin": "http://127.0.0.1"},
                                follow_redirects=False,
                            )
                            if response.status_code != 303:
                                raise AssertionError(
                                    f"Dashboard search returned {response.status_code}: {response.text}"
                                )
                            job_id = int(response.headers["location"].rsplit("/", 1)[-1])
                            deadline = time.perf_counter() + 30
                            snapshot: dict[str, Any] = {}
                            while time.perf_counter() < deadline:
                                snapshot = client.get(f"/jobs/{job_id}/log").json()
                                if snapshot["status"] not in {"running", "paused"}:
                                    break
                                time.sleep(0.002)
                            if snapshot.get("status") != "done":
                                raise AssertionError(f"Dashboard benchmark job failed: {snapshot}")
                            render_started = time.perf_counter()
                            rendered = client.get("/vacancies")
                            render_seconds = time.perf_counter() - render_started
                            total_seconds = time.perf_counter() - started
                        if rendered.status_code != 200:
                            raise AssertionError("Dashboard result page did not render successfully.")
                        counts = _web_database_counts(db_path)
                        eligible = sum(
                            _work_mode_match_score(vacancy, runtime_profile)[2]
                            for vacancy in source.parsed_vacancies
                        )
                        row = {
                            "query_class": class_name,
                            "temperature": temperature,
                            "repeat": repeat + 1,
                            "keyword_count": source.keyword_calls,
                            "raw_listings": source.raw_urls_returned,
                            "normalized_listings": len(source.parsed_vacancies),
                            "unique_listings": counts["vacancies"],
                            "eligible_listings": eligible if temperature == "cold" else None,
                            "relevant_listings": counts["relevant"],
                            "shortlisted_listings": counts["shortlisted"],
                            "usable_result_rows": counts["vacancies"],
                            "total_seconds": total_seconds,
                            "stage_seconds": {
                                "source_retrieval": source.collection_seconds + source.fetch_seconds,
                                "parse_and_normalize": source.parse_seconds,
                                "analysis": stage["analysis_seconds"],
                                "persistence": stage["persistence_seconds"],
                                "result_query_and_render": render_seconds,
                            },
                        }
                        observations.append(row)
                        if class_name == "complex" and temperature == "cold":
                            representative_funnels.append(row)

    by_class: dict[str, Any] = {}
    for class_name in query_classes:
        class_rows = [row for row in observations if row["query_class"] == class_name]
        by_class[class_name] = {
            temperature: latency_distribution(
                [
                    row["total_seconds"]
                    for row in class_rows
                    if row["temperature"] == temperature
                ]
            )
            for temperature in ("cold", "warm")
        }
        by_class[class_name]["keyword_count"] = class_rows[0]["keyword_count"]
        by_class[class_name]["cold_result_rows"] = class_rows[0]["usable_result_rows"]

    representative = representative_funnels[0]
    stage_names = representative["stage_seconds"]
    representative_summary = {
        "scenario": "complex query through dashboard, offline synthetic source, rule analysis",
        "runs": len(representative_funnels),
        "funnel": {
            key: representative[key]
            for key in (
                "raw_listings",
                "normalized_listings",
                "unique_listings",
                "eligible_listings",
                "relevant_listings",
                "shortlisted_listings",
            )
        },
        "total_latency": latency_distribution(
            [row["total_seconds"] for row in representative_funnels]
        ),
        "median_stage_seconds": {
            name: statistics.median(row["stage_seconds"][name] for row in representative_funnels)
            for name in stage_names
        },
    }
    raw = representative["raw_listings"]
    shortlist = representative["shortlisted_listings"]
    representative_summary["manual_review_reduction"] = 1 - shortlist / raw
    representative_summary["compression_ratio"] = raw / shortlist if shortlist else None
    all_samples = [row["total_seconds"] for row in observations]
    return {
        "method": (
            "FastAPI TestClient POST /search/start -> background job polling -> GET /vacancies; "
            "real fixture parsing, rule analysis, SQLite persistence, and HTML rendering; no network/model calls"
        ),
        "query_classes": len(query_classes),
        "paired_repetitions_per_class": repetitions,
        "total_observations": len(observations),
        "overall": latency_distribution(all_samples),
        "cold": latency_distribution(
            [row["total_seconds"] for row in observations if row["temperature"] == "cold"]
        ),
        "warm": latency_distribution(
            [row["total_seconds"] for row in observations if row["temperature"] == "warm"]
        ),
        "by_query_class": by_class,
        "representative_end_to_end": representative_summary,
        "limitations": [
            "Synthetic offline source backed by two real HTML fixtures; not a live-board latency result.",
            "Rule analysis only; external AI/model latency is not included.",
            "Warm runs reuse persisted results and therefore do less work than cold runs.",
            "Per-class sample sizes are too small for a stable p99.",
        ],
    }


def performance_metrics(profile: UserProfile, runs: int, storage_runs: int, storage_items: int) -> dict[str, Any]:
    runs = max(3, runs)
    storage_runs = max(3, storage_runs)
    urls = []
    for job_id in range(5000):
        base = f"https://jobs.example.com/roles/{job_id}"
        urls.extend(
            [
                base,
                f"{base}/",
                f"{base}?utm_source=source-{job_id}",
                f"{base}?utm_medium=email",
                f"{base}?fbclid={job_id}",
                f"{base}#apply",
                f"{base}?gclid={job_id}",
                f"{base}?utm_campaign=a&utm_source=b",
                f"{base}?mc_cid={job_id}",
                f"{base}?yclid={job_id}",
            ]
        )
    for url in urls[:1000]:
        canonicalize_source_url(url)
    canonical_samples = []
    canonical_unique = 0
    for _ in range(runs):
        start = time.perf_counter()
        canonical_unique = len({canonicalize_source_url(url) for url in urls})
        canonical_samples.append(time.perf_counter() - start)

    analysis_cases = [
        make_vacancy(
            {
                "id": f"perf-{index}",
                "title": "AI Automation Engineer",
                "location": "Remote",
                "text": "Python APIs n8n SQL LLM webhook Docker 2 years English B2",
            },
            index,
        )
        for index in range(20000)
    ]
    for vacancy in analysis_cases[:100]:
        _calculate_rule_based_components(vacancy, profile)
    analysis_samples = []
    for _ in range(runs):
        start = time.perf_counter()
        for vacancy in analysis_cases:
            _calculate_rule_based_components(vacancy, profile)
        analysis_samples.append(time.perf_counter() - start)

    storage_samples = []
    query_samples = []
    service = VacancyAnalysisService(RuleBasedAnalysisStrategy())
    for repeat in range(storage_runs):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = DatabaseManager(Path(temp_dir) / f"pipeline-{repeat}.db")
            database.initialize()
            start = time.perf_counter()
            for index in range(storage_items):
                vacancy = make_vacancy(
                    {
                        "id": f"store-{repeat}-{index}",
                        "title": "AI Automation Engineer",
                        "location": "Remote",
                        "text": "Python APIs n8n SQL LLM automation",
                    },
                    index,
                    f"https://audit.example/{repeat}/jobs/{index}",
                )
                analysis = service.analyze(vacancy, profile)
                database.save_processed_vacancy(vacancy=vacancy, analysis=analysis)
            storage_samples.append(time.perf_counter() - start)
            for _ in range(5):
                database.query_inbox(
                    preferences=InboxPreferences(current_run_only=False)
                )
            local_query_samples = []
            for _ in range(25):
                query_start = time.perf_counter()
                rows = database.query_inbox(
                    preferences=InboxPreferences(current_run_only=False)
                )
                local_query_samples.append(time.perf_counter() - query_start)
                if len(rows) != storage_items:
                    raise AssertionError("Inbox benchmark returned an unexpected row count")
            query_samples.append(statistics.median(local_query_samples))
    return {
        "url_canonicalization_and_set_dedup": {
            **median_timing(canonical_samples, len(urls)),
            "input_urls": len(urls),
            "unique_urls": canonical_unique,
        },
        "deterministic_analysis": median_timing(analysis_samples, len(analysis_cases)),
        "analysis_plus_atomic_sqlite_persistence": median_timing(storage_samples, storage_items),
        "inbox_query": {
            **median_timing(query_samples, 1),
            "rows_returned": storage_items,
            "per_outer_run_median_of_queries": 25,
            "warmup_queries_per_outer_run": 5,
        },
    }


def main() -> int:
    args = parse_args()
    cases, cases_hash = load_cases(args.cases)
    profile = make_profile(cases["profile"])
    results: dict[str, Any] = {
        "audit_schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "cases_sha256": cases_hash,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "repository": repository_inventory(args.config),
        "tests": test_inventory(),
        "active_database": database_metrics(args.database),
        "legacy_database": database_metrics(args.legacy_database),
        "logs": log_metrics(),
        "evaluations": {
            "deduplication": evaluate_dedup(cases["deduplication"]),
            "relevance": evaluate_relevance(cases["relevance"], profile),
            "work_mode": evaluate_work_modes(cases["work_mode"]),
            "eligibility": evaluate_eligibility(cases["eligibility"], profile),
            "seniority": evaluate_seniority(cases["seniority"]),
        },
        "fault_injection": {
            "source_isolation": source_failure_metrics(),
            "ai_analysis": ai_fault_metrics(profile),
            "concurrent_idempotency": concurrency_metrics(profile),
        },
    }
    if not args.skip_performance:
        results["performance"] = performance_metrics(
            profile, args.performance_runs, args.storage_runs, args.storage_items
        )
        results["web_search_performance"] = web_search_performance_metrics(
            profile, args.web_runs
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
