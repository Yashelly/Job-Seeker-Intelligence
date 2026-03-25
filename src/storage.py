from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    company TEXT,
    role_title TEXT,
    location TEXT,
    work_mode TEXT,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT,
    salary_type TEXT,
    score INTEGER,
    decision TEXT,
    extracted_json TEXT NOT NULL,
    score_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    cover_letter TEXT NOT NULL
);
"""


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(SCHEMA_SQL)
        conn.commit()


def save_run(
    db_path: str,
    source_type: str,
    source_ref: str,
    job: Dict[str, Any],
    score_result: Dict[str, Any],
    summary: str,
    cover_letter: str,
) -> int:
    salary = job.get("salary", {}) or {}
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO job_runs (
                source_type, source_ref, company, role_title, location, work_mode,
                salary_min, salary_max, salary_currency, salary_type,
                score, decision, extracted_json, score_json, summary, cover_letter
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_type,
                source_ref,
                job.get("company"),
                job.get("role_title"),
                job.get("location"),
                job.get("work_mode"),
                salary.get("min"),
                salary.get("max"),
                salary.get("currency"),
                salary.get("gross_or_net"),
                score_result.get("score"),
                score_result.get("decision"),
                json.dumps(job, ensure_ascii=False),
                json.dumps(score_result, ensure_ascii=False),
                summary,
                cover_letter,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_run(db_path: str, run_id: int) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return dict(row)
