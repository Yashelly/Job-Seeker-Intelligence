"""Company career directory, independent of vacancies and application history."""

from __future__ import annotations

import io
import json
import re
import unicodedata
import zipfile
from datetime import date
from typing import Any
from urllib.parse import unquote, urlsplit

from .storage import DatabaseManager, utc_now_iso

MAX_REGISTRY_BYTES = 5 * 1024 * 1024
MAX_COMPANIES = 5000
SUPPORTED_ATS = {"ashby", "lever", "greenhouse", "workable", "recruitee", "smartrecruiters", "personio", "teamtailor", "workday", "paylocity", "elastic_custom", "astro_server_island", "seb", "html"}
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,119}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_URL_FIELDS = (
    "career_url", "source_url", "ats_url", "fallback_url", "evidence_url", "api_url", "greenhouse_api_url",
)
_SCAN_FIELDS = {"last_scan_at", "last_scan_status", "last_scan_error", "last_scan_jobs_count"}
_SOURCE_FIELDS = (
    "name", "collection_enabled", "career_url", "ats_url", "ats_type", "ats_token", "ats_sources",
    "api_url", "fallback_url", "greenhouse_api_url", "greenhouse_metadata_filter", "vacancy_company_name", "region", "language", "locale",
)
_BOARD_OPTIONS = ("ats_sources", "api_url", "fallback_url", "region", "language", "locale", "greenhouse_api_url", "greenhouse_metadata_filter", "vacancy_company_name")


def source_configuration(data: dict) -> tuple:
    return tuple((bool(data.get(field)) if field == "collection_enabled" else data.get(field) or None) for field in _SOURCE_FIELDS)


def _text(value: Any, field: str, *, limit: int = 4000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{field} must be text of at most {limit} characters.")
    return value.strip()


def _name_key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _url(value: Any, field: str) -> str | None:
    text = _text(value, field, limit=2048)
    if not text:
        return None
    try:
        parts = urlsplit(text)
        valid = parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password
        _ = parts.port
    except ValueError:
        valid = False
    if not valid or any(char.isspace() or ord(char) < 32 for char in text):
        raise ValueError(f"{field} must be an HTTP or HTTPS URL without credentials.")
    return text


def detect_ats(url: str | None) -> tuple[str, str | None]:
    """Recognize public job board URLs without fetching user-provided hosts."""
    if not url:
        return "unknown", None
    parts = urlsplit(url)
    path = [unquote(part) for part in parts.path.split("/") if part]
    host = (parts.hostname or "").lower()
    if host.endswith(".myworkdayjobs.com"):
        return "workday", host.split(".")[0]
    if host.endswith(".recruitee.com"):
        return "recruitee", host.removesuffix(".recruitee.com")
    for suffix in (".jobs.personio.de", ".jobs.personio.com"):
        if host.endswith(suffix):
            return "personio", host.removesuffix(suffix)
    if not path:
        return "unknown", None
    providers = {
        "jobs.ashbyhq.com": "ashby",
        "jobs.lever.co": "lever",
        "jobs.eu.lever.co": "lever",
        "boards.greenhouse.io": "greenhouse",
        "job-boards.greenhouse.io": "greenhouse",
        "apply.workable.com": "workable",
        "jobs.smartrecruiters.com": "smartrecruiters",
        "careers.smartrecruiters.com": "smartrecruiters",
    }
    provider = providers.get(host)
    if provider == "workable" and path[0] in {"j", "api"}:
        return "unknown", None
    if provider and _TOKEN.fullmatch(path[0]):
        return provider, path[0]
    return "unknown", None


def normalize_company(payload: dict, *, company_id: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Each company must be a JSON object.")
    data = {key: value for key, value in payload.items() if key not in _SCAN_FIELDS and key != "source_revision"}
    name = _text(data.get("name"), "name", limit=200)
    if not name:
        raise ValueError("Company name is required.")
    identity = company_id or _text(data.get("company_id"), "company_id", limit=120)
    if not identity:
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        identity = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")[:120]
    if not identity or not _ID.fullmatch(identity):
        raise ValueError("Company ID must contain lowercase letters, digits, hyphens, or underscores.")
    data.update(company_id=identity, name=name)
    for field in _URL_FIELDS:
        data[field] = _url(data.get(field), field)
    aliases = data.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 50:
        raise ValueError("Aliases must be a list of at most 50 names.")
    data["aliases"] = list(dict.fromkeys(_text(value, "alias", limit=200) for value in aliases if value))
    pool = _text(data.get("pool", "LT"), "pool", limit=80)
    data["pool"] = pool or "LT"
    priority = data.get("priority", 2)
    if isinstance(priority, bool):
        raise ValueError("Priority must be 1, 2, or 3.")
    try:
        priority = int(str(priority))
    except (TypeError, ValueError) as error:
        raise ValueError("Priority must be 1, 2, or 3.") from error
    if priority not in {1, 2, 3}:
        raise ValueError("Priority must be 1, 2, or 3.")
    data["priority"] = priority
    for field, default in (
        ("notes", ""), ("check_status", "unverified"), ("remote_eligibility", "check_each_job"),
    ):
        data[field] = _text(data.get(field, default), field)
    checked_on = _text(data.get("checked_on"), "checked_on", limit=10)
    if checked_on:
        try:
            date.fromisoformat(checked_on)
        except ValueError as error:
            raise ValueError("Checked date must use YYYY-MM-DD.") from error
    data["checked_on"] = checked_on or None
    data["vacancy_company_name"] = _text(data.get("vacancy_company_name"), "vacancy_company_name", limit=200)
    for field in ("language", "locale", "region"):
        data[field] = _text(data.get(field), field, limit=80)
    metadata_filter = data.get("greenhouse_metadata_filter")
    if metadata_filter:
        if not isinstance(metadata_filter, dict) or set(metadata_filter) != {"id", "value"}:
            raise ValueError("Greenhouse metadata filter requires an id and value.")
        filter_id = metadata_filter["id"]
        if not isinstance(filter_id, str | int) or isinstance(filter_id, bool):
            raise ValueError("Greenhouse metadata filter id must be text or an integer.")
        filter_id = _text(str(filter_id), "Greenhouse metadata filter id", limit=120)
        filter_value = _text(metadata_filter["value"], "Greenhouse metadata filter value", limit=200)
        if not filter_id or not filter_value:
            raise ValueError("Greenhouse metadata filter requires a non-empty id and value.")
        data["greenhouse_metadata_filter"] = {"id": filter_id, "value": filter_value}
    ats_type = _text(data.get("ats_type", "unknown"), "ats_type", limit=40).lower()
    token = _text(data.get("ats_token"), "ats_token", limit=120) or None
    if token and not _TOKEN.fullmatch(token):
        raise ValueError("ATS token must be a board identifier, without slashes or query parameters.")
    inferred_type, inferred_token = detect_ats(data.get("ats_url") or data.get("career_url"))
    if inferred_type != "unknown" and (ats_type in {"", "unknown"} or not token):
        ats_type, token = inferred_type, inferred_token
    if token and not _TOKEN.fullmatch(token):
        raise ValueError("ATS token must be a board identifier, without slashes or query parameters.")
    data.update(ats_type=ats_type or "unknown", ats_token=token)
    enabled = data.get("collection_enabled", data.get("collector_ready", False))
    if not isinstance(enabled, bool):
        raise ValueError("Collection enabled must be true or false.")
    data["collection_enabled"] = enabled
    sources = data.get("ats_sources", [])
    if not isinstance(sources, list) or len(sources) > 20:
        raise ValueError("Additional career boards must be a list of at most 20 sources.")
    normalized_sources = []
    board_fields = ("ats_type", "ats_token", "ats_url", "career_url", "api_url", "region", "language", "locale", "greenhouse_api_url", "greenhouse_metadata_filter", "vacancy_company_name")
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Each additional career board must be an object.")
        normalized = normalize_company({
            "name": name, "company_id": identity, "collection_enabled": True,
            **{key: source[key] for key in board_fields if key in source},
        })
        normalized_sources.append({key: normalized[key] for key in board_fields if key in normalized})
    if normalized_sources:
        data["ats_sources"] = normalized_sources
        if ats_type not in SUPPORTED_ATS or (not token and ats_type not in {"html", "astro_server_island"}):
            for field, value in normalized_sources[0].items():
                if field != "career_url" or not data.get(field):
                    data[field] = value
            ats_type, token = data["ats_type"], data.get("ats_token")
    if enabled and ats_type in {"", "unknown"} and (data.get("career_url") or data.get("ats_url")):
        ats_type, token = "html", None
        data.update(ats_type=ats_type, ats_token=token)
    if enabled and (ats_type not in SUPPORTED_ATS or (not token and ats_type not in {"html", "astro_server_island"})):
        raise ValueError("Automatic collection requires a supported board or an HTML career page.")
    if enabled and ats_type in {"html", "astro_server_island"} and not (data.get("career_url") or data.get("ats_url")):
        raise ValueError("HTML collection requires a career URL.")
    return data


def read_registry(data: bytes, filename: str) -> list[dict]:
    """Read only companies.json from an archive; never extract or execute files."""
    if len(data) > MAX_REGISTRY_BYTES:
        raise ValueError("Registry upload exceeds the 5 MiB limit.")
    try:
        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                candidates = [
                    entry for entry in archive.infolist()
                    if entry.filename in {"companies.json", "career_registry/companies.json"}
                ]
                if len(candidates) != 1:
                    raise ValueError("ZIP must contain exactly one companies.json or career_registry/companies.json.")
                if candidates[0].file_size > MAX_REGISTRY_BYTES:
                    raise ValueError("Uncompressed registry exceeds the 5 MiB limit.")
                with archive.open(candidates[0]) as stream:
                    data = stream.read(MAX_REGISTRY_BYTES + 1)
                if len(data) > MAX_REGISTRY_BYTES:
                    raise ValueError("Uncompressed registry exceeds the 5 MiB limit.")
        elif not filename.lower().endswith(".json"):
            raise ValueError("Upload a JSON registry or a ZIP containing companies.json.")
        payload = json.loads(data.decode("utf-8-sig"))
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, RuntimeError, NotImplementedError) as error:
        raise ValueError("Registry file is not valid JSON or ZIP.") from error
    if not isinstance(payload, list) or len(payload) > MAX_COMPANIES:
        raise ValueError(f"Registry must be a JSON list of at most {MAX_COMPANIES} companies.")
    return [normalize_company(item) for item in payload]


class CompanyRegistry:
    def __init__(self, database: DatabaseManager) -> None:
        self.database = database

    @staticmethod
    def _record(row: Any) -> dict:
        data = json.loads(row["data_json"])
        data.update({field: row[field] for field in _SCAN_FIELDS})
        return data

    def get(self, company_id: str) -> dict | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM career_companies WHERE company_id = ?", (company_id,),
            ).fetchone()
        return self._record(row) if row else None

    def list_companies(self, query: str = "", pool: str = "", enabled_only: bool = False) -> list[dict]:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM career_companies").fetchall()
        records = [self._record(row) for row in rows]
        needle = _name_key(query)
        filtered = [
            item for item in records
            if (not pool or item["pool"] == pool)
            and (not enabled_only or item["collection_enabled"])
            and (not needle or needle in _name_key(" ".join([
                item["name"], *item["aliases"], item["notes"], item.get("career_url") or "",
            ])))
        ]
        return sorted(filtered, key=lambda item: (item["priority"], _name_key(item["name"])))

    def save(self, payload: dict, company_id: str | None = None) -> dict:
        payload = dict(payload)
        if company_id and payload.get("company_id") not in {None, "", company_id}:
            raise ValueError("Company ID cannot be changed after creation.")
        for field in _URL_FIELDS:
            if field in payload:
                payload[field] = _url(payload[field], field)
        if "ats_token" in payload:
            payload["ats_token"] = _text(payload["ats_token"], "ats_token", limit=120) or None
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM career_companies WHERE company_id = ?", (company_id,),
            ).fetchone() if company_id else None
            if company_id and not existing:
                raise ValueError("Company was not found.")
            merged = json.loads(existing["data_json"]) if existing else {}
            merged.update(payload)
            source_changed = False
            if existing:
                old = json.loads(existing["data_json"])
                if any(field in payload and payload[field] != old.get(field) for field in ("career_url", "ats_url")):
                    merged.update(check_status="unverified", checked_on=None, ats_type="unknown", ats_token=None)
                    if "career_url" in payload and (
                        "ats_url" not in payload
                        or (payload["ats_url"] == old.get("ats_url") == old.get("career_url"))
                    ) and (old.get("ats_url") == old.get("career_url") or detect_ats(payload["career_url"])[0] != "unknown"):
                        merged["ats_url"] = None
                    for field in ("ats_type", "ats_token"):
                        if payload.get(field) and payload[field] != old.get(field):
                            merged[field] = payload[field]
                old_board = (old.get("ats_type"), old.get("ats_token"), old.get("ats_url") or old.get("career_url"))
                inferred = detect_ats(merged.get("ats_url") or merged.get("career_url"))
                new_board = (
                    inferred[0] if merged.get("ats_type") in {None, "", "unknown"} else merged.get("ats_type"),
                    inferred[1] if not merged.get("ats_token") else merged.get("ats_token"),
                    merged.get("ats_url") or merged.get("career_url"),
                )
                if old_board != new_board:
                    for field in _BOARD_OPTIONS:
                        if field not in payload:
                            merged.pop(field, None)
            data = normalize_company(merged, company_id=company_id)
            source_changed = bool(existing) and source_configuration(data) != source_configuration(old)
            data["source_revision"] = (int(old.get("source_revision", 0)) + int(source_changed)) if existing else 0
            key = _name_key(data["name"])
            duplicate = connection.execute(
                "SELECT company_id FROM career_companies WHERE (name_key = ? OR company_id = ?) "
                "AND company_id != ?",
                (key, data["company_id"], company_id or ""),
            ).fetchone()
            if duplicate:
                raise ValueError("This company already exists. Edit its existing entry.")
            now = utc_now_iso()
            if existing:
                connection.execute(
                    "UPDATE career_companies SET name_key = ?, data_json = ?, updated_at = ? WHERE company_id = ?",
                    (key, json.dumps(data, ensure_ascii=False), now, company_id),
                )
                if source_changed:
                    connection.execute(
                        "UPDATE career_companies SET last_scan_at = NULL, last_scan_status = NULL, "
                        "last_scan_error = '', last_scan_jobs_count = NULL WHERE company_id = ?", (company_id,),
                    )
            else:
                connection.execute(
                    "INSERT INTO career_companies (company_id, name_key, data_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (data["company_id"], key, json.dumps(data, ensure_ascii=False), now, now),
                )
        return self.get(data["company_id"])

    def import_bytes(self, data: bytes, filename: str) -> dict[str, int]:
        companies = read_registry(data, filename)
        inserted = skipped = 0
        now = utc_now_iso()
        with self.database.transaction() as connection:
            for item in companies:
                result = connection.execute(
                    "INSERT OR IGNORE INTO career_companies "
                    "(company_id, name_key, data_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (item["company_id"], _name_key(item["name"]), json.dumps(item, ensure_ascii=False), now, now),
                )
                if result.rowcount:
                    inserted += 1
                else:
                    skipped += 1
        return {"inserted": inserted, "skipped": skipped}

    def export(self) -> list[dict]:
        return self.list_companies()

    def record_scan(self, company_id: str, status: str, error: str = "", jobs_count: int | None = None,
                    *, expected_revision: int | None = None) -> bool:
        with self.database.transaction() as connection:
            if expected_revision is not None:
                row = connection.execute("SELECT data_json FROM career_companies WHERE company_id = ?", (company_id,)).fetchone()
                if not row or json.loads(row["data_json"]).get("source_revision", 0) != expected_revision:
                    return False
            connection.execute(
                "UPDATE career_companies SET last_scan_at = ?, last_scan_status = ?, "
                "last_scan_error = ?, last_scan_jobs_count = ? WHERE company_id = ?",
                (utc_now_iso(), status, error[:2000], jobs_count, company_id),
            )
        return True
