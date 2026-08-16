from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import os
import socket
import tempfile
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .io_utils import ProfileFileReader
from .main import (
    _optional_env,
    parse_search_keywords,
    resolve_ai_backend,
    run_batch,
    run_import,
)
from .models import ApplicationStatus, ApplicationStatusOrigin, InboxPreferences
from .profile_builder import (
    AI_PROFILE_BACKENDS,
    MAX_CV_UPLOAD_BYTES,
    SUPPORTED_CV_SUFFIXES,
    ProfileGenerationError,
    extract_cv_text,
    generate_profile_dict,
    write_profile_json,
)
from .storage import DatabaseManager, bootstrap_database, canonicalize_source_url
from .tracking import ActionService, ApplicationTracker, utc_iso_to_local_datetime
from .web_jobs import JobConflictError, JobManager
from .web_scheduler import (
    DailyScheduler,
    ScheduleConfig,
    ScheduleError,
    SchedulerBusyError,
)

_CSRF_COOKIE = "job_seeker_csrf"
_ALLOWED_STATUS = {status.value.lower(): status for status in ApplicationStatus}
_ALLOWED_SORTS = {"score", "newest", "title", "company"}
_ALLOWED_STRATEGIES = {"ai", "rule"}
WEB_BACKEND_CHOICES = ("rule", "demo", "openai", "claude_cli", "codex_cli")
WEB_SOURCE_CHOICES = ("cvbankas", "hh", "startup_jobs", "justjoin", "euremotejobs", "sample")
_DEFAULT_EXPORT = "exports/job_seeker_report.md"
_DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


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
            raise ValueError("Web host must be localhost or a loopback IP address.") from None
        try:
            infos = socket.getaddrinfo(candidate, None)
        except socket.gaierror as error:  # pragma: no cover - platform DNS failure
            raise ValueError(f"Could not resolve web host: {candidate}") from error
        addresses = {info[4][0] for info in infos}
        if not addresses or not all(ipaddress.ip_address(address).is_loopback for address in addresses):
            raise ValueError("localhost must resolve only to loopback addresses.") from None
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


_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    """Cache-busting token derived from the stylesheet mtime.

    Appended to the CSS URL so browsers fetch a fresh file after every edit
    instead of serving a stale cached copy.
    """
    try:
        return str(int((_STATIC_DIR / "dashboard.css").stat().st_mtime))
    except OSError:
        return "1"


def _render(templates: Jinja2Templates, request: Request, name: str, **values: Any):
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request,
        name,
        {"request": request, "csrf_token": token, "asset_version": _asset_version(), **values},
    )
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


def resolve_web_profile_path(user_path: str, *, base_dir: Path) -> Path:
    """Constrain a browser-submitted profile save path to the app working tree.

    The dashboard is loopback-only and CSRF-protected, but ``save_path`` is still
    untrusted input from a form. A generated profile may only be written *inside*
    ``base_dir`` (the process working directory): absolute paths and ``..``
    escapes are rejected, so this endpoint can never clobber a file elsewhere on
    disk. CLI/TUI callers keep full path freedom — the constraint lives at the
    web boundary, not in ``write_profile_json``.
    """
    base = Path(base_dir).resolve()
    raw = (user_path or "").strip() or "profile_from_cv.json"
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError("Profile path must stay inside the application directory.")
    if resolved.is_dir():
        raise ValueError("Profile path must be a file, not a directory.")
    return resolved


def _coerce_bool(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def _positive_int(value: str | None, *, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _clamp_percent(value: str | None, *, default: int) -> int:
    """Parse a 0–100 percentage from form input, falling back to ``default``."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(0, min(100, parsed))


def _backend_context(next_path: str) -> dict[str, Any]:
    return {
        "backend": resolve_ai_backend(),
        "web_backend_choices": WEB_BACKEND_CHOICES,
        "ai_backends": AI_PROFILE_BACKENDS,
        "openai_key_present": bool((os.getenv("OPENAI_API_KEY") or "").strip()),
        "backend_form_next": next_path,
    }


def _local_due_text(due_at_utc: str | None, timezone_name: str) -> str:
    if not due_at_utc:
        return "-"
    return f"{utc_iso_to_local_datetime(due_at_utc, timezone_name)} ({timezone_name})"


async def require_safe_multipart(
    request: Request,
    csrf_token: str = Form(...),
) -> None:
    """CSRF/Origin guard for multipart form posts (file uploads)."""
    if not _is_loopback_request_host(request.headers.get("host")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsafe Host header for local dashboard.")
    if not _origin_matches_request(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unsafe Origin for local dashboard mutation.")
    if not csrf_token or csrf_token != request.cookies.get(_CSRF_COOKIE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid CSRF token.")


def _runner_namespace(
    *,
    profile_path: str,
    db_path: str,
    enabled_sources: list[str],
    keywords: list[str],
    limit: int,
    max_pages: int,
    analysis_strategy: str,
    refresh: bool,
    import_urls: str = "",
    daily_run: bool = False,
    prune_threshold: int | None = None,
    auto_save: bool = True,
    auto_save_threshold: int = 0,
    infinite: bool = False,
) -> argparse.Namespace:
    effective_keywords = keywords or ["automation"]
    return argparse.Namespace(
        prune_threshold=prune_threshold,
        auto_save=auto_save,
        auto_save_threshold=auto_save_threshold,
        infinite=infinite,
        config="",
        profile=profile_path,
        db=str(db_path),
        export=_DEFAULT_EXPORT,
        daily_run=daily_run,
        keyword=effective_keywords[0],
        keywords="\n".join(effective_keywords),
        listing_url="",
        limit=limit,
        max_pages=max_pages,
        source="live",
        sources=",".join(enabled_sources),
        cvbankas=False,
        analysis_strategy=analysis_strategy,
        openai_model=_DEFAULT_OPENAI_MODEL,
        refresh=refresh,
        import_urls=import_urls,
        import_urls_file="",
        enabled_sources=list(enabled_sources),
        search_keywords=list(effective_keywords),
        list_vacancies=False,
        list_tracked=False,
        vacancy_url="",
        vacancy_id="",
        vacancy_source="",
        show_vacancy=False,
        status=None,
        note=None,
        export_tracked="",
    )


_MAX_PROFILE_SEARCH_KEYWORDS = 12


def _profile_search_keywords(profile_path: str) -> list[str]:
    """Derive search terms from the active profile for profile-based search.

    Prefers the profile's ``additional_keywords`` (which are written as short
    search phrases) and falls back to the target role titles. Returns an empty
    list when the profile cannot be read, so the caller can surface a clear
    error instead of silently searching for nothing.
    """
    try:
        profile = ProfileFileReader().read(profile_path)
    except (OSError, ValueError, KeyError):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    # Prefer explicit search keywords and target roles, then fall back to the
    # candidate's skills so any non-empty profile still yields search terms.
    for term in [
        *profile.additional_keywords,
        *profile.target_roles,
        *profile.must_have_skills,
        *profile.skills,
    ]:
        text = str(term).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        ordered.append(text)
        if len(ordered) >= _MAX_PROFILE_SEARCH_KEYWORDS:
            break
    return ordered


def _build_run_cfg(base_cfg: dict, enabled_sources: list[str], keywords: list[str]) -> dict:
    run_cfg = copy.deepcopy(base_cfg or {})
    if not keywords:
        return run_cfg
    run_cfg.setdefault("sources", {})
    run_cfg["sources"].setdefault("keywords", {})
    for source_name in enabled_sources:
        run_cfg["sources"]["keywords"][source_name] = list(keywords)
    return run_cfg


def _telegram_ready() -> bool:
    return bool((os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()) and bool(
        (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    )


def _start_daily_job(app: FastAPI, schedule: ScheduleConfig) -> int:
    """Start the daily collection + Telegram summary as a background job.

    Returns the job id, or raises ``SchedulerBusyError`` if a job is active.
    """
    sources = list(schedule.sources) or list(WEB_SOURCE_CHOICES)
    keywords = list(schedule.keywords)
    strategy = schedule.analysis_strategy if schedule.analysis_strategy in _ALLOWED_STRATEGIES else "ai"
    args = _runner_namespace(
        profile_path=app.state.profile_path,
        db_path=str(app.state.db_path),
        enabled_sources=sources,
        keywords=keywords,
        limit=schedule.limit,
        max_pages=schedule.max_pages,
        analysis_strategy=strategy,
        refresh=False,
        daily_run=True,
    )
    run_cfg = _build_run_cfg(app.state.cfg, sources, keywords)
    try:
        job = app.state.jobs.start("daily", lambda control: run_batch(args, run_cfg, control=control))
    except JobConflictError as error:
        raise SchedulerBusyError(str(error)) from error
    return job.id


def create_app(
    db_path: str | Path,
    *,
    bootstrap: bool = True,
    cfg: dict | None = None,
    profile_path: str = "sample_data/active_profile.json",
) -> FastAPI:
    resolved_db_path = Path(db_path).expanduser().resolve()
    if bootstrap:
        bootstrap_database(resolved_db_path)
        # First-run timezone discovery is display-only until the user saves the
        # Settings form; do not silently persist a confirmed timezone at startup.

    app = FastAPI(title="Job Seeker Local Dashboard")
    app.state.db_path = resolved_db_path
    app.state.cfg = cfg or {}
    # A profile the user previously marked active is persisted in the DB; restore
    # it on startup so the choice survives a restart/redeploy. Fall back to the
    # launch profile if the stored file is missing or nothing was saved.
    app.state.profile_path = profile_path
    if bootstrap:
        try:
            stored = DatabaseManager(resolved_db_path).get_active_profile_path()
            if stored and Path(stored).exists():
                app.state.profile_path = stored
        except Exception:
            pass
    app.state.jobs = JobManager()
    app.state.scheduler = DailyScheduler(
        resolved_db_path.parent / "scheduler.json",
        lambda schedule: _start_daily_job(app, schedule),
    )
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
        # Recent = every vacancy from the most recent run, regardless of score.
        recent_items = database.query_inbox(
            preferences=preferences,
            new_only=False,
            current_run_only=True,
            include_below_threshold=True,
        )
        return _render(
            templates,
            request,
            "today.html",
            page_title="Recent",
            inbox_items=recent_items,
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
            page_title="Saved",
            applications=database.list_tracked_applications(),
            statuses=list(ApplicationStatus),
            actions=database.list_action_items(),
            vacancies=database.list_vacancies_with_latest_scores(),
            timezone=ActionService(database).resolve_user_timezone(),
        )

    @app.post("/vacancy/save")
    def save_vacancy(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        database = _db(request)
        canonical_url = canonicalize_source_url(form.get("vacancy_source_url", ""))
        if not canonical_url or database.get_vacancy(canonical_url) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vacancy not found.")
        ApplicationTracker(database).ensure_record(
            canonical_url,
            analysis_id=database.get_latest_analysis_id(canonical_url),
            notes="Saved from the dashboard.",
            origin=ApplicationStatusOrigin.WEB,
        )
        return _redirect(form.get("next") or "/today")

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
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Invalid transition; provide a correction reason.",
                ) from None
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
    def actions() -> RedirectResponse:
        # Actions were merged into the Applications page.
        return _redirect("/applications")

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
        return _redirect("/applications")

    @app.post("/actions/complete")
    def complete_action(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        try:
            ActionService(_db(request)).complete_action(int(form.get("action_id", "0")))
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        return _redirect("/applications")

    @app.post("/actions/reopen")
    def reopen_action(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        try:
            ActionService(_db(request)).reopen_action(int(form.get("action_id", "0")))
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        return _redirect("/applications")

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
            **_backend_context("/settings"),
        )

    @app.post("/settings/backend")
    def set_backend(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        choice = (form.get("ai_backend", "") or "").strip().lower()
        if choice not in WEB_BACKEND_CHOICES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid AI backend.")
        os.environ["AI_BACKEND"] = choice
        dest = form.get("next", "/settings") or "/settings"
        if not dest.startswith("/"):
            dest = "/settings"
        return _redirect(dest)

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

    @app.get("/search")
    def search(request: Request):
        return _render(
            templates,
            request,
            "search.html",
            page_title="Search",
            sources=WEB_SOURCE_CHOICES,
            default_sources=["cvbankas", "hh", "justjoin"],
            default_limit=10,
            default_pages=1,
            default_prune_threshold=40,
            default_autosave_threshold=40,
            profile_keywords=_profile_search_keywords(request.app.state.profile_path),
        )

    @app.post("/search/start")
    def start_search(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        selected = [name for name in WEB_SOURCE_CHOICES if _coerce_bool(form.get(f"source_{name}"))]
        if not selected:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one source.")
        if _coerce_bool(form.get("use_keywords")):
            keywords = parse_search_keywords(form.get("keywords", ""))
            if not keywords:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Enter at least one keyword, or switch to profile search.",
                )
        else:
            keywords = _profile_search_keywords(request.app.state.profile_path)
            if not keywords:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "The active profile has no search terms. Tick "
                    "“Search by specific keywords” and enter some.",
                )
        strategy = form.get("analysis_strategy", "ai")
        if strategy not in _ALLOWED_STRATEGIES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid analysis strategy.")
        args = _runner_namespace(
            profile_path=request.app.state.profile_path,
            db_path=str(request.app.state.db_path),
            enabled_sources=selected,
            keywords=keywords,
            limit=_positive_int(form.get("limit"), default=10),
            max_pages=_positive_int(form.get("max_pages"), default=1),
            analysis_strategy=strategy,
            refresh=_coerce_bool(form.get("refresh")),
            prune_threshold=_clamp_percent(form.get("prune_threshold"), default=40),
            auto_save=_coerce_bool(form.get("auto_save")),
            auto_save_threshold=_clamp_percent(form.get("auto_save_threshold"), default=40),
            infinite=_coerce_bool(form.get("infinite")),
        )
        run_cfg = _build_run_cfg(request.app.state.cfg, selected, keywords)
        try:
            job = request.app.state.jobs.start("search", lambda control: run_batch(args, run_cfg, control=control))
        except JobConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return _redirect(f"/jobs/{job.id}")

    @app.get("/import")
    def import_page() -> RedirectResponse:
        # The URL-import form now lives on the Profile page.
        return _redirect("/profile")

    @app.post("/import/start")
    def start_import(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        raw_urls = form.get("import_urls", "").strip()
        if not raw_urls:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paste at least one URL.")
        args = _runner_namespace(
            profile_path=request.app.state.profile_path,
            db_path=str(request.app.state.db_path),
            enabled_sources=list(WEB_SOURCE_CHOICES),
            keywords=[],
            limit=_positive_int(form.get("limit"), default=10),
            max_pages=1,
            analysis_strategy=form.get("analysis_strategy", "ai")
            if form.get("analysis_strategy", "ai") in _ALLOWED_STRATEGIES
            else "ai",
            refresh=_coerce_bool(form.get("refresh")),
            import_urls=raw_urls,
        )
        run_cfg = _build_run_cfg(request.app.state.cfg, list(WEB_SOURCE_CHOICES), [])
        try:
            job = request.app.state.jobs.start("import", lambda control: run_import(args, run_cfg))
        except JobConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return _redirect(f"/jobs/{job.id}")

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: int):
        job = request.app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
        return _render(
            templates,
            request,
            "job.html",
            page_title=f"{job.kind.title()} job",
            job=job,
        )

    @app.get("/jobs/{job_id}/log")
    def job_log(request: Request, job_id: int):
        snapshot = request.app.state.jobs.snapshot(job_id)
        if snapshot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
        return JSONResponse(snapshot)

    def _job_control(request: Request, job_id: int, action: str) -> JSONResponse:
        jobs = request.app.state.jobs
        applied = {"pause": jobs.pause, "resume": jobs.resume, "cancel": jobs.cancel}[action](job_id)
        snapshot = jobs.snapshot(job_id)
        if snapshot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
        return JSONResponse({"applied": applied, **snapshot})

    @app.post("/jobs/{job_id}/pause")
    def job_pause(request: Request, job_id: int, _f: dict[str, str] = Depends(require_safe_post)):
        return _job_control(request, job_id, "pause")

    @app.post("/jobs/{job_id}/resume")
    def job_resume(request: Request, job_id: int, _f: dict[str, str] = Depends(require_safe_post)):
        return _job_control(request, job_id, "resume")

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel(request: Request, job_id: int, _f: dict[str, str] = Depends(require_safe_post)):
        return _job_control(request, job_id, "cancel")

    @app.get("/schedule")
    def schedule_page(request: Request):
        return _render(
            templates,
            request,
            "schedule.html",
            page_title="Schedule",
            schedule=request.app.state.scheduler.snapshot(),
            sources=WEB_SOURCE_CHOICES,
            strategies=sorted(_ALLOWED_STRATEGIES),
            telegram_ready=_telegram_ready(),
        )

    @app.post("/schedule/save")
    def schedule_save(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        selected = [name for name in WEB_SOURCE_CHOICES if _coerce_bool(form.get(f"source_{name}"))]
        if not selected:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one source.")
        strategy = form.get("analysis_strategy", "ai")
        if strategy not in _ALLOWED_STRATEGIES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid analysis strategy.")
        try:
            request.app.state.scheduler.update(
                enabled=_coerce_bool(form.get("enabled")),
                time=form.get("time", ""),
                sources=selected,
                keywords=parse_search_keywords(form.get("keywords", "")),
                limit=_positive_int(form.get("limit"), default=10),
                max_pages=_positive_int(form.get("max_pages"), default=1),
                analysis_strategy=strategy,
            )
        except ScheduleError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        return _redirect("/schedule")

    @app.post("/schedule/run-now")
    def schedule_run_now(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        try:
            job_id = _start_daily_job(request.app, request.app.state.scheduler.config)
        except SchedulerBusyError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return _redirect(f"/jobs/{job_id}")

    @app.get("/profile")
    def profile_page(request: Request):
        return _render(
            templates,
            request,
            "profile.html",
            page_title="Profile",
            **_profile_page_context(request),
        )

    @app.post("/profile/build")
    async def build_profile(
        request: Request,
        cv_file: UploadFile,
        _guard: None = Depends(require_safe_multipart),
    ):
        backend = resolve_ai_backend()
        if backend not in AI_PROFILE_BACKENDS:
            return _render(
                templates,
                request,
                "profile.html",
                page_title="Profile",
                error=(
                    "Building a profile from a CV needs an AI backend "
                    f"({', '.join(AI_PROFILE_BACKENDS)}). Current backend: {backend}. "
                    "Set AI_BACKEND=claude_cli or codex_cli, or AI_BACKEND=openai with OPENAI_API_KEY."
                ),
                **_profile_page_context(request),
            )

        suffix = Path(cv_file.filename or "").suffix.lower()
        if suffix not in SUPPORTED_CV_SUFFIXES:
            return _render(
                templates,
                request,
                "profile.html",
                page_title="Profile",
                error=f"Unsupported CV type '{suffix or '(none)'}'. Supported: {', '.join(SUPPORTED_CV_SUFFIXES)}.",
                **_profile_page_context(request),
            )

        # Read one byte past the cap so an oversized upload is detected without
        # buffering the whole (possibly huge) file into memory.
        contents = await cv_file.read(MAX_CV_UPLOAD_BYTES + 1)
        if len(contents) > MAX_CV_UPLOAD_BYTES:
            return _render(
                templates,
                request,
                "profile.html",
                page_title="Profile",
                error=f"CV is too large (limit {MAX_CV_UPLOAD_BYTES // (1024 * 1024)} MB).",
                **_profile_page_context(request),
            )
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(contents)
                tmp_path = Path(handle.name)
            cv_text = extract_cv_text(tmp_path)
            profile = generate_profile_dict(
                cv_text,
                backend=backend,
                openai_model=_DEFAULT_OPENAI_MODEL,
                claude_model=_optional_env("CLAUDE_CLI_MODEL"),
                codex_model=_optional_env("CODEX_CLI_MODEL"),
            )
        except ProfileGenerationError as error:
            return _render(
                templates,
                request,
                "profile.html",
                page_title="Profile",
                error=str(error),
                **_profile_page_context(request),
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        return _render(
            templates,
            request,
            "profile.html",
            page_title="Profile",
            generated=profile,
            generated_json=json.dumps(profile, ensure_ascii=False),
            default_save_path="profile_from_cv.json",
            **_profile_page_context(request),
        )

    @app.post("/profile/save")
    def save_profile(request: Request, form: dict[str, str] = Depends(require_safe_post)):
        raw_json = form.get("profile_json", "")
        try:
            profile = json.loads(raw_json)
        except json.JSONDecodeError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid profile data: {error}") from error
        if not isinstance(profile, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid profile data: expected a JSON object.")

        try:
            target = resolve_web_profile_path(
                form.get("save_path", ""), base_dir=Path.cwd()
            )
            written = write_profile_json(target, profile)
            ProfileFileReader().read(written)
        except Exception as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not save profile: {error}") from error

        if _coerce_bool(form.get("make_active")):
            request.app.state.profile_path = str(written)
            # Persist the choice so it survives a dashboard restart/redeploy.
            _db(request).save_active_profile_path(str(written))
        return _redirect("/profile")

    def _profile_page_context(request: Request) -> dict[str, Any]:
        active_path = request.app.state.profile_path
        try:
            active_profile = ProfileFileReader().read(active_path)
        except Exception:
            active_profile = None
        return {
            "active_path": active_path,
            "active_profile": active_profile,
            "supported_suffixes": SUPPORTED_CV_SUFFIXES,
            **_backend_context("/profile"),
        }

    return app


def run_web(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    cfg: dict | None = None,
    profile_path: str = "sample_data/active_profile.json",
) -> int:
    import uvicorn

    safe_host = validate_loopback_bind_host(host)
    app = create_app(db_path, cfg=cfg, profile_path=profile_path)
    app.state.scheduler.start()
    try:
        uvicorn.run(app, host=safe_host, port=int(port))
    finally:
        app.state.scheduler.stop()
    return 0
