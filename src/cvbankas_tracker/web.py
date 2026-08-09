from __future__ import annotations

import ipaddress
from pathlib import Path
from secrets import token_urlsafe
import socket
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import ApplicationStatus, ApplicationStatusOrigin, InboxPreferences
from .storage import DatabaseManager, bootstrap_database, canonicalize_source_url
from .tracking import ActionService, ApplicationTracker, utc_iso_to_local_datetime

_CSRF_COOKIE = "job_seeker_csrf"
_ALLOWED_STATUS = {status.value.lower(): status for status in ApplicationStatus}
_ALLOWED_SORTS = {"score", "newest", "title", "company"}


def validate_loopback_bind_host(host: str) -> str:
    """Return a normalized host only if it resolves exclusively to loopback."""

    candidate = (host or "").strip().strip("[]")
    if not candidate:
        raise ValueError("Web host is required.")
    if candidate in {"0.0.0.0", "::", "*"}:
        raise ValueError("Wildcard web binds are not allowed; use 127.0.0.1, ::1, or localhost.")
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        if candidate.lower() != "localhost":
            raise ValueError("Web host must be localhost or a loopback IP address.")
        try:
            infos = socket.getaddrinfo(candidate, None)
        except socket.gaierror as error:  # pragma: no cover - platform DNS failure
            raise ValueError(f"Could not resolve web host: {candidate}") from error
        addresses = {info[4][0] for info in infos}
        if not addresses or not all(ipaddress.ip_address(address).is_loopback for address in addresses):
            raise ValueError("localhost must resolve only to loopback addresses.")
        return "localhost"
    if not ip.is_loopback:
        raise ValueError("Web host must be loopback-only.")
    return candidate


def _host_without_port(value: str) -> str:
    host = value.strip()
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    return host.rsplit(":", 1)[0] if ":" in host and host.count(":") == 1 else host


def _is_loopback_request_host(value: str | None) -> bool:
    if not value:
        return False
    try:
        validate_loopback_bind_host(_host_without_port(value))
    except ValueError:
        return False
    return True


def _origin_matches_request(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not _is_loopback_request_host(parts.netloc):
        return False
    return parts.netloc.lower() == (request.headers.get("host") or "").lower()


def _csrf_token(request: Request) -> str:
    return request.cookies.get(_CSRF_COOKIE) or token_urlsafe(32)


def _render(templates: Jinja2Templates, request: Request, name: str, **values: Any):
    token = _csrf_token(request)
    response = templates.TemplateResponse(request, name, {"request": request, "csrf_token": token, **values})
    if request.cookies.get(_CSRF_COOKIE) != token:
        response.set_cookie(_CSRF_COOKIE, token, httponly=True, samesite="strict")
    return response


async def require_safe_post(request: Request) -> dict[str, str]:
    if not _is_loopback_request_host(request.headers.get("host")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsafe Host header for local dashboard.")
    if not _origin_matches_request(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unsafe Origin for local dashboard mutation.")
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
    if not form.get("csrf_token") or form.get("csrf_token") != request.cookies.get(_CSRF_COOKIE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid CSRF token.")
    return form


def _db(request: Request) -> DatabaseManager:
    return DatabaseManager(request.app.state.db_path)


def _safe_external_url(value: str | None) -> str:
    if not value:
        return "#"
    parts = urlsplit(value.strip())
    if parts.scheme in {"http", "https"} and parts.netloc:
        return value.strip()
    return "#"


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def _coerce_bool(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def _local_due_text(due_at_utc: str | None, timezone_name: str) -> str:
    if not due_at_utc:
        return "-"
    return f"{utc_iso_to_local_datetime(due_at_utc, timezone_name)} ({timezone_name})"


def create_app(db_path: str | Path, *, bootstrap: bool = True) -> FastAPI:
    resolved_db_path = Path(db_path).expanduser().resolve()
    if bootstrap:
        bootstrap_database(resolved_db_path)
        # First-run timezone discovery is display-only until the user saves the
        # Settings form; do not silently persist a confirmed timezone at startup.

    app = FastAPI(title="Job Seeker Local Dashboard")
    app.state.db_path = resolved_db_path
    root = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(root / "templates"))
    templates.env.filters["safe_external_url"] = _safe_external_url
    templates.env.filters["local_due"] = _local_due_text
    app.mount("/static", StaticFiles(directory=str(root / "static")), name="static")

    @app.get("/")
    def index() -> RedirectResponse:
        return _redirect("/today")

    @app.get("/today")
    def today(request: Request):
        database = _db(request)
        preferences = database.get_inbox_preferences()
        latest_run = database.get_latest_inbox_run()
        return _render(
            templates,
            request,
            "today.html",
            page_title="Today",
            inbox_items=database.query_inbox(preferences=preferences, new_only=True),
            reminders=database.query_action_reminders(),
            preferences=preferences,
            latest_run=latest_run,
            timezone=ActionService(database).resolve_user_timezone(),
        )

    @app.get("/vacancies")
    def vacancies(request: Request):
        database = _db(request)
        preferences = database.get_inbox_preferences()
        latest_run = database.get_latest_inbox_run()
        return _render(
            templates,
            request,
            "vacancies.html",
            page_title="Vacancies",
            items=database.query_inbox(preferences=preferences),
            preferences=preferences,
            latest_run=latest_run,
        )

    @app.get("/vacancy")
    def vacancy_detail(request: Request, url: str):
        database = _db(request)
        vacancy = database.get_vacancy(url)
        if vacancy is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vacancy not found.")
        return _render(
            templates,
            request,
            "vacancy_detail.html",
            page_title=vacancy.title,
            vacancy=vacancy,
            analysis=database.get_latest_analysis(url),
            application=database.get_application_record(url),
            history=database.list_application_status_events(url),
            actions=database.list_action_items(vacancy_source_url=url),
            statuses=list(ApplicationStatus),
        )

    @app.get("/applications")
    def applications(request: Request):
        database = _db(request)
        return _render(
            templates,
            request,
            "applications.html",
            page_title="Applications",
            applications=database.list_tracked_applications(),
            statuses=list(ApplicationStatus),
        )

    @app.post("/applications/status")
    def update_application_status(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        source_url = form.get("vacancy_source_url", "")
        status_key = form.get("status", "").lower()
        if status_key not in _ALLOWED_STATUS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown application status.")
        database = _db(request)
        canonical_url = canonicalize_source_url(source_url)
        if not canonical_url or database.get_vacancy(canonical_url) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vacancy not found for status update.")
        tracker = ApplicationTracker(database)
        latest_analysis_id = database.get_latest_analysis_id(canonical_url)
        tracker.ensure_record(canonical_url, analysis_id=latest_analysis_id, origin=ApplicationStatusOrigin.WEB)
        try:
            tracker.update_status(canonical_url, _ALLOWED_STATUS[status_key], origin=ApplicationStatusOrigin.WEB)
        except ValueError:
            reason = form.get("correction_reason", "").strip()
            if not reason:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid transition; provide a correction reason.")
            tracker.set_status(
                canonical_url,
                _ALLOWED_STATUS[status_key],
                analysis_id=latest_analysis_id,
                origin=ApplicationStatusOrigin.WEB,
                reason=reason,
                notes=form.get("notes", ""),
            )
        return _redirect(f"/vacancy?{urlencode({'url': canonical_url})}")

    @app.get("/actions")
    def actions(request: Request):
        database = _db(request)
        return _render(
            templates,
            request,
            "actions.html",
            page_title="Actions",
            actions=database.list_action_items(),
            vacancies=database.list_vacancies_with_latest_scores(),
            timezone=ActionService(database).resolve_user_timezone(),
        )

    @app.post("/actions/create")
    def create_action(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        try:
            ActionService(_db(request)).create_action(
                vacancy_source_url=form.get("vacancy_source_url", ""),
                title=form.get("title", ""),
                notes=form.get("notes", ""),
                local_due_at=form.get("due_at") or None,
                fold=int(form["fold"]) if form.get("fold") in {"0", "1"} else None,
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        return _redirect("/actions")

    @app.post("/actions/complete")
    def complete_action(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        try:
            ActionService(_db(request)).complete_action(int(form.get("action_id", "0")))
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        return _redirect("/actions")

    @app.post("/actions/reopen")
    def reopen_action(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        try:
            ActionService(_db(request)).reopen_action(int(form.get("action_id", "0")))
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        return _redirect("/actions")

    @app.get("/settings")
    def settings(request: Request):
        database = _db(request)
        return _render(
            templates,
            request,
            "settings.html",
            page_title="Settings",
            preferences=database.get_inbox_preferences(),
            timezone=ActionService(database).resolve_user_timezone(),
        )

    @app.post("/settings")
    def save_settings(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        sort_by = form.get("sort_by", "score")
        if sort_by not in _ALLOWED_SORTS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid inbox sort.")
        try:
            preferences = InboxPreferences(
                minimum_score=int(form.get("minimum_score", "0")),
                hide_below_threshold=_coerce_bool(form.get("hide_below_threshold")),
                sort_by=sort_by,
                source_name=form.get("source_name", ""),
                fit_label=form.get("fit_label", ""),
                application_status=form.get("application_status", ""),
                new_only=_coerce_bool(form.get("new_only")),
                current_run_only=_coerce_bool(form.get("current_run_only")),
            )
            database = _db(request)
            database.save_inbox_preferences(preferences)
            timezone_name = form.get("timezone", "").strip()
            if timezone_name:
                ActionService(database).set_user_timezone(timezone_name)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        return _redirect("/settings")

    return app


def run_web(db_path: str | Path, *, host: str = "127.0.0.1", port: int = 8000) -> int:
    import uvicorn

    safe_host = validate_loopback_bind_host(host)
    app = create_app(db_path)
    uvicorn.run(app, host=safe_host, port=int(port))
    return 0
