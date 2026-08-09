from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from .models import (
    ActionItem,
    ApplicationRecord,
    ApplicationStatus,
    ApplicationStatusEventKind,
    ApplicationStatusOrigin,
)
from .storage import DatabaseManager

_WINDOWS_TZID_CANDIDATES = {
    "FLE Standard Time": ("Europe/Vilnius", "Europe/Kyiv", "Europe/Helsinki"),
    "E. Europe Standard Time": ("Europe/Chisinau", "Europe/Kyiv"),
    "GTB Standard Time": ("Europe/Athens", "Europe/Bucharest"),
    "Central Europe Standard Time": ("Europe/Budapest", "Europe/Warsaw"),
    "Central European Standard Time": ("Europe/Warsaw", "Europe/Prague"),
    "W. Europe Standard Time": ("Europe/Berlin", "Europe/Paris"),
    "GMT Standard Time": ("Europe/London",),
    "UTC": ("UTC",),
}


def local_datetime_to_utc_iso(
    local_value: str,
    timezone_name: str,
    *,
    fold: int | None = None,
) -> str:
    """Convert a UI-local wall time to a UTC ISO instant.

    Ambiguous DST times require ``fold`` (0 for earlier, 1 for later). Local
    times that do not exist due to a DST jump are rejected.
    """

    naive = datetime.fromisoformat(local_value)
    if naive.tzinfo is not None:
        raise ValueError("Local reminder input must not include a timezone offset.")
    zone = ZoneInfo(timezone_name)
    valid_folds: list[tuple[int, datetime]] = []
    for candidate_fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=candidate_fold)
        round_trip = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip == naive:
            valid_folds.append((candidate_fold, aware))
    if not valid_folds:
        raise ValueError(f"Local time does not exist in {timezone_name}: {local_value}")
    ambiguous = len(valid_folds) == 2 and valid_folds[0][1].utcoffset() != valid_folds[1][1].utcoffset()
    if ambiguous:
        if fold not in {0, 1}:
            raise ValueError("Ambiguous local time requires fold=0 (earlier) or fold=1 (later).")
        aware = next(value for candidate_fold, value in valid_folds if candidate_fold == fold)
    else:
        aware = valid_folds[0][1]
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_iso_to_local_datetime(utc_value: str, timezone_name: str) -> str:
    parsed = datetime.fromisoformat(utc_value.replace("Z", "+00:00"))
    return parsed.astimezone(ZoneInfo(timezone_name)).replace(microsecond=0).isoformat()


def _valid_timezone_name(timezone_name: str) -> str | None:
    try:
        ZoneInfo(timezone_name)
    except Exception:
        return None
    return timezone_name


def _windows_timezone_key() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except Exception:
        return None
    return str(value).strip() or None


def _timezone_offsets_match(timezone_name: str, years: tuple[int, ...] | None = None) -> bool:
    years = years or (datetime.now().year, datetime.now().year + 1)
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        return False
    for year in years:
        for month in (1, 7):
            probe = datetime(year, month, 1, 12, 0, tzinfo=timezone.utc)
            expected = probe.astimezone().utcoffset()
            actual = probe.astimezone(zone).utcoffset()
            if expected != actual:
                return False
    return True


def _discover_timezone_by_offsets(default: str) -> str:
    local_names = {name for name in time.tzname if name}
    matches: list[str] = []
    for timezone_name in sorted(available_timezones()):
        if not timezone_name.startswith(("Europe/", "Etc/", "UTC")):
            continue
        if not _timezone_offsets_match(timezone_name):
            continue
        zone = ZoneInfo(timezone_name)
        abbreviations = {
            datetime(datetime.now().year, month, 1, 12, 0, tzinfo=timezone.utc)
            .astimezone(zone)
            .tzname()
            for month in (1, 7)
        }
        if local_names and abbreviations.isdisjoint(local_names):
            continue
        matches.append(timezone_name)
    if "Europe/Vilnius" in matches:
        return "Europe/Vilnius"
    return matches[0] if matches else default


def discover_local_timezone(default: str = "UTC") -> str:
    """Best-effort IANA timezone discovery for first-run UI defaults.

    Windows exposes a local ``datetime.timezone`` without an IANA ``key``; map
    the registry Windows time-zone id first, then verify candidates against the
    current machine's local offsets so Europe/Vilnius is resolved on Lithuanian
    desktops instead of falling back to UTC.
    """

    env_tz = os.environ.get("TZ", "").strip()
    if env_tz and _valid_timezone_name(env_tz):
        return env_tz

    local_tz = datetime.now().astimezone().tzinfo
    key = getattr(local_tz, "key", None)
    if isinstance(key, str) and key and _valid_timezone_name(key):
        return key

    windows_key = _windows_timezone_key()
    if windows_key:
        for timezone_name in _WINDOWS_TZID_CANDIDATES.get(windows_key, ()):
            if _valid_timezone_name(timezone_name) and _timezone_offsets_match(timezone_name):
                return timezone_name

    return _discover_timezone_by_offsets(default)


class ApplicationTracker:
    def __init__(self, database: DatabaseManager) -> None:
        self._database = database

    def ensure_record(
        self,
        vacancy_source_url: str,
        analysis_id: int | None = None,
        notes: str = "",
        origin: ApplicationStatusOrigin = ApplicationStatusOrigin.CLI,
    ) -> ApplicationRecord:
        record = self._database.get_application_record(vacancy_source_url)
        if record is not None:
            if analysis_id is not None and record.analysis_id != analysis_id:
                record.analysis_id = analysis_id
                self._database.save_application_record(record)
            return record

        return self.create_record(
            vacancy_source_url=vacancy_source_url,
            analysis_id=analysis_id,
            notes=notes,
            origin=origin,
        )

    def create_record(
        self,
        vacancy_source_url: str,
        analysis_id: int | None = None,
        notes: str = "",
        origin: ApplicationStatusOrigin = ApplicationStatusOrigin.CLI,
    ) -> ApplicationRecord:
        record = ApplicationRecord(
            vacancy_source_url=vacancy_source_url,
            analysis_id=analysis_id,
            notes=notes,
        )
        self._database.create_application_record_with_event(
            record,
            origin=origin,
            note=notes,
        )
        return record

    def update_status(
        self,
        vacancy_source_url: str,
        new_status: ApplicationStatus,
        *,
        origin: ApplicationStatusOrigin = ApplicationStatusOrigin.CLI,
    ) -> ApplicationRecord:
        record = self._database.get_application_record(vacancy_source_url)
        if record is None:
            raise ValueError(f"No application record exists for {vacancy_source_url}.")

        record.update_status(new_status)
        return self._database.update_application_status_with_event(
            vacancy_source_url,
            new_status,
            origin=origin,
            kind=ApplicationStatusEventKind.NORMAL,
        )

    def set_status(
        self,
        vacancy_source_url: str,
        new_status: ApplicationStatus,
        analysis_id: int | None = None,
        notes: str = "",
        origin: ApplicationStatusOrigin = ApplicationStatusOrigin.CLI,
        reason: str = "",
    ) -> ApplicationRecord:
        if not reason.strip():
            raise ValueError("Corrective status reassignment requires a reason.")
        record = self.ensure_record(
            vacancy_source_url=vacancy_source_url,
            analysis_id=analysis_id,
            notes=notes,
        )
        if record.status == new_status:
            return record

        return self._database.update_application_status_with_event(
            vacancy_source_url,
            new_status,
            origin=origin,
            kind=ApplicationStatusEventKind.CORRECTIVE,
            reason=reason,
            note=notes,
            analysis_id=analysis_id,
        )

    def update_notes(self, vacancy_source_url: str, notes: str) -> ApplicationRecord:
        record = self._database.get_application_record(vacancy_source_url)
        if record is None:
            raise ValueError(f"No application record exists for {vacancy_source_url}.")

        record.set_notes(notes)
        self._database.save_application_record(record)
        return record


class ActionService:
    def __init__(self, database: DatabaseManager) -> None:
        self._database = database

    def set_user_timezone(self, timezone_name: str) -> None:
        self._database.save_user_timezone(timezone_name, confirmed=True)

    def resolve_user_timezone(self) -> str:
        timezone_name = self._database.get_user_timezone()
        if self._database.get_user_timezone_confirmation() is not None:
            return timezone_name
        return discover_local_timezone(default=timezone_name)

    def get_user_timezone(self) -> str:
        return self.resolve_user_timezone()

    def confirm_user_timezone(self, timezone_name: str | None = None) -> str:
        resolved = timezone_name or self.resolve_user_timezone()
        self._database.save_user_timezone(resolved, confirmed=True)
        return resolved

    def ensure_user_timezone_confirmed(self) -> str:
        return self.resolve_user_timezone()

    def local_due_to_utc(self, local_value: str, *, fold: int | None = None) -> str:
        return local_datetime_to_utc_iso(local_value, self.get_user_timezone(), fold=fold)

    def due_utc_to_local(self, utc_value: str | None) -> str:
        if not utc_value:
            return "-"
        return utc_iso_to_local_datetime(utc_value, self.get_user_timezone())

    def create_action(
        self,
        *,
        vacancy_source_url: str,
        title: str,
        notes: str = "",
        local_due_at: str | None = None,
        fold: int | None = None,
    ) -> ActionItem:
        due_at_utc = (
            self.local_due_to_utc(local_due_at, fold=fold)
            if local_due_at
            else None
        )
        return self._database.create_action_item(
            vacancy_source_url=vacancy_source_url,
            title=title,
            notes=notes,
            due_at_utc=due_at_utc,
        )

    def update_action(
        self,
        action_id: int,
        *,
        title: str | None = None,
        notes: str | None = None,
        local_due_at: str | None = None,
        clear_due: bool = False,
        fold: int | None = None,
    ) -> ActionItem:
        if clear_due and local_due_at:
            raise ValueError("Use either a new due time or clear_due, not both.")
        due_at_utc = None
        due_changed = clear_due or local_due_at is not None
        if local_due_at:
            due_at_utc = self.local_due_to_utc(local_due_at, fold=fold)
        kwargs = {"title": title, "notes": notes}
        if due_changed:
            kwargs["due_at_utc"] = due_at_utc
        return self._database.update_action_item(
            action_id,
            **kwargs,
        )

    def complete_action(self, action_id: int) -> ActionItem:
        return self._database.complete_action_item(action_id)

    def reopen_action(self, action_id: int) -> ActionItem:
        return self._database.reopen_action_item(action_id)
