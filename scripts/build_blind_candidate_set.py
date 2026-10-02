"""Create an unlabeled, local-only candidate set from an existing vacancy DB."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build unlabeled local blind-evaluation candidates.")
    parser.add_argument("--database", type=Path, default=ROOT / "config" / "job_seeker.db")
    parser.add_argument("--profile", type=Path, default=ROOT / "sample_data" / "active_profile.json")
    parser.add_argument("--seed", type=int, default=20260907)
    parser.add_argument("--per-category", type=int, default=120)
    parser.add_argument("--dedup-pairs", type=int, default=100)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "benchmarks" / "private" / "blind_inputs.json",
    )
    return parser.parse_args()


def profile_payload(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "name": data["name"],
        "target_roles": list(data["target_roles"]),
        "skills": list(data["skills"]),
        "preferred_locations": list(data["preferred_locations"]),
        "experience_level": data["experience_level"],
        "years_of_experience": data.get("years_of_experience"),
        "additional_keywords": list(data.get("additional_keywords", [])),
        "must_have_skills": list(data.get("must_have_skills", [])),
        "nice_to_have_skills": list(data.get("nice_to_have_skills", [])),
        "excluded_keywords": list(data.get("excluded_keywords", [])),
        "max_english_level": data.get("max_english_level"),
        "work_modes": list(data.get("work_modes", [])),
    }


def load_rows(database: Path) -> list[dict[str, str]]:
    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT source_url, title, company, location, raw_text, source_name FROM vacancies "
            "WHERE TRIM(source_url) <> '' ORDER BY source_url"
        ).fetchall()
    finally:
        connection.close()
    return [{key: str(row[key] or "") for key in row.keys()} for row in rows]


def sample_rows(rows: list[dict[str, str]], count: int, rng: random.Random) -> list[dict[str, str]]:
    if not rows:
        raise ValueError("No vacancies available for candidate construction.")
    return rng.sample(rows, min(count, len(rows)))


def vacancy_case(row: dict[str, str], case_id: str) -> dict[str, str]:
    return {
        "id": case_id,
        "source_name": row["source_name"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "text": row["raw_text"][:6000],
    }


def repeated_pairs(rows: list[dict[str, str]], requested: int, rng: random.Random) -> list[tuple[dict[str, str], dict[str, str]]]:
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = tuple(row[field].strip().lower() for field in ("title", "company", "location"))
        if all(key):
            groups[key].append(row)
    positive = [group[:2] for group in groups.values() if len(group) >= 2]
    rng.shuffle(positive)
    pairs = [(group[0], group[1]) for group in positive[: requested // 2]]
    while len(pairs) < requested and len(rows) >= 2:
        left, right = rng.sample(rows, 2)
        pairs.append((left, right))
    return pairs


def main() -> int:
    args = parse_args()
    if args.per_category < 1 or args.dedup_pairs < 1:
        raise ValueError("--per-category and --dedup-pairs must be positive.")
    rng = random.Random(args.seed)
    rows = load_rows(args.database)
    categories = {
        "relevance": [vacancy_case(row, f"rel-{index:04}") for index, row in enumerate(sample_rows(rows, args.per_category, rng), 1)],
        "eligibility": [vacancy_case(row, f"elig-{index:04}") for index, row in enumerate(sample_rows(rows, args.per_category, rng), 1)],
        "work_mode": [
            {"id": f"mode-{index:04}", "text": row["raw_text"][:6000]}
            for index, row in enumerate(sample_rows(rows, args.per_category, rng), 1)
        ],
        "seniority": [
            {"id": f"sen-{index:04}", "text": row["raw_text"][:6000]}
            for index, row in enumerate(sample_rows(rows, args.per_category, rng), 1)
        ],
        "deduplication": [],
    }
    for index, (left, right) in enumerate(repeated_pairs(rows, args.dedup_pairs, rng), 1):
        categories["deduplication"].extend(
            [
                {"id": f"dedup-{index:04}-a", "url": left["source_url"]},
                {"id": f"dedup-{index:04}-b", "url": right["source_url"]},
            ]
        )
    payload = {
        "schema_version": 1,
        "test_set_id": f"local-candidate-seed-{args.seed}",
        "profile": profile_payload(args.profile),
        "cases": categories,
        "construction_note": "Unlabeled candidates sampled locally. Independent annotation is required before blind evaluation.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.output}: {len(rows)} source rows; "
        f"{args.per_category} cases per classification category; {args.dedup_pairs} dedup pairs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
