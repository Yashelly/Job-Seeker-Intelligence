from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import (
    ActionItem,
    ActionReminderItem,
    ActionState,
    AnalysisMethod,
    ApplicationRecord,
    ApplicationStatus,
    ApplicationStatusEvent,
    ApplicationStatusEventKind,
    ApplicationStatusOrigin,
    CollectionRun,
    FitLabel,
    InboxItem,
    InboxPreferences,
    TrackedApplicationItem,
    Vacancy,
    VacancyAnalysis,
    VacancyListItem,
)

DEFAULT_BUSY_TIMEOUT_MS = 30_000
_LOCK_BYTE_COUNT = 1
_UNSET = object()
_WRITE_LOCKS_GUARD = threading.Lock()
_WRITE_LOCKS: dict[Path, threading.RLock] = {}

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on non-Windows only
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None


class DatabaseMigrationError(RuntimeError):
    """Raised when a database cannot be bootstrapped safely."""


class CollectionRunAlreadyActive(RuntimeError):
    """Raised when a collection run is already active for this database path."""


def _write_lock_for(db_path: Path) -> threading.RLock:
    resolved = db_path.expanduser().resolve()
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS[resolved] = lock
        return lock


@dataclass(frozen=True, slots=True)
class DatabaseBootstrapResult:
    db_path: Path
    backup_path: Path | None
    migrated: bool
    journal_mode: str


def project_entrypoint_dir() -> Path:
    """Return the stable project entrypoint directory containing root main.py."""

    return Path(__file__).resolve().parents[2]


def resolve_database_path(
    db_path: str | Path,
    *,
    config_path: str | Path | None = None,
    entrypoint_dir: str | Path | None = None,
) -> Path:
    """Resolve the SQLite path without depending on the caller's CWD.

    Relative DB paths from a resolved config are anchored to the config file's
    directory. Relative paths without a config are anchored to the project
    entrypoint directory (the root containing ``main.py``).
    """

    path = Path(db_path).expanduser()
    if path.is_absolute():
        return path.resolve()

    if config_path:
        base = Path(config_path).expanduser()
        if not base.is_absolute():
            base = (project_entrypoint_dir() / base).resolve()
        base_dir = base.parent
    else:
        base_dir = Path(entrypoint_dir).expanduser() if entrypoint_dir else project_entrypoint_dir()
        if not base_dir.is_absolute():
            base_dir = (project_entrypoint_dir() / base_dir).resolve()

    return (base_dir / path).resolve()



def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_utc_instant(value: str) -> str:
    """Validate and normalize an aware ISO-8601 instant to UTC ``Z`` form."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid UTC instant: {value}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"UTC instant must include a timezone offset: {value}")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonicalize_source_url(source_url: str) -> str:
    """Return the stable duplicate identity for a vacancy URL."""

    parts = urlsplit(source_url.strip())
    scheme = (parts.scheme or "https").lower()
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return source_url.strip()
    port = parts.port
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    ignored_prefixes = ("utm_",)
    ignored_names = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in ignored_names
        and not any(key.lower().startswith(prefix) for prefix in ignored_prefixes)
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def bootstrap_database(
    db_path: str | Path,
    *,
    create_backup: bool | None = None,
    backup_dir: str | Path | None = None,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> DatabaseBootstrapResult:
    """Run one-time schema/WAL migration and safety audits for a SQLite DB."""

    resolved = Path(db_path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with _bootstrap_lock(resolved):
        return _bootstrap_database_locked(
            resolved,
            create_backup=create_backup,
            backup_dir=backup_dir,
            busy_timeout_ms=busy_timeout_ms,
        )


def _bootstrap_database_locked(
    resolved: Path,
    *,
    create_backup: bool | None,
    backup_dir: str | Path | None,
    busy_timeout_ms: int,
) -> DatabaseBootstrapResult:
    existed = resolved.exists() and resolved.stat().st_size > 0
    backup_path: Path | None = None

    connection = _open_connection(resolved, busy_timeout_ms=busy_timeout_ms, foreign_keys=False)
    try:
        if existed:
            _raise_unless_integrity_ok(connection)
            _raise_unless_foreign_keys_clean(connection)
            should_backup = _schema_needs_migration(connection) if create_backup is None else create_backup
            if should_backup:
                backup_path = _backup_database(connection, resolved, backup_dir)

        before_signature = _schema_signature(connection)
        journal_mode = _set_wal_mode(connection)
        _migrate_schema(connection)
        _raise_unless_integrity_ok(connection)
        _raise_unless_foreign_keys_clean(connection)
        after_signature = _schema_signature(connection)
    finally:
        connection.close()

    return DatabaseBootstrapResult(
        db_path=resolved,
        backup_path=backup_path,
        migrated=before_signature != after_signature,
        journal_mode=journal_mode,
    )


@contextmanager
def _bootstrap_lock(db_path: Path) -> Iterator[None]:
    lock_path = _bootstrap_lock_path(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _bootstrap_lock_path(db_path: Path) -> Path:
    digest = hashlib.sha256(str(db_path).casefold().encode("utf-8")).hexdigest()[:16]
    return db_path.with_name(f"{db_path.name}.{digest}.bootstrap.lock")


def _lock_file(lock_file: IO[bytes]) -> None:
    lock_file.seek(0)
    if msvcrt is not None:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, _LOCK_BYTE_COUNT)
        return
    if fcntl is None:  # pragma: no cover - no known supported platform lacks both
        raise DatabaseMigrationError("No file-locking primitive is available for bootstrap.")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock_file: IO[bytes]) -> None:
    lock_file.seek(0)
    if msvcrt is not None:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTE_COUNT)
        return
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _open_connection(
    db_path: Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    foreign_keys: bool = True,
) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=busy_timeout_ms / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
    return connection


def _set_wal_mode(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    return str(row[0]).lower() if row is not None else ""


def _schema_signature(connection: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger', 'view')
        ORDER BY type, name
        """
    ).fetchall()
    return tuple((row["name"], row["sql"] or "") for row in rows)


def _backup_database(
    connection: sqlite3.Connection,
    db_path: Path,
    backup_dir: str | Path | None,
) -> Path:
    target_dir = Path(backup_dir).expanduser() if backup_dir else db_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = target_dir / f"{db_path.name}.{timestamp}.bak"
    backup_connection = sqlite3.connect(backup_path)
    try:
        connection.backup(backup_connection)
    finally:
        backup_connection.close()
    return backup_path


def _raise_unless_integrity_ok(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA quick_check").fetchall()
    problems = [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]
    if problems:
        raise DatabaseMigrationError(
            "SQLite integrity check failed; migration was not run. "
            f"Create/restore a backup and repair the database first: {problems[:3]}"
        )


def _raise_unless_foreign_keys_clean(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if rows:
        examples = [
            f"table={row[0]} rowid={row[1]} parent={row[2]} fk_index={row[3]}"
            for row in rows[:5]
        ]
        raise DatabaseMigrationError(
            "Foreign-key audit failed; bootstrap stopped before serving requests. "
            "Back up the database, repair or remove orphan rows, then retry. "
            f"Examples: {examples}"
        )


def _migrate_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS vacancies (
            source_url TEXT PRIMARY KEY,
            source_name TEXT NOT NULL DEFAULT 'cvbankas',
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            salary_text TEXT NOT NULL,
            requirements_json TEXT NOT NULL,
            responsibilities_json TEXT NOT NULL,
            raw_text TEXT NOT NULL DEFAULT '',
            original_source_url TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            first_seen_run_id INTEGER,
            last_seen_run_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            db_path TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'partial', 'failed')),
            source_summary_json TEXT NOT NULL DEFAULT '{}',
            error_summary_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS collection_run_leases (
            db_path TEXT PRIMARY KEY,
            run_id INTEGER NOT NULL,
            acquired_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES collection_runs(id)
        );

        CREATE TABLE IF NOT EXISTS collection_run_observations (
            run_id INTEGER NOT NULL,
            vacancy_source_url TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source_name TEXT NOT NULL,
            original_source_url TEXT,
            PRIMARY KEY (run_id, vacancy_source_url),
            FOREIGN KEY (run_id) REFERENCES collection_runs(id),
            FOREIGN KEY (vacancy_source_url) REFERENCES vacancies(source_url)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vacancy_url_aliases (
            original_source_url TEXT PRIMARY KEY,
            canonical_source_url TEXT NOT NULL,
            collision_group TEXT NOT NULL,
            migrated_at TEXT NOT NULL,
            legacy_vacancy_json TEXT,
            FOREIGN KEY (canonical_source_url) REFERENCES vacancies(source_url)
        );

        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_source_url TEXT NOT NULL,
            analysis_method TEXT NOT NULL,
            score INTEGER NOT NULL,
            fit_label TEXT NOT NULL,
            explanation TEXT NOT NULL,
            matched_points_json TEXT NOT NULL,
            missing_points_json TEXT NOT NULL,
            notes TEXT NOT NULL,
            FOREIGN KEY (vacancy_source_url) REFERENCES vacancies(source_url)
        );

        CREATE TABLE IF NOT EXISTS applications (
            vacancy_source_url TEXT PRIMARY KEY,
            analysis_id INTEGER,
            status TEXT NOT NULL,
            notes TEXT NOT NULL,
            FOREIGN KEY (vacancy_source_url) REFERENCES vacancies(source_url),
            FOREIGN KEY (analysis_id) REFERENCES analyses(id)
        );

        CREATE TABLE IF NOT EXISTS application_status_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_source_url TEXT NOT NULL,
            previous_status TEXT,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin IN ('cli', 'tui', 'web', 'migration', 'system')),
            kind TEXT NOT NULL CHECK(kind IN ('normal', 'corrective', 'baseline')),
            note TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (vacancy_source_url) REFERENCES applications(vacancy_source_url)
        );

        CREATE TABLE IF NOT EXISTS action_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_source_url TEXT NOT NULL,
            title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            due_at_utc TEXT,
            state TEXT NOT NULL CHECK(state IN ('open', 'completed')),
            completed_at_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            FOREIGN KEY (vacancy_source_url) REFERENCES vacancies(source_url)
        );
        """
    )
    _ensure_column(connection, "vacancies", "raw_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "vacancies", "source_name", "TEXT NOT NULL DEFAULT 'cvbankas'")
    _ensure_column(connection, "vacancies", "last_seen_run_id", "INTEGER")
    _ensure_column(connection, "vacancies", "first_seen_run_id", "INTEGER")
    _ensure_column(connection, "vacancies", "last_seen_at", "TEXT")
    _ensure_column(connection, "vacancies", "first_seen_at", "TEXT")
    _ensure_column(connection, "vacancies", "original_source_url", "TEXT")
    _ensure_column(connection, "vacancy_url_aliases", "legacy_vacancy_json", "TEXT")
    _canonicalize_legacy_vacancy_urls(connection)
    _baseline_legacy_application_status_events(connection)
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_vacancies_source_name_id
            ON vacancies(source_name, source_id);
        CREATE INDEX IF NOT EXISTS idx_analyses_vacancy_id
            ON analyses(vacancy_source_url, id);
        CREATE INDEX IF NOT EXISTS idx_applications_status
            ON applications(status);
        CREATE INDEX IF NOT EXISTS idx_vacancies_lifecycle
            ON vacancies(last_seen_run_id, first_seen_run_id, last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_vacancies_source_score_lookup
            ON vacancies(source_name, last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_collection_runs_status_finished
            ON collection_runs(status, finished_at);
        CREATE INDEX IF NOT EXISTS idx_observations_run_source
            ON collection_run_observations(run_id, source_name);
        CREATE INDEX IF NOT EXISTS idx_analyses_score_fit
            ON analyses(score, fit_label);
        CREATE INDEX IF NOT EXISTS idx_vacancy_url_aliases_canonical
            ON vacancy_url_aliases(canonical_source_url);
        CREATE INDEX IF NOT EXISTS idx_application_status_events_vacancy
            ON application_status_events(vacancy_source_url, id);
        CREATE INDEX IF NOT EXISTS idx_action_items_due_state
            ON action_items(state, due_at_utc);
        CREATE INDEX IF NOT EXISTS idx_action_items_vacancy
            ON action_items(vacancy_source_url);
        """
    )
    connection.commit()


def _schema_needs_migration(connection: sqlite3.Connection) -> bool:
    required_tables = {"vacancies", "analyses", "applications", "collection_runs", "collection_run_leases", "collection_run_observations", "settings", "vacancy_url_aliases", "application_status_events", "action_items"}
    existing_tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not required_tables.issubset(existing_tables):
        return True

    vacancy_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(vacancies)").fetchall()
    }
    if not {"source_name", "raw_text", "original_source_url", "first_seen_at", "last_seen_at", "first_seen_run_id", "last_seen_run_id"}.issubset(vacancy_columns):
        return True

    required_indexes = {
        "idx_vacancies_source_name_id",
        "idx_analyses_vacancy_id",
        "idx_applications_status",
        "idx_vacancies_lifecycle",
        "idx_vacancies_source_score_lookup",
        "idx_collection_runs_status_finished",
        "idx_observations_run_source",
        "idx_analyses_score_fit",
        "idx_vacancy_url_aliases_canonical",
        "idx_application_status_events_vacancy",
        "idx_action_items_due_state",
        "idx_action_items_vacancy",
    }
    existing_indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    return not required_indexes.issubset(existing_indexes)




def _canonicalize_legacy_vacancy_urls(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT * FROM vacancies ORDER BY source_url").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(canonicalize_source_url(row["source_url"]), []).append(row)

    migrated_at = utc_now_iso()
    for canonical_url, group_rows in groups.items():
        original_urls = [row["source_url"] for row in group_rows]
        if len(group_rows) == 1 and original_urls[0] == canonical_url:
            continue

        survivor = _choose_canonical_survivor(canonical_url, original_urls)
        merged_application = _merged_application_for_urls(connection, original_urls, canonical_url)

        for row in group_rows:
            original_url = row["source_url"]
            if original_url != canonical_url or len(group_rows) > 1:
                connection.execute(
                    """
                    INSERT INTO vacancy_url_aliases (
                        original_source_url, canonical_source_url, collision_group,
                        migrated_at, legacy_vacancy_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(original_source_url) DO UPDATE SET
                        canonical_source_url = excluded.canonical_source_url,
                        collision_group = excluded.collision_group,
                        legacy_vacancy_json = COALESCE(vacancy_url_aliases.legacy_vacancy_json, excluded.legacy_vacancy_json)
                    """,
                    (
                        original_url,
                        canonical_url,
                        canonical_url,
                        migrated_at,
                        json.dumps(dict(row), sort_keys=True),
                    ),
                )

        connection.execute(
            f"DELETE FROM applications WHERE vacancy_source_url IN ({','.join('?' for _ in original_urls)})",
            tuple(original_urls),
        )
        connection.execute(
            f"UPDATE analyses SET vacancy_source_url = ? WHERE vacancy_source_url IN ({','.join('?' for _ in original_urls)})",
            (canonical_url, *original_urls),
        )
        merged_lifecycle = _merged_lifecycle_for_rows(group_rows)
        merged_observations = _merged_observations_for_urls(connection, original_urls, canonical_url)
        connection.execute(
            f"DELETE FROM collection_run_observations WHERE vacancy_source_url IN ({','.join('?' for _ in original_urls)})",
            tuple(original_urls),
        )
        for row in group_rows:
            if row["source_url"] != survivor:
                connection.execute("DELETE FROM vacancies WHERE source_url = ?", (row["source_url"],))
        connection.execute(
            """
            UPDATE vacancies
            SET source_url = ?,
                original_source_url = COALESCE(original_source_url, ?),
                first_seen_at = ?,
                last_seen_at = ?,
                first_seen_run_id = ?,
                last_seen_run_id = ?
            WHERE source_url = ?
            """,
            (
                canonical_url,
                survivor,
                merged_lifecycle["first_seen_at"],
                merged_lifecycle["last_seen_at"],
                merged_lifecycle["first_seen_run_id"],
                merged_lifecycle["last_seen_run_id"],
                survivor,
            ),
        )
        for observation in merged_observations:
            connection.execute(
                """
                INSERT INTO collection_run_observations (
                    run_id, vacancy_source_url, observed_at, source_name, original_source_url
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    observation["run_id"],
                    canonical_url,
                    observation["observed_at"],
                    observation["source_name"],
                    observation["original_source_url"],
                ),
            )
        if merged_application is not None:
            connection.execute(
                """
                INSERT INTO applications (vacancy_source_url, analysis_id, status, notes)
                VALUES (?, ?, ?, ?)
                """,
                (
                    canonical_url,
                    merged_application["analysis_id"],
                    merged_application["status"],
                    merged_application["notes"],
                ),
            )



def _merged_lifecycle_for_rows(group_rows: list[sqlite3.Row]) -> dict[str, object | None]:
    def value(row: sqlite3.Row, name: str) -> object | None:
        return row[name] if name in row.keys() else None

    first_seen_pairs = [
        (str(value(row, "first_seen_at")), value(row, "first_seen_run_id"))
        for row in group_rows
        if value(row, "first_seen_at") is not None
    ]
    last_seen_pairs = [
        (str(value(row, "last_seen_at")), value(row, "last_seen_run_id"))
        for row in group_rows
        if value(row, "last_seen_at") is not None
    ]
    first_seen_at, first_seen_run_id = min(first_seen_pairs) if first_seen_pairs else (None, None)
    last_seen_at, last_seen_run_id = max(last_seen_pairs) if last_seen_pairs else (None, None)
    if first_seen_run_id is None:
        run_ids = [value(row, "first_seen_run_id") for row in group_rows if value(row, "first_seen_run_id") is not None]
        first_seen_run_id = min(run_ids) if run_ids else None
    if last_seen_run_id is None:
        run_ids = [value(row, "last_seen_run_id") for row in group_rows if value(row, "last_seen_run_id") is not None]
        last_seen_run_id = max(run_ids) if run_ids else None
    return {
        "first_seen_at": first_seen_at,
        "first_seen_run_id": first_seen_run_id,
        "last_seen_at": last_seen_at,
        "last_seen_run_id": last_seen_run_id,
    }


def _merged_observations_for_urls(
    connection: sqlite3.Connection,
    original_urls: list[str],
    canonical_url: str,
) -> list[dict[str, object | None]]:
    placeholders = ",".join("?" for _ in original_urls)
    rows = connection.execute(
        f"""
        SELECT run_id, vacancy_source_url, observed_at, source_name, original_source_url
        FROM collection_run_observations
        WHERE vacancy_source_url IN ({placeholders})
        ORDER BY run_id, observed_at, vacancy_source_url
        """,
        tuple(original_urls),
    ).fetchall()
    merged: dict[int, dict[str, object | None]] = {}
    for row in rows:
        run_id = int(row["run_id"])
        current = merged.get(run_id)
        if current is None:
            merged[run_id] = {
                "run_id": run_id,
                "observed_at": row["observed_at"],
                "source_name": row["source_name"],
                "original_source_url": row["original_source_url"] or row["vacancy_source_url"],
            }
            continue
        if row["observed_at"] and str(row["observed_at"]) > str(current["observed_at"] or ""):
            current["observed_at"] = row["observed_at"]
            current["source_name"] = row["source_name"]
        originals = [part for part in str(current["original_source_url"] or "").split("\n") if part]
        candidate = row["original_source_url"] or row["vacancy_source_url"]
        if candidate not in originals:
            originals.append(candidate)
        current["original_source_url"] = "\n".join(originals)
    return [merged[run_id] for run_id in sorted(merged)]


def _choose_canonical_survivor(canonical_url: str, original_urls: list[str]) -> str:
    return canonical_url if canonical_url in original_urls else sorted(original_urls)[0]


def _merged_application_for_urls(
    connection: sqlite3.Connection,
    original_urls: list[str],
    canonical_url: str,
) -> dict[str, object] | None:
    placeholders = ",".join("?" for _ in original_urls)
    rows = connection.execute(
        f"SELECT * FROM applications WHERE vacancy_source_url IN ({placeholders}) ORDER BY vacancy_source_url",
        tuple(original_urls),
    ).fetchall()
    if not rows:
        return None
    status_rank = {
        ApplicationStatus.SAVED.value: 0,
        ApplicationStatus.APPLIED.value: 1,
        ApplicationStatus.INTERVIEW.value: 2,
        ApplicationStatus.OFFER.value: 3,
        ApplicationStatus.REJECTED.value: 4,
        ApplicationStatus.WITHDRAWN.value: 5,
    }
    chosen = max(rows, key=lambda row: (status_rank.get(row["status"], -1), row["vacancy_source_url"]))
    notes = []
    for row in rows:
        note = (row["notes"] or "").strip()
        if note:
            notes.append(f"[{row['vacancy_source_url']}] {note}")
    analysis_ids = [row["analysis_id"] for row in rows if row["analysis_id"] is not None]
    return {
        "analysis_id": max(analysis_ids) if analysis_ids else None,
        "status": chosen["status"],
        "notes": "\n".join(dict.fromkeys(notes)),
    }


def _baseline_legacy_application_status_events(connection: sqlite3.Connection) -> None:
    now = utc_now_iso()
    rows = connection.execute(
        """
        SELECT app.vacancy_source_url, app.status, app.notes
        FROM applications app
        WHERE NOT EXISTS (
            SELECT 1
            FROM application_status_events event
            WHERE event.vacancy_source_url = app.vacancy_source_url
        )
        ORDER BY app.vacancy_source_url
        """
    ).fetchall()
    for row in rows:
        connection.execute(
            """
            INSERT INTO application_status_events (
                vacancy_source_url, previous_status, new_status, changed_at,
                origin, kind, note, reason
            ) VALUES (?, NULL, ?, ?, 'migration', 'baseline', ?, ?)
            """,
            (
                row["vacancy_source_url"],
                row["status"],
                now,
                row["notes"] or "",
                "Baseline event created during migration; prior status history is unavailable.",
            ),
        )

def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing_names = {column["name"] for column in columns}
    if column_name in existing_names:
        return
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


class DatabaseManager:
    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._busy_timeout_ms = busy_timeout_ms

    @property
    def db_path(self) -> Path:
        return self._db_path

    def initialize(self, *, create_backup: bool | None = None) -> DatabaseBootstrapResult:
        return bootstrap_database(
            self._db_path,
            create_backup=create_backup,
            busy_timeout_ms=self._busy_timeout_ms,
        )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = _open_connection(
            self._db_path,
            busy_timeout_ms=self._busy_timeout_ms,
            foreign_keys=True,
        )
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # SQLite permits many WAL readers but only one writer. Source batches run
        # concurrently in-process with per-operation connections, so serialize
        # write transactions per database path and acquire the SQLite write lock
        # up front. This prevents mid-vacancy partial writes caused by competing
        # deferred transactions while retaining normal busy_timeout protection
        # for external processes.
        with _write_lock_for(self._db_path), self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def close(self) -> None:
        """Compatibility no-op; operational connections are per method."""

    def begin_collection_run(self) -> CollectionRun:
        now = utc_now_iso()
        db_identity = str(self._db_path)
        with _write_lock_for(self._db_path):
            with self.connection() as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    active = connection.execute(
                        """
                        SELECT r.id, r.started_at
                        FROM collection_runs r
                        JOIN collection_run_leases l ON l.run_id = r.id
                        WHERE l.db_path = ? AND r.status = 'running'
                        """,
                        (db_identity,),
                    ).fetchone()
                    if active is not None:
                        raise CollectionRunAlreadyActive(
                            f"Collection run already active for {db_identity}: run_id={active['id']} started_at={active['started_at']}"
                        )
                    cursor = connection.execute(
                        """
                        INSERT INTO collection_runs (db_path, started_at, status, source_summary_json, error_summary_json)
                        VALUES (?, ?, 'running', '{}', '{}')
                        """,
                        (db_identity, now),
                    )
                    run_id = int(cursor.lastrowid)
                    connection.execute(
                        "INSERT OR REPLACE INTO collection_run_leases (db_path, run_id, acquired_at) VALUES (?, ?, ?)",
                        (db_identity, run_id, now),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        run = self.get_collection_run(run_id)
        assert run is not None
        return run

    def finish_collection_run(
        self,
        run_id: int,
        *,
        status: Literal["completed", "partial", "failed"],
        source_summary: dict[str, object] | None = None,
        error_summary: dict[str, object] | None = None,
    ) -> CollectionRun:
        if status not in {"completed", "partial", "failed"}:
            raise ValueError(f"Invalid terminal collection run status: {status}")
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE collection_runs
                SET finished_at = ?, status = ?, source_summary_json = ?, error_summary_json = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    utc_now_iso(),
                    status,
                    json.dumps(source_summary or {}, sort_keys=True),
                    json.dumps(error_summary or {}, sort_keys=True),
                    run_id,
                ),
            )
            connection.execute(
                "DELETE FROM collection_run_leases WHERE db_path = ? AND run_id = ?",
                (str(self._db_path), run_id),
            )
        run = self.get_collection_run(run_id)
        assert run is not None
        return run

    def get_collection_run(self, run_id: int) -> CollectionRun | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM collection_runs WHERE id = ?", (run_id,)).fetchone()
        return _collection_run_from_row(row) if row is not None else None

    def get_latest_inbox_run_id(self) -> int | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM collection_runs
                WHERE status IN ('completed', 'partial')
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return int(row["id"]) if row is not None else None

    def get_latest_inbox_run(self) -> CollectionRun | None:
        run_id = self.get_latest_inbox_run_id()
        return self.get_collection_run(run_id) if run_id is not None else None

    def record_vacancy_observation(
        self,
        source_url: str,
        *,
        collection_run_id: int | None,
        source_name: str | None = None,
        observed_at: str | None = None,
        original_source_url: str | None = None,
    ) -> bool:
        canonical_url = canonicalize_source_url(source_url)
        observed_at = observed_at or utc_now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT source_name FROM vacancies WHERE source_url = ?",
                (canonical_url,),
            ).fetchone()
            if row is None:
                return False
            existing_source_name = source_name or row["source_name"]
            connection.execute(
                """
                UPDATE vacancies
                SET last_seen_at = ?,
                    last_seen_run_id = COALESCE(?, last_seen_run_id),
                    original_source_url = COALESCE(original_source_url, ?)
                WHERE source_url = ?
                """,
                (observed_at, collection_run_id, original_source_url, canonical_url),
            )
            if collection_run_id is not None:
                connection.execute(
                    """
                    INSERT INTO collection_run_observations (
                        run_id, vacancy_source_url, observed_at, source_name, original_source_url
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, vacancy_source_url) DO UPDATE SET
                        observed_at = excluded.observed_at,
                        source_name = excluded.source_name,
                        original_source_url = COALESCE(collection_run_observations.original_source_url, excluded.original_source_url)
                    """,
                    (collection_run_id, canonical_url, observed_at, existing_source_name, original_source_url),
                )
        return True

    def get_inbox_preferences(self) -> InboxPreferences:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = 'inbox_preferences'"
            ).fetchone()
        if row is None:
            return InboxPreferences()
        data = json.loads(row["value_json"])
        return _inbox_preferences_from_mapping(data)

    def save_inbox_preferences(self, preferences: InboxPreferences) -> None:
        preferences = _validate_inbox_preferences(preferences)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value_json)
                VALUES ('inbox_preferences', ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                """,
                (json.dumps({
                    "minimum_score": preferences.minimum_score,
                    "hide_below_threshold": preferences.hide_below_threshold,
                    "sort_by": preferences.sort_by,
                    "source_name": preferences.source_name,
                    "fit_label": preferences.fit_label,
                    "application_status": preferences.application_status,
                    "new_only": preferences.new_only,
                    "current_run_only": preferences.current_run_only,
                }, sort_keys=True),),
            )

    def query_inbox(
        self,
        *,
        preferences: InboxPreferences | None = None,
        run_id: int | None = None,
        source_name: str | None = None,
        fit_label: str | FitLabel | None = None,
        application_status: str | ApplicationStatus | None = None,
        new_only: bool | None = None,
        current_run_only: bool | None = None,
        include_below_threshold: bool | None = None,
    ) -> list[InboxItem]:
        preferences = _validate_inbox_preferences(preferences or self.get_inbox_preferences())
        source_name = source_name if source_name is not None else (preferences.source_name or None)
        fit_label = fit_label if fit_label is not None else (preferences.fit_label or None)
        application_status = (
            application_status
            if application_status is not None
            else (preferences.application_status or None)
        )
        explicit_current_run_filter = current_run_only is not None
        new_only = preferences.new_only if new_only is None else new_only
        current_run_only = (
            preferences.current_run_only if current_run_only is None else current_run_only
        )
        if new_only and not explicit_current_run_filter:
            current_run_only = False
        run_id = run_id if run_id is not None else self.get_latest_inbox_run_id()
        show_below = (not preferences.hide_below_threshold) if include_below_threshold is None else include_below_threshold
        clauses: list[str] = []
        params: list[object] = []
        if source_name:
            clauses.append("v.source_name = ?")
            params.append(source_name)
        if fit_label:
            clauses.append("a.fit_label = ?")
            params.append(fit_label.value if isinstance(fit_label, FitLabel) else str(fit_label))
        if application_status:
            clauses.append("app.status = ?")
            params.append(application_status.value if isinstance(application_status, ApplicationStatus) else str(application_status))
        if new_only:
            clauses.append("v.first_seen_run_id = ?")
            params.append(run_id)
        if current_run_only:
            clauses.append("v.last_seen_run_id = ?")
            params.append(run_id)
        if not show_below:
            clauses.append("COALESCE(a.score, -1) >= ?")
            params.append(preferences.minimum_score)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        order_by = {
            "score": "COALESCE(a.score, -1) DESC, v.last_seen_at DESC, v.title ASC",
            "newest": "v.last_seen_at DESC, v.title ASC",
            "title": "v.title COLLATE NOCASE ASC, v.company COLLATE NOCASE ASC",
            "company": "v.company COLLATE NOCASE ASC, v.title COLLATE NOCASE ASC",
        }[preferences.sort_by]
        sql = f"""
            SELECT
                v.source_name, v.source_id, v.source_url, v.original_source_url,
                v.title, v.company, v.location,
                v.first_seen_at, v.last_seen_at, v.first_seen_run_id, v.last_seen_run_id,
                a.score AS latest_score, a.fit_label AS latest_fit_label, a.explanation,
                a.matched_points_json, a.missing_points_json, app.status AS application_status
            FROM vacancies v
            LEFT JOIN analyses a ON a.id = (
                SELECT a2.id FROM analyses a2
                WHERE a2.vacancy_source_url = v.source_url
                ORDER BY a2.id DESC LIMIT 1
            )
            LEFT JOIN applications app ON app.vacancy_source_url = v.source_url
            {where}
            ORDER BY {order_by}
        """
        with self.connection() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [_inbox_item_from_row(row, run_id) for row in rows]

    def save_vacancy(self, vacancy: Vacancy, *, collection_run_id: int | None = None, observed_at: str | None = None, original_source_url: str | None = None) -> None:
        canonical_url = canonicalize_source_url(vacancy.source_url)
        observed_at = observed_at or utc_now_iso()
        original_source_url = original_source_url or vacancy.source_url
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO vacancies (
                    source_url, source_name, source_id, title, company, location, salary_text,
                    requirements_json, responsibilities_json, raw_text, original_source_url,
                    first_seen_at, last_seen_at, first_seen_run_id, last_seen_run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
                    source_name = excluded.source_name,
                    source_id = excluded.source_id,
                    title = excluded.title,
                    company = excluded.company,
                    location = excluded.location,
                    salary_text = excluded.salary_text,
                    requirements_json = excluded.requirements_json,
                    responsibilities_json = excluded.responsibilities_json,
                    raw_text = excluded.raw_text,
                    original_source_url = COALESCE(vacancies.original_source_url, excluded.original_source_url),
                    first_seen_at = COALESCE(vacancies.first_seen_at, excluded.first_seen_at),
                    last_seen_at = excluded.last_seen_at,
                    first_seen_run_id = COALESCE(vacancies.first_seen_run_id, excluded.first_seen_run_id),
                    last_seen_run_id = excluded.last_seen_run_id
                """,
                (
                    canonical_url,
                    vacancy.source_name,
                    vacancy.source_id,
                    vacancy.title,
                    vacancy.company,
                    vacancy.location,
                    vacancy.salary_text,
                    json.dumps(vacancy.requirements),
                    json.dumps(vacancy.responsibilities),
                    vacancy.raw_text,
                    original_source_url,
                    observed_at,
                    observed_at,
                    collection_run_id,
                    collection_run_id,
                ),
            )
            if collection_run_id is not None:
                connection.execute(
                    """
                    INSERT INTO collection_run_observations (
                        run_id, vacancy_source_url, observed_at, source_name, original_source_url
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, vacancy_source_url) DO UPDATE SET
                        observed_at = excluded.observed_at,
                        source_name = excluded.source_name,
                        original_source_url = COALESCE(collection_run_observations.original_source_url, excluded.original_source_url)
                    """,
                    (collection_run_id, canonical_url, observed_at, vacancy.source_name, original_source_url),
                )

    def get_vacancy(self, source_url: str) -> Vacancy | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM vacancies WHERE source_url = ?",
                (canonicalize_source_url(source_url),),
            ).fetchone()
        if row is None:
            return None

        return Vacancy(
            source_name=row["source_name"],
            source_id=row["source_id"],
            source_url=row["source_url"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            salary_text=row["salary_text"],
            requirements=json.loads(row["requirements_json"]),
            responsibilities=json.loads(row["responsibilities_json"]),
            raw_text=row["raw_text"],
        )

    def has_vacancy(self, source_url: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM vacancies WHERE source_url = ?",
                (canonicalize_source_url(source_url),),
            ).fetchone()
        return row is not None

    def list_vacancies(self) -> list[Vacancy]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM vacancies ORDER BY title"
            ).fetchall()
        return [
            Vacancy(
                source_name=row["source_name"],
                source_id=row["source_id"],
                source_url=row["source_url"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                salary_text=row["salary_text"],
                requirements=json.loads(row["requirements_json"]),
                responsibilities=json.loads(row["responsibilities_json"]),
                raw_text=row["raw_text"],
            )
            for row in rows
        ]

    def list_vacancies_with_latest_scores(self) -> list[VacancyListItem]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    v.source_name,
                    v.source_id,
                    v.source_url,
                    v.title,
                    v.company,
                    v.location,
                    a.score AS latest_score,
                    a.fit_label AS latest_fit_label,
                    app.status AS application_status
                FROM vacancies v
                LEFT JOIN analyses a
                    ON a.id = (
                        SELECT a2.id
                        FROM analyses a2
                        WHERE a2.vacancy_source_url = v.source_url
                        ORDER BY a2.id DESC
                        LIMIT 1
                    )
                LEFT JOIN applications app
                    ON app.vacancy_source_url = v.source_url
                ORDER BY COALESCE(a.score, -1) DESC, v.title ASC
                """
            ).fetchall()
        return [
            VacancyListItem(
                source_name=row["source_name"],
                source_id=row["source_id"],
                source_url=row["source_url"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                latest_score=row["latest_score"],
                latest_fit_label=row["latest_fit_label"],
                application_status=(
                    ApplicationStatus(row["application_status"])
                    if row["application_status"]
                    else None
                ),
            )
            for row in rows
        ]

    def save_analysis(self, analysis: VacancyAnalysis) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO analyses (
                    vacancy_source_url, analysis_method, score, fit_label, explanation,
                    matched_points_json, missing_points_json, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonicalize_source_url(analysis.vacancy_source_url),
                    analysis.analysis_method.value,
                    analysis.score,
                    analysis.fit_label.value,
                    analysis.explanation,
                    json.dumps(list(analysis.matched_points)),
                    json.dumps(list(analysis.missing_points)),
                    analysis.notes,
                ),
            )
            return int(cursor.lastrowid)

    def save_processed_vacancy(
        self,
        *,
        vacancy: Vacancy,
        analysis: VacancyAnalysis,
        collection_run_id: int | None = None,
        original_source_url: str | None = None,
        application_origin: ApplicationStatusOrigin = ApplicationStatusOrigin.SYSTEM,
        application_note: str = "",
    ) -> tuple[int, ApplicationRecord]:
        """Persist one processed vacancy as one all-or-nothing write unit.

        A batch item is only reportable after its vacancy, analysis,
        application projection, and initial application event have all been
        committed together. This keeps parallel source workers from observing a
        partially persisted vacancy if SQLite write-lock acquisition fails.
        """

        canonical_url = canonicalize_source_url(vacancy.source_url)
        observed_at = utc_now_iso()
        original_source_url = original_source_url or vacancy.source_url
        now = utc_now_iso()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO vacancies (
                    source_url, source_name, source_id, title, company, location, salary_text,
                    requirements_json, responsibilities_json, raw_text, original_source_url,
                    first_seen_at, last_seen_at, first_seen_run_id, last_seen_run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
                    source_name = excluded.source_name,
                    source_id = excluded.source_id,
                    title = excluded.title,
                    company = excluded.company,
                    location = excluded.location,
                    salary_text = excluded.salary_text,
                    requirements_json = excluded.requirements_json,
                    responsibilities_json = excluded.responsibilities_json,
                    raw_text = excluded.raw_text,
                    original_source_url = COALESCE(vacancies.original_source_url, excluded.original_source_url),
                    first_seen_at = COALESCE(vacancies.first_seen_at, excluded.first_seen_at),
                    last_seen_at = excluded.last_seen_at,
                    first_seen_run_id = COALESCE(vacancies.first_seen_run_id, excluded.first_seen_run_id),
                    last_seen_run_id = excluded.last_seen_run_id
                """,
                (
                    canonical_url,
                    vacancy.source_name,
                    vacancy.source_id,
                    vacancy.title,
                    vacancy.company,
                    vacancy.location,
                    vacancy.salary_text,
                    json.dumps(vacancy.requirements),
                    json.dumps(vacancy.responsibilities),
                    vacancy.raw_text,
                    original_source_url,
                    observed_at,
                    observed_at,
                    collection_run_id,
                    collection_run_id,
                ),
            )
            if collection_run_id is not None:
                connection.execute(
                    """
                    INSERT INTO collection_run_observations (
                        run_id, vacancy_source_url, observed_at, source_name, original_source_url
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, vacancy_source_url) DO UPDATE SET
                        observed_at = excluded.observed_at,
                        source_name = excluded.source_name,
                        original_source_url = COALESCE(collection_run_observations.original_source_url, excluded.original_source_url)
                    """,
                    (collection_run_id, canonical_url, observed_at, vacancy.source_name, original_source_url),
                )
            cursor = connection.execute(
                """
                INSERT INTO analyses (
                    vacancy_source_url, analysis_method, score, fit_label, explanation,
                    matched_points_json, missing_points_json, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_url,
                    analysis.analysis_method.value,
                    analysis.score,
                    analysis.fit_label.value,
                    analysis.explanation,
                    json.dumps(list(analysis.matched_points)),
                    json.dumps(list(analysis.missing_points)),
                    analysis.notes,
                ),
            )
            analysis_id = int(cursor.lastrowid)
            existing_application = connection.execute(
                "SELECT * FROM applications WHERE vacancy_source_url = ?",
                (canonical_url,),
            ).fetchone()
            if existing_application is None:
                connection.execute(
                    """
                    INSERT INTO applications (vacancy_source_url, analysis_id, status, notes)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        canonical_url,
                        analysis_id,
                        ApplicationStatus.SAVED.value,
                        application_note,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO application_status_events (
                        vacancy_source_url, previous_status, new_status, changed_at,
                        origin, kind, note, reason
                    ) VALUES (?, NULL, ?, ?, ?, 'normal', ?, '')
                    """,
                    (
                        canonical_url,
                        ApplicationStatus.SAVED.value,
                        now,
                        application_origin.value,
                        application_note,
                    ),
                )
                application = ApplicationRecord(
                    vacancy_source_url=canonical_url,
                    analysis_id=analysis_id,
                    status=ApplicationStatus.SAVED,
                    notes=application_note,
                )
            else:
                connection.execute(
                    """
                    UPDATE applications
                    SET analysis_id = ?, notes = CASE WHEN notes = '' THEN ? ELSE notes END
                    WHERE vacancy_source_url = ?
                    """,
                    (analysis_id, application_note, canonical_url),
                )
                application = ApplicationRecord(
                    vacancy_source_url=canonical_url,
                    analysis_id=analysis_id,
                    status=ApplicationStatus(existing_application["status"]),
                    notes=existing_application["notes"] or application_note,
                )
        return analysis_id, application

    def list_analyses(self) -> list[tuple[int, VacancyAnalysis]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM analyses ORDER BY id").fetchall()
        analyses: list[tuple[int, VacancyAnalysis]] = []
        for row in rows:
            analyses.append(
                (
                    int(row["id"]),
                    VacancyAnalysis(
                        vacancy_source_url=row["vacancy_source_url"],
                        analysis_method=AnalysisMethod(row["analysis_method"]),
                        score=row["score"],
                        fit_label=FitLabel(row["fit_label"]),
                        explanation=row["explanation"],
                        matched_points=tuple(json.loads(row["matched_points_json"])),
                        missing_points=tuple(json.loads(row["missing_points_json"])),
                        notes=row["notes"],
                    ),
                )
            )
        return analyses

    def get_latest_analysis(self, source_url: str) -> VacancyAnalysis | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM analyses
                WHERE vacancy_source_url = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (canonicalize_source_url(source_url),),
            ).fetchone()
        if row is None:
            return None

        return VacancyAnalysis(
            vacancy_source_url=row["vacancy_source_url"],
            analysis_method=AnalysisMethod(row["analysis_method"]),
            score=row["score"],
            fit_label=FitLabel(row["fit_label"]),
            explanation=row["explanation"],
            matched_points=tuple(json.loads(row["matched_points_json"])),
            missing_points=tuple(json.loads(row["missing_points_json"])),
            notes=row["notes"],
        )

    def get_latest_analysis_id(self, source_url: str) -> int | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT id
                FROM analyses
                WHERE vacancy_source_url = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (canonicalize_source_url(source_url),),
            ).fetchone()
        return int(row["id"]) if row is not None else None

    def get_vacancy_by_source_id(
        self,
        source_id: str,
        source_name: str | None = None,
    ) -> Vacancy | None:
        with self.connection() as connection:
            if source_name:
                row = connection.execute(
                    "SELECT * FROM vacancies WHERE source_name = ? AND source_id = ?",
                    (source_name, source_id),
                ).fetchone()
                return _vacancy_from_row(row) if row is not None else None

            rows = connection.execute(
                "SELECT * FROM vacancies WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        if not rows:
            return None
        source_names = {row["source_name"] for row in rows}
        if len(source_names) > 1:
            joined = ", ".join(sorted(source_names))
            raise ValueError(
                f"Source ID {source_id!r} exists in multiple sources ({joined}); "
                "provide a vacancy source."
            )
        return _vacancy_from_row(rows[0])

    def save_application_record(self, record: ApplicationRecord) -> None:
        """Persist an application projection without silently rewriting status.

        Status creation/changes are always accompanied by an append-only event.
        Existing-row status changes through this compatibility method are treated
        as normal system-origin transitions and are validated by the domain graph.
        """

        vacancy_source_url = canonicalize_source_url(record.vacancy_source_url)
        now = utc_now_iso()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM applications WHERE vacancy_source_url = ?",
                (vacancy_source_url,),
            ).fetchone()
            if existing is not None:
                previous_status = ApplicationStatus(existing["status"])
                if previous_status != record.status:
                    ApplicationRecord(
                        vacancy_source_url=vacancy_source_url,
                        analysis_id=existing["analysis_id"],
                        status=previous_status,
                        notes=existing["notes"],
                    ).update_status(record.status)
            connection.execute(
                """
                INSERT INTO applications (vacancy_source_url, analysis_id, status, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(vacancy_source_url) DO UPDATE SET
                    analysis_id = excluded.analysis_id,
                    status = excluded.status,
                    notes = excluded.notes
                """,
                (
                    vacancy_source_url,
                    record.analysis_id,
                    record.status.value,
                    record.notes,
                ),
            )
            if existing is None or ApplicationStatus(existing["status"]) != record.status:
                connection.execute(
                    """
                    INSERT INTO application_status_events (
                        vacancy_source_url, previous_status, new_status, changed_at,
                        origin, kind, note, reason
                    ) VALUES (?, ?, ?, ?, 'system', 'normal', ?, '')
                    """,
                    (
                        vacancy_source_url,
                        existing["status"] if existing is not None else None,
                        record.status.value,
                        now,
                        record.notes,
                    ),
                )

    def create_application_record_with_event(
        self,
        record: ApplicationRecord,
        *,
        origin: ApplicationStatusOrigin = ApplicationStatusOrigin.CLI,
        note: str = "",
    ) -> None:
        vacancy_source_url = canonicalize_source_url(record.vacancy_source_url)
        now = utc_now_iso()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT status FROM applications WHERE vacancy_source_url = ?",
                (vacancy_source_url,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO applications (vacancy_source_url, analysis_id, status, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(vacancy_source_url) DO UPDATE SET
                    analysis_id = COALESCE(excluded.analysis_id, applications.analysis_id),
                    notes = CASE
                        WHEN applications.notes = '' THEN excluded.notes
                        ELSE applications.notes
                    END
                """,
                (vacancy_source_url, record.analysis_id, record.status.value, record.notes),
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO application_status_events (
                        vacancy_source_url, previous_status, new_status, changed_at,
                        origin, kind, note, reason
                    ) VALUES (?, NULL, ?, ?, ?, 'normal', ?, '')
                    """,
                    (vacancy_source_url, record.status.value, now, origin.value, note),
                )

    def update_application_status_with_event(
        self,
        vacancy_source_url: str,
        new_status: ApplicationStatus,
        *,
        origin: ApplicationStatusOrigin,
        kind: ApplicationStatusEventKind = ApplicationStatusEventKind.NORMAL,
        reason: str = "",
        note: str = "",
        analysis_id: int | None = None,
    ) -> ApplicationRecord:
        canonical_url = canonicalize_source_url(vacancy_source_url)
        if origin not in set(ApplicationStatusOrigin):
            raise ValueError(f"Invalid application status origin: {origin}")
        if kind == ApplicationStatusEventKind.CORRECTIVE and not reason.strip():
            raise ValueError("Corrective status reassignment requires a reason.")
        if kind != ApplicationStatusEventKind.CORRECTIVE and reason.strip():
            raise ValueError("Reasons are only accepted for corrective status reassignment.")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM applications WHERE vacancy_source_url = ?",
                (canonical_url,),
            ).fetchone()
            if row is None:
                raise ValueError(f"No application record exists for {canonical_url}.")
            previous_status = ApplicationStatus(row["status"])
            if previous_status == new_status:
                return ApplicationRecord(
                    vacancy_source_url=canonical_url,
                    analysis_id=row["analysis_id"],
                    status=previous_status,
                    notes=row["notes"],
                )
            if kind != ApplicationStatusEventKind.CORRECTIVE:
                ApplicationRecord(
                    vacancy_source_url=canonical_url,
                    analysis_id=row["analysis_id"],
                    status=previous_status,
                    notes=row["notes"],
                ).update_status(new_status)
            connection.execute(
                """
                UPDATE applications
                SET status = ?, analysis_id = COALESCE(?, analysis_id)
                WHERE vacancy_source_url = ?
                """,
                (new_status.value, analysis_id, canonical_url),
            )
            connection.execute(
                """
                INSERT INTO application_status_events (
                    vacancy_source_url, previous_status, new_status, changed_at,
                    origin, kind, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_url,
                    previous_status.value,
                    new_status.value,
                    utc_now_iso(),
                    origin.value,
                    kind.value,
                    note,
                    reason.strip(),
                ),
            )
        record = self.get_application_record(canonical_url)
        assert record is not None
        return record

    def list_application_status_events(self, source_url: str) -> list[ApplicationStatusEvent]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM application_status_events
                WHERE vacancy_source_url = ?
                ORDER BY id
                """,
                (canonicalize_source_url(source_url),),
            ).fetchall()
        return [_application_status_event_from_row(row) for row in rows]

    def get_application_record(self, source_url: str) -> ApplicationRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM applications WHERE vacancy_source_url = ?",
                (canonicalize_source_url(source_url),),
            ).fetchone()
        if row is None:
            return None

        return ApplicationRecord(
            vacancy_source_url=row["vacancy_source_url"],
            analysis_id=row["analysis_id"],
            status=ApplicationStatus(row["status"]),
            notes=row["notes"],
        )

    def list_tracked_applications(self) -> list[TrackedApplicationItem]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    v.source_name,
                    v.source_id,
                    app.vacancy_source_url AS source_url,
                    v.title,
                    v.company,
                    app.status,
                    app.notes,
                    a.score AS latest_score,
                    a.fit_label AS latest_fit_label
                FROM applications app
                JOIN vacancies v
                    ON v.source_url = app.vacancy_source_url
                LEFT JOIN analyses a
                    ON a.id = COALESCE(
                        app.analysis_id,
                        (
                            SELECT a2.id
                            FROM analyses a2
                            WHERE a2.vacancy_source_url = app.vacancy_source_url
                            ORDER BY a2.id DESC
                            LIMIT 1
                        )
                    )
                ORDER BY app.status ASC, v.title ASC
                """
            ).fetchall()
        return [
            TrackedApplicationItem(
                source_name=row["source_name"],
                source_id=row["source_id"],
                source_url=row["source_url"],
                title=row["title"],
                company=row["company"],
                status=ApplicationStatus(row["status"]),
                latest_score=row["latest_score"],
                latest_fit_label=row["latest_fit_label"],
                notes=row["notes"],
            )
            for row in rows
        ]

    def get_user_timezone(self) -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = 'user_timezone'"
            ).fetchone()
        if row is None:
            return "UTC"
        return str(json.loads(row["value_json"]))

    def save_user_timezone(self, timezone_name: str, *, confirmed: bool = True) -> None:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone_name)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value_json)
                VALUES ('user_timezone', ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                """,
                (json.dumps(timezone_name),),
            )
            if confirmed:
                connection.execute(
                    """
                    INSERT INTO settings (key, value_json)
                    VALUES ('user_timezone_confirmed_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                    """,
                    (json.dumps(utc_now_iso()),),
                )

    def get_user_timezone_confirmation(self) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = 'user_timezone_confirmed_at'"
            ).fetchone()
        return None if row is None else str(json.loads(row["value_json"]))

    def create_action_item(
        self,
        *,
        vacancy_source_url: str,
        title: str,
        notes: str = "",
        due_at_utc: str | None = None,
    ) -> ActionItem:
        canonical_url = canonicalize_source_url(vacancy_source_url)
        cleaned_title = " ".join(title.split())
        if not cleaned_title:
            raise ValueError("Action title is required.")
        due_at_utc = normalize_utc_instant(due_at_utc) if due_at_utc is not None else None
        now = utc_now_iso()
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM vacancies WHERE source_url = ?", (canonical_url,)).fetchone() is None:
                raise ValueError(f"Vacancy not found in the database: {canonical_url}")
            cursor = connection.execute(
                """
                INSERT INTO action_items (
                    vacancy_source_url, title, notes, due_at_utc, state,
                    completed_at_utc, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'open', NULL, ?, ?)
                """,
                (canonical_url, cleaned_title, notes.strip(), due_at_utc, now, now),
            )
            action_id = int(cursor.lastrowid)
        action = self.get_action_item(action_id)
        assert action is not None
        return action

    def update_action_item(
        self,
        action_id: int,
        *,
        title: str | None = None,
        notes: str | None = None,
        due_at_utc: str | object | None = _UNSET,
    ) -> ActionItem:
        cleaned_title = None if title is None else " ".join(title.split())
        if cleaned_title == "":
            raise ValueError("Action title is required.")
        if due_at_utc is not _UNSET and due_at_utc is not None:
            due_at_utc = normalize_utc_instant(str(due_at_utc))
        due_changed = due_at_utc is not _UNSET
        due_bind_value = due_at_utc if due_changed else None
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM action_items WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                raise ValueError(f"Action not found: {action_id}")
            connection.execute(
                """
                UPDATE action_items
                SET title = COALESCE(?, title),
                    notes = COALESCE(?, notes),
                    due_at_utc = CASE WHEN ? THEN ? ELSE due_at_utc END,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (
                    cleaned_title,
                    notes.strip() if notes is not None else None,
                    due_changed,
                    due_bind_value,
                    utc_now_iso(),
                    action_id,
                ),
            )
        action = self.get_action_item(action_id)
        assert action is not None
        return action

    def complete_action_item(self, action_id: int) -> ActionItem:
        now = utc_now_iso()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE action_items
                SET state = 'completed', completed_at_utc = COALESCE(completed_at_utc, ?), updated_at_utc = ?
                WHERE id = ?
                """,
                (now, now, action_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Action not found: {action_id}")
        action = self.get_action_item(action_id)
        assert action is not None
        return action

    def reopen_action_item(self, action_id: int) -> ActionItem:
        now = utc_now_iso()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE action_items
                SET state = 'open', completed_at_utc = NULL, updated_at_utc = ?
                WHERE id = ?
                """,
                (now, action_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Action not found: {action_id}")
        action = self.get_action_item(action_id)
        assert action is not None
        return action

    def get_action_item(self, action_id: int) -> ActionItem | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM action_items WHERE id = ?", (action_id,)).fetchone()
        return _action_item_from_row(row) if row is not None else None

    def list_action_items(self, *, vacancy_source_url: str | None = None, include_completed: bool = True) -> list[ActionItem]:
        clauses: list[str] = []
        params: list[object] = []
        if vacancy_source_url:
            clauses.append("vacancy_source_url = ?")
            params.append(canonicalize_source_url(vacancy_source_url))
        if not include_completed:
            clauses.append("state = 'open'")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM action_items
                {where}
                ORDER BY due_at_utc IS NULL ASC, due_at_utc ASC, id ASC
                """,
                tuple(params),
            ).fetchall()
        return [_action_item_from_row(row) for row in rows]

    def query_action_reminders(self, *, now_utc: str | None = None) -> list[ActionReminderItem]:
        now_utc = normalize_utc_instant(now_utc) if now_utc is not None else utc_now_iso()
        due_soon_end = _add_utc_hours(now_utc, 24)
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT *,
                    CASE
                        WHEN due_at_utc < ? THEN 'overdue'
                        WHEN due_at_utc >= ? AND due_at_utc <= ? THEN 'due_soon'
                        ELSE 'none'
                    END AS reminder_state
                FROM action_items
                WHERE state = 'open'
                  AND due_at_utc IS NOT NULL
                  AND due_at_utc <= ?
                ORDER BY due_at_utc ASC, id ASC
                """,
                (now_utc, now_utc, due_soon_end, due_soon_end),
            ).fetchall()
        return [
            ActionReminderItem(
                action=_action_item_from_row(row),
                reminder_state=row["reminder_state"],
            )
            for row in rows
        ]


def _vacancy_from_row(row: sqlite3.Row) -> Vacancy:
    return Vacancy(
        source_name=row["source_name"],
        source_id=row["source_id"],
        source_url=row["source_url"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        salary_text=row["salary_text"],
        requirements=json.loads(row["requirements_json"]),
        responsibilities=json.loads(row["responsibilities_json"]),
        raw_text=row["raw_text"],
    )



def _collection_run_from_row(row: sqlite3.Row) -> CollectionRun:
    return CollectionRun(
        id=int(row["id"]),
        db_path=row["db_path"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        source_summary=json.loads(row["source_summary_json"] or "{}"),
        error_summary=json.loads(row["error_summary_json"] or "{}"),
    )


def _application_status_event_from_row(row: sqlite3.Row) -> ApplicationStatusEvent:
    return ApplicationStatusEvent(
        id=int(row["id"]),
        vacancy_source_url=row["vacancy_source_url"],
        previous_status=ApplicationStatus(row["previous_status"]) if row["previous_status"] else None,
        new_status=ApplicationStatus(row["new_status"]),
        changed_at=row["changed_at"],
        origin=ApplicationStatusOrigin(row["origin"]),
        kind=ApplicationStatusEventKind(row["kind"]),
        note=row["note"] or "",
        reason=row["reason"] or "",
    )


def _action_item_from_row(row: sqlite3.Row) -> ActionItem:
    return ActionItem(
        id=int(row["id"]),
        vacancy_source_url=row["vacancy_source_url"],
        title=row["title"],
        notes=row["notes"] or "",
        due_at_utc=row["due_at_utc"],
        state=ActionState(row["state"]),
        completed_at_utc=row["completed_at_utc"],
        created_at_utc=row["created_at_utc"],
        updated_at_utc=row["updated_at_utc"],
    )


def _add_utc_hours(instant: str, hours: int) -> str:
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (
        parsed.astimezone(UTC)
        .replace(microsecond=0)
        .__add__(timedelta(hours=hours))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _validate_inbox_preferences(preferences: InboxPreferences) -> InboxPreferences:
    if not 0 <= int(preferences.minimum_score) <= 100:
        raise ValueError("Inbox minimum_score must be between 0 and 100.")
    allowed_sorts = {"score", "newest", "title", "company"}
    if preferences.sort_by not in allowed_sorts:
        raise ValueError(f"Inbox sort_by must be one of {sorted(allowed_sorts)}.")
    source_name = " ".join(str(preferences.source_name or "").split())
    fit_label = " ".join(str(preferences.fit_label or "").split())
    if fit_label:
        fit_matches = [label.value for label in FitLabel if label.value.lower() == fit_label.lower()]
        if not fit_matches:
            raise ValueError("Inbox fit_label must be High, Medium, Low, or blank.")
        fit_label = fit_matches[0]
    application_status = " ".join(str(preferences.application_status or "").split())
    if application_status:
        status_matches = [
            status.value for status in ApplicationStatus if status.value.lower() == application_status.lower()
        ]
        if not status_matches:
            raise ValueError("Inbox application_status must be a known status or blank.")
        application_status = status_matches[0]
    return InboxPreferences(
        minimum_score=int(preferences.minimum_score),
        hide_below_threshold=bool(preferences.hide_below_threshold),
        sort_by=preferences.sort_by,
        source_name=source_name,
        fit_label=fit_label,
        application_status=application_status,
        new_only=bool(preferences.new_only),
        current_run_only=bool(preferences.current_run_only),
    )


def _inbox_preferences_from_mapping(data: dict[str, object]) -> InboxPreferences:
    return _validate_inbox_preferences(
        InboxPreferences(
            minimum_score=int(data.get("minimum_score", 0)),
            hide_below_threshold=bool(data.get("hide_below_threshold", False)),
            sort_by=str(data.get("sort_by", "score")),
            source_name=str(data.get("source_name", "")),
            fit_label=str(data.get("fit_label", "")),
            application_status=str(data.get("application_status", "")),
            new_only=bool(data.get("new_only", False)),
            current_run_only=bool(data.get("current_run_only", True)),
        )
    )


def _inbox_item_from_row(row: sqlite3.Row, run_id: int | None) -> InboxItem:
    return InboxItem(
        source_name=row["source_name"],
        source_id=row["source_id"],
        source_url=row["source_url"],
        original_source_url=row["original_source_url"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        first_seen_run_id=row["first_seen_run_id"],
        last_seen_run_id=row["last_seen_run_id"],
        is_new_in_run=(run_id is not None and row["first_seen_run_id"] == run_id),
        is_current_run=(run_id is not None and row["last_seen_run_id"] == run_id),
        latest_score=row["latest_score"],
        latest_fit_label=row["latest_fit_label"],
        explanation=row["explanation"] or "",
        matched_points=tuple(json.loads(row["matched_points_json"] or "[]")),
        missing_points=tuple(json.loads(row["missing_points_json"] or "[]")),
        application_status=(
            ApplicationStatus(row["application_status"])
            if row["application_status"]
            else None
        ),
    )
