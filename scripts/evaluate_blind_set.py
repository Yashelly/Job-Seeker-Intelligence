"""Evaluate a privately labelled holdout set without exposing labels in Git."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from achievement_metrics import (  # noqa: E402
    evaluate_dedup,
    evaluate_eligibility,
    evaluate_relevance,
    evaluate_seniority,
    evaluate_work_modes,
    make_profile,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate a holdout set with private labels kept outside Git."
    )
    parser.add_argument("--cases", type=Path, required=True, help="Public, unlabeled cases JSON.")
    parser.add_argument("--labels", type=Path, required=True, help="Private labels JSON.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "benchmarks" / "private" / "blind_results.json",
        help="Local result path; defaults to an ignored directory.",
    )
    parser.add_argument(
        "--include-errors",
        action="store_true",
        help="Include per-case expected/predicted values. This consumes the holdout set for tuning.",
    )
    return parser.parse_args()


def read_json(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return decoded, hashlib.sha256(raw).hexdigest()


def require_case_ids(cases: list[dict[str, Any]], labels: list[dict[str, Any]], category: str) -> None:
    case_ids = [str(case.get("id", "")) for case in cases]
    label_ids = [str(label.get("id", "")) for label in labels]
    if not all(case_ids) or not all(label_ids):
        raise ValueError(f"{category} cases and labels require non-empty ids.")
    if len(case_ids) != len(set(case_ids)) or len(label_ids) != len(set(label_ids)):
        raise ValueError(f"{category} contains duplicate ids.")
    if set(case_ids) != set(label_ids):
        missing = sorted(set(case_ids) - set(label_ids))
        extra = sorted(set(label_ids) - set(case_ids))
        raise ValueError(f"{category} label ids do not match cases; missing={missing}, extra={extra}.")


def merge_labels(
    cases: dict[str, Any], labels: dict[str, Any], category: str, expected_key: str
) -> list[dict[str, Any]]:
    case_rows = cases["cases"].get(category, [])
    label_rows = labels["labels"].get(category, [])
    if not isinstance(case_rows, list) or not isinstance(label_rows, list):
        raise ValueError(f"{category} must be an array in cases and labels.")
    require_case_ids(case_rows, label_rows, category)
    label_by_id = {str(row["id"]): row for row in label_rows}
    merged: list[dict[str, Any]] = []
    for case in case_rows:
        label = label_by_id[str(case["id"])]
        if expected_key not in label:
            raise ValueError(f"{category} label {case['id']} is missing {expected_key}.")
        merged.append({**case, expected_key: label[expected_key]})
    return merged


def without_case_details(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in {"errors", "results", "missed_pairs", "incorrect_merges"}}


def evaluate(cases: dict[str, Any], labels: dict[str, Any], include_errors: bool) -> dict[str, Any]:
    if cases.get("schema_version") != 1 or labels.get("schema_version") != 1:
        raise ValueError("Only schema_version 1 is supported.")
    if not isinstance(cases.get("profile"), dict):
        raise ValueError("cases.profile must be an object.")
    profile = make_profile(cases["profile"])
    results = {
        "deduplication": evaluate_dedup(
            merge_labels(cases, labels, "deduplication", "expected_group")
        ),
        "relevance": evaluate_relevance(
            merge_labels(cases, labels, "relevance", "expected_relevant"), profile
        ),
        "work_mode": evaluate_work_modes(
            merge_labels(cases, labels, "work_mode", "expected")
        ),
        "eligibility": evaluate_eligibility(
            merge_labels(cases, labels, "eligibility", "expected"), profile
        ),
        "seniority": evaluate_seniority(
            merge_labels(cases, labels, "seniority", "expected")
        ),
    }
    return results if include_errors else {key: without_case_details(value) for key, value in results.items()}


def main() -> int:
    args = parse_args()
    cases, cases_hash = read_json(args.cases)
    labels, labels_hash = read_json(args.labels)
    if labels.get("cases_sha256") != cases_hash:
        raise ValueError("labels.cases_sha256 does not match the supplied cases file.")
    result = {
        "schema_version": 1,
        "test_set_id": cases.get("test_set_id", "unnamed"),
        "cases_sha256": cases_hash,
        "labels_sha256": labels_hash,
        "per_case_details_included": args.include_errors,
        "results": evaluate(cases, labels, args.include_errors),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
