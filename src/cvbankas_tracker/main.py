from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import textwrap
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from .analysis import (
    AIBasedAnalysisStrategy,
    ClaudeCLIAnalysisClient,
    CodexCLIAnalysisClient,
    DemoAIAnalysisClient,
    OpenAIAnalysisClient,
    RuleBasedAnalysisStrategy,
    VacancyAnalysisService,
)
from .extraction import (
    ClaudeCLIVacancyExtractionClient,
    CodexCLIVacancyExtractionClient,
    DemoAIVacancyExtractionClient,
    OpenAIVacancyExtractionClient,
    VacancyAIEnrichmentService,
)
from .io_utils import ProfileFileReader, ReportFileWriter
from .models import (
    ApplicationRecord,
    ApplicationStatus,
    ApplicationStatusOrigin,
    InboxPreferences,
    UserProfile,
    Vacancy,
    VacancyAnalysis,
)
from .sources import VacancySource, resolve_sources
from .storage import (
    CollectionRunAlreadyActive,
    DatabaseManager,
    canonicalize_source_url,
    resolve_database_path,
)
from .telegram import (
    TelegramNotificationError,
    TelegramNotifier,
    discover_telegram_chats,
)
from .tracking import ActionService, ApplicationTracker, utc_iso_to_local_datetime

DEFAULT_CONFIG_CANDIDATES = (
    "config/cvbankas.local.yaml",
    "config/cvbankas.yaml",
    "config/cvbankas.example.yaml",
)
STATUS_CHOICES = tuple(status.value.lower() for status in ApplicationStatus)
INBOX_SORT_CHOICES = ("score", "newest", "title", "company")
FIT_LABEL_CHOICES = ("High", "Medium", "Low")
DEFAULT_TRACKED_EXPORT_PATH = "exports/tracked_applications.md"
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"


@dataclass(slots=True)
class SourceBatchResult:
    source_name: str
    report_rows: list[tuple[Vacancy, VacancyAnalysis, ApplicationRecord | None]]
    attempted_count: int = 0
    observed_count: int = 0
    failed_count: int = 0
    total_pages: int = 0


def load_dotenv_if_present(dotenv_path: str | Path = ".env") -> None:
    path = Path(dotenv_path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_console_text(value: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(encoding, errors="replace").decode(encoding, errors="replace")


def supports_color() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.getenv("NO_COLOR")


def colorize(text: str, color_code: str, bold: bool = False) -> str:
    if not supports_color():
        return text
    prefix = color_code
    if bold:
        prefix = f"{ANSI_BOLD}{color_code}"
    return f"{prefix}{text}{ANSI_RESET}"


def colorize_fit_label(fit_label: str) -> str:
    normalized = fit_label.lower()
    if normalized == "high":
        return colorize(fit_label, ANSI_GREEN, bold=True)
    if normalized == "medium":
        return colorize(fit_label, ANSI_YELLOW, bold=True)
    return colorize(fit_label, ANSI_RED, bold=True)


def colorize_score(score: int | None) -> str:
    if score is None:
        return "-"
    if score >= 75:
        return colorize(str(score), ANSI_GREEN, bold=True)
    if score >= 45:
        return colorize(str(score), ANSI_YELLOW, bold=True)
    return colorize(str(score), ANSI_RED, bold=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-source vacancy discovery, analysis, and tracking CLI"
    )
    parser.add_argument(
        "--config",
        default="auto",
        help="Optional YAML config file for reusing CLI settings.",
    )
    parser.add_argument(
        "--profile",
        default="sample_data/active_profile.json",
        help="Path to the active user profile JSON file.",
    )
    parser.add_argument(
        "--db",
        default="job_seeker.db",
        help="Path to the SQLite database file.",
    )
    parser.add_argument(
        "--export",
        default="exports/job_seeker_report.md",
        help="Path to the generated Markdown report.",
    )
    parser.add_argument(
        "--keyword",
        default="python",
        help="Keyword used for vacancy search across enabled sources.",
    )
    parser.add_argument(
        "--keywords",
        default="",
        help="Comma, semicolon, or newline separated search keywords. Overrides --keyword.",
    )
    parser.add_argument(
        "--listing-url",
        default="",
        help="Optional listing page URL for single-source runs. Overrides --keyword.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of vacancy pages to process per enabled source.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="How many listing/search pages to crawl per enabled source.",
    )
    parser.add_argument(
        "--source",
        choices=("live", "sample", "cvbankas"),
        default="live",
        help="Legacy single-source selector: live/cvbankas or local sample fixtures.",
    )
    parser.add_argument(
        "--sources",
        default="",
        help="Comma-separated vacancy sources to enable, e.g. cvbankas,sample.",
    )
    parser.add_argument(
        "--cvbankas",
        action="store_true",
        help="Alias for live CVbankas batch mode.",
    )
    parser.add_argument(
        "--analysis-strategy",
        choices=("ai", "rule"),
        default="ai",
        help="Choose the primary vacancy analysis strategy.",
    )
    parser.add_argument(
        "--openai-model",
        default="gpt-4.1-mini",
        help="OpenAI model used when the AI strategy is active.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Reprocess vacancies even if they already exist in the database.",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Open the interactive terminal UI. This is also the default when no arguments are provided.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start the local loopback-only web dashboard.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Populate a throwaway demo database from bundled sample fixtures using "
            "deterministic rule-based scoring (no network, API keys, or logins). "
            "Add --web to also open the dashboard on the demo data."
        ),
    )
    parser.add_argument(
        "--web-host",
        default="127.0.0.1",
        help="Loopback host for --web; wildcard and non-loopback binds are rejected.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8000,
        help="Port for the local web dashboard.",
    )
    parser.add_argument(
        "--daily-run",
        action="store_true",
        help="Run the configured search once and send a Telegram summary of new vacancies.",
    )
    parser.add_argument(
        "--telegram-test",
        action="store_true",
        help="Send a Telegram test message without running a vacancy search.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        action="store_true",
        help="Show chat IDs from recent bot updates after you send the bot a message.",
    )
    parser.add_argument(
        "--import-urls",
        "--import-url",
        default="",
        help="Import direct vacancy URLs pasted as one value; separate by newlines, spaces, or commas.",
    )
    parser.add_argument(
        "--import-urls-file",
        default="",
        help="Path to a text file with vacancy URLs separated by newlines, spaces, or commas.",
    )
    parser.add_argument(
        "--list-vacancies",
        action="store_true",
        help="List saved vacancies together with their latest scores from the database.",
    )
    parser.add_argument(
        "--inbox",
        action="store_true",
        help="Show the explained recommendation inbox using saved/shared preferences.",
    )
    parser.add_argument(
        "--inbox-min-score",
        type=int,
        help="Minimum recommendation score for inbox preferences.",
    )
    parser.add_argument(
        "--inbox-hide-below-threshold",
        action="store_true",
        help="Save inbox preferences to hide vacancies below the minimum score.",
    )
    parser.add_argument(
        "--inbox-show-below-threshold",
        action="store_true",
        help="Save inbox preferences to show vacancies below the minimum score.",
    )
    parser.add_argument(
        "--inbox-sort",
        choices=INBOX_SORT_CHOICES,
        help="Inbox preference/order: score, newest, title, or company.",
    )
    parser.add_argument("--inbox-source", default="", help="Filter inbox by source name.")
    parser.add_argument("--inbox-fit", choices=FIT_LABEL_CHOICES, help="Filter inbox by fit label.")
    parser.add_argument(
        "--inbox-status",
        choices=STATUS_CHOICES,
        help="Filter inbox by current application status.",
    )
    parser.add_argument(
        "--inbox-new-only",
        action="store_const",
        const=True,
        default=None,
        help="Show only new items for the inbox run.",
    )
    parser.add_argument(
        "--inbox-current-run-only",
        action="store_const",
        const=True,
        default=None,
        help="Filter inbox to the latest completed/partial collection run.",
    )
    parser.add_argument(
        "--inbox-all-runs",
        action="store_const",
        const=True,
        default=None,
        help="Clear the current-run-only inbox filter and include older saved vacancies.",
    )
    parser.add_argument(
        "--clear-inbox-filters",
        action="store_const",
        const=True,
        default=None,
        help="Clear persisted source/fit/status/new/current-run inbox filters.",
    )
    parser.add_argument(
        "--save-inbox-preferences",
        action="store_true",
        help="Persist provided inbox threshold/hide/sort options for TUI/web/CLI.",
    )
    parser.add_argument(
        "--list-tracked",
        action="store_true",
        help="List tracked applications and their current statuses.",
    )
    parser.add_argument("--today", action="store_true", help="Show Today: new inbox items and due/overdue actions.")
    parser.add_argument("--list-actions", action="store_true", help="List action/reminder items.")
    parser.add_argument("--action-id", type=int, help="Action identifier for complete/reopen commands.")
    parser.add_argument("--action-title", default="", help="Create an action/reminder with this title.")
    parser.add_argument("--action-notes", default="", help="Notes for a created action/reminder.")
    parser.add_argument(
        "--action-due",
        default="",
        help="Local due date/time for a created action, e.g. 2026-08-08T17:30:00.",
    )
    parser.add_argument("--action-fold", type=int, choices=(0, 1), help="DST fold for ambiguous local due times.")
    parser.add_argument("--update-action", action="store_true", help="Edit --action-id title/notes/due fields.")
    parser.add_argument("--clear-action-due", action="store_true", help="Clear the due time when editing --action-id.")
    parser.add_argument("--complete-action", action="store_true", help="Mark --action-id completed.")
    parser.add_argument("--reopen-action", action="store_true", help="Reopen --action-id.")
    parser.add_argument(
        "--list-status-history",
        action="store_true",
        help="Show append-only application status history for a vacancy.",
    )
    parser.add_argument(
        "--vacancy-url",
        default="",
        help="Vacancy URL used by manual application tracking commands.",
    )
    parser.add_argument(
        "--vacancy-id",
        default="",
        help="Source ID used by inspect and manual application tracking commands.",
    )
    parser.add_argument(
        "--vacancy-source",
        default="",
        help="Optional source/provider name used with --vacancy-id when IDs overlap.",
    )
    parser.add_argument(
        "--show-vacancy",
        action="store_true",
        help="Show one stored vacancy together with its latest analysis and tracking state.",
    )
    parser.add_argument(
        "--status",
        choices=STATUS_CHOICES,
        help="Update the manual application status for one stored vacancy.",
    )
    parser.add_argument(
        "--status-correction-reason",
        default="",
        help="Explicit reason required to override the normal application status transition graph.",
    )
    parser.add_argument(
        "--note",
        help="Update the manual application note for one stored vacancy.",
    )
    parser.add_argument(
        "--export-tracked",
        nargs="?",
        const=DEFAULT_TRACKED_EXPORT_PATH,
        default="",
        help="Write a Markdown report for tracked applications.",
    )
    return parser.parse_args()


def load_yaml_config(config_path: str | Path) -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML config must contain a top-level mapping/object.")
    return data


def resolve_config_path(config_value: str) -> str:
    if config_value and config_value != "auto":
        return config_value

    for candidate in DEFAULT_CONFIG_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def _cfg_get(cfg: dict, *keys, default=None):
    current = cfg
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _cfg_bool(cfg: dict, *keys: str, default: bool = False) -> bool:
    value = _cfg_get(cfg, *keys, default=default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _cli_option_present(option_name: str) -> bool:
    return any(arg == option_name or arg.startswith(f"{option_name}=") for arg in sys.argv)


def _parse_source_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def parse_search_keywords(value: object) -> list[str]:
    if isinstance(value, list):
        raw_values = [str(part) for part in value]
    elif isinstance(value, str):
        raw_values = re.split(r"[\n;,]+", value)
    else:
        raw_values = []

    keywords: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        keyword = " ".join(str(raw_value).split())
        normalized = keyword.lower()
        if not keyword or normalized in seen:
            continue
        seen.add(normalized)
        keywords.append(keyword)
    return keywords


def parse_import_urls(value: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    for match in re.finditer(r"https?://[^\s,]+", value):
        url = match.group(0).strip().rstrip(".,;)]}>\"'")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    return urls


def load_import_urls(args: argparse.Namespace, workspace: Path) -> list[str]:
    raw_parts: list[str] = []
    if args.import_urls:
        raw_parts.append(args.import_urls)
    if args.import_urls_file:
        path = Path(args.import_urls_file)
        if not path.is_absolute():
            path = workspace / path
        raw_parts.append(path.read_text(encoding="utf-8"))

    return parse_import_urls("\n".join(raw_parts))


def _legacy_source_name(source: str, cvbankas_alias: bool) -> str:
    if cvbankas_alias or source in {"live", "cvbankas"}:
        return "cvbankas"
    return source


def resolve_enabled_source_names(
    args: argparse.Namespace,
    cfg: dict,
    *,
    cli_specified_source: bool,
    cli_specified_sources: bool,
) -> list[str]:
    if cli_specified_sources:
        return _parse_source_names(args.sources)
    if cli_specified_source:
        return [_legacy_source_name(args.source, args.cvbankas)]

    configured_sources = _parse_source_names(_cfg_get(cfg, "sources", "enabled", default=[]))
    if configured_sources:
        return configured_sources

    source_type = _cfg_get(cfg, "source", "type", default="")
    if source_type:
        return [_legacy_source_name(str(source_type), cvbankas_alias=False)]

    return [_legacy_source_name(args.source, args.cvbankas)]


def resolve_search_keywords(args: argparse.Namespace, cfg: dict) -> list[str]:
    if _cli_option_present("--keywords"):
        keywords = parse_search_keywords(args.keywords)
        if keywords:
            return keywords
    if _cli_option_present("--keyword"):
        return parse_search_keywords(args.keyword) or [args.keyword]

    configured_keywords = parse_search_keywords(_cfg_get(cfg, "search", "keywords", default=[]))
    if configured_keywords:
        return configured_keywords

    configured_keyword = _cfg_get(
        cfg,
        "search",
        "keyword",
        default=_cfg_get(cfg, "source", "keyword", default=args.keyword),
    )
    return parse_search_keywords(configured_keyword) or [args.keyword]


def resolve_source_search_keywords(
    source_name: str,
    args: argparse.Namespace,
    cfg: dict,
) -> list[str]:
    if _cli_option_present("--keywords") or _cli_option_present("--keyword"):
        return list(args.search_keywords)

    source_keywords = parse_search_keywords(
        _cfg_get(cfg, "sources", "keywords", source_name, default=[])
    )
    if source_keywords:
        return source_keywords

    return list(args.search_keywords)


def resolve_source_options(cfg: dict) -> dict:
    options = _cfg_get(cfg, "sources", "options", default={})
    return options if isinstance(options, dict) else {}


def close_source_resources(sources: Iterable[VacancySource]) -> None:
    for source in sources:
        close = getattr(source, "close", None)
        if callable(close):
            close()


def resolve_source_for_import_url(
    url: str,
    sources: Iterable[VacancySource],
) -> VacancySource:
    resolved_sources = list(sources)
    for source in resolved_sources:
        if source.can_handle_url(url):
            return source

    available = ", ".join(source.name for source in resolved_sources)
    raise ValueError(f"No enabled source can handle URL: {url}. Enabled sources: {available}.")


def resolve_ai_backend() -> str:
    """Return the configured AI backend.

    ``AI_BACKEND`` accepts ``claude_cli``, ``codex_cli``, ``openai``, ``demo``, or ``rule``.
    When unset: OpenAI if a key is present, otherwise honest rule-based keyword scoring
    (``demo`` remains available explicitly for the score-boosting offline showcase client).
    """
    backend = (os.getenv("AI_BACKEND") or "").strip().lower()
    if backend in {"claude_cli", "codex_cli", "openai", "demo", "rule"}:
        return backend
    if (os.getenv("OPENAI_API_KEY") or "").strip():
        return "openai"
    return "rule"


def _optional_env(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value or None


def build_analysis_service(strategy_name: str, openai_model: str) -> VacancyAnalysisService:
    fallback = RuleBasedAnalysisStrategy()
    if strategy_name == "rule":
        return VacancyAnalysisService(primary_strategy=fallback)

    backend = resolve_ai_backend()
    if backend == "rule":
        return VacancyAnalysisService(primary_strategy=fallback)

    if backend == "claude_cli":
        ai_client = ClaudeCLIAnalysisClient(model=_optional_env("CLAUDE_CLI_MODEL"))
    elif backend == "codex_cli":
        ai_client = CodexCLIAnalysisClient(model=_optional_env("CODEX_CLI_MODEL"))
    elif backend == "openai":
        ai_client = OpenAIAnalysisClient(model=openai_model)
    else:
        ai_client = DemoAIAnalysisClient()

    return VacancyAnalysisService(
        primary_strategy=AIBasedAnalysisStrategy(ai_client),
        fallback_strategy=fallback,
    )


def build_extraction_service(
    openai_model: str,
    *,
    use_openai: bool = True,
) -> VacancyAIEnrichmentService:
    backend = resolve_ai_backend() if use_openai else "demo"
    if backend == "claude_cli":
        client = ClaudeCLIVacancyExtractionClient(model=_optional_env("CLAUDE_CLI_MODEL"))
    elif backend == "codex_cli":
        client = CodexCLIVacancyExtractionClient(model=_optional_env("CODEX_CLI_MODEL"))
    elif backend == "openai":
        client = OpenAIVacancyExtractionClient(model=openai_model)
    else:
        client = DemoAIVacancyExtractionClient()
    return VacancyAIEnrichmentService(client)


def parse_application_status(value: str) -> ApplicationStatus:
    normalized = value.strip().lower()
    for status in ApplicationStatus:
        if status.value.lower() == normalized:
            return status
    raise ValueError(f"Unsupported application status: {value}")


def print_section(title: str, lines: Iterable[str]) -> None:
    header = f"=== {title} ==="
    rendered_header = colorize(header, "", bold=True) if supports_color() else header
    rendered_lines = [rendered_header, "", *(safe_console_text(line) for line in lines)]
    print("\n".join(rendered_lines))


def terminal_width(default: int = 100) -> int:
    try:
        return max(72, shutil.get_terminal_size((default, 24)).columns)
    except OSError:
        return default


def format_field(label: str, value: str) -> list[str]:
    prefix = f"    {label:<11}: "
    width = terminal_width()
    wrapped = textwrap.wrap(
        value or "-",
        width=max(20, width - len(prefix)),
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not wrapped:
        return [prefix + "-"]

    lines = [prefix + wrapped[0]]
    continuation = " " * len(prefix)
    for part in wrapped[1:]:
        lines.append(continuation + part)
    return lines


def format_list_block(title: str, values: list[str]) -> list[str]:
    width = terminal_width()
    lines = [f"    {title}:"]
    if not values:
        lines.append("      -")
        return lines

    for value in values:
        wrapped = textwrap.wrap(
            value,
            width=max(20, width - 8),
            break_long_words=False,
            break_on_hyphens=False,
        )
        if not wrapped:
            lines.append("      -")
            continue
        lines.append(f"      - {wrapped[0]}")
        for part in wrapped[1:]:
            lines.append(f"        {part}")
    return lines


def format_text_block(title: str, value: str) -> list[str]:
    width = terminal_width()
    lines = [f"    {title}:"]
    wrapped = textwrap.wrap(
        value or "-",
        width=max(20, width - 6),
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not wrapped:
        return lines + ["      -"]
    return lines + [f"      {part}" for part in wrapped]


def resolve_vacancy_url(
    database: DatabaseManager,
    vacancy_url: str,
    vacancy_id: str,
    vacancy_source: str = "",
) -> str:
    if vacancy_url:
        return vacancy_url
    if vacancy_id:
        vacancy = database.get_vacancy_by_source_id(vacancy_id, vacancy_source or None)
        if vacancy is None:
            raise ValueError(f"Vacancy not found in the database: {vacancy_id}")
        return vacancy.source_url
    raise ValueError("Provide --vacancy-url or --vacancy-id for this command.")


def resolve_cli_inbox_preferences(database: DatabaseManager, args: argparse.Namespace) -> InboxPreferences:
    current = database.get_inbox_preferences()
    minimum_score = current.minimum_score if args.inbox_min_score is None else args.inbox_min_score
    hide_below_threshold = current.hide_below_threshold
    if args.inbox_hide_below_threshold:
        hide_below_threshold = True
    if args.inbox_show_below_threshold:
        hide_below_threshold = False
    sort_by = args.inbox_sort or current.sort_by
    clear_filters = args.clear_inbox_filters is True
    source_name = "" if clear_filters else (args.inbox_source or current.source_name)
    fit_label = "" if clear_filters else (args.inbox_fit or current.fit_label)
    application_status = current.application_status
    if clear_filters:
        application_status = ""
    elif args.inbox_status:
        application_status = parse_application_status(args.inbox_status).value
    new_only = current.new_only
    if clear_filters:
        new_only = False
    elif args.inbox_new_only is True:
        new_only = True
    current_run_only = current.current_run_only
    if clear_filters:
        current_run_only = True
    if args.inbox_current_run_only is True:
        current_run_only = True
    if args.inbox_all_runs is True:
        current_run_only = False
    preferences = InboxPreferences(
        minimum_score=minimum_score,
        hide_below_threshold=hide_below_threshold,
        sort_by=sort_by,
        source_name=source_name,
        fit_label=fit_label,
        application_status=application_status,
        new_only=new_only,
        current_run_only=current_run_only,
    )
    if _inbox_preference_update_requested(args):
        database.save_inbox_preferences(preferences)
    return preferences


def _inbox_preference_update_requested(args: argparse.Namespace) -> bool:
    return (
        args.save_inbox_preferences
        or args.inbox_min_score is not None
        or args.inbox_hide_below_threshold
        or args.inbox_show_below_threshold
        or args.inbox_sort
        or args.inbox_source
        or args.inbox_fit
        or args.inbox_status
        or args.inbox_new_only is True
        or args.inbox_current_run_only is True
        or args.inbox_all_runs is True
        or args.clear_inbox_filters is True
    )


def print_inbox(database: DatabaseManager, args: argparse.Namespace) -> int:
    preferences = resolve_cli_inbox_preferences(database, args)
    latest_run = database.get_latest_inbox_run()
    items = database.query_inbox(
        preferences=preferences,
        source_name=args.inbox_source or None,
        fit_label=args.inbox_fit or None,
        application_status=parse_application_status(args.inbox_status) if args.inbox_status else None,
        new_only=True if args.inbox_new_only is True else None,
    )
    visibility = "hidden" if preferences.hide_below_threshold else "shown"
    summary = (
        f"Preferences: minimum_score={preferences.minimum_score}; "
        f"below_threshold={visibility}; sort={preferences.sort_by}; "
        f"source={preferences.source_name or '*'}; fit={preferences.fit_label or '*'}; "
        f"status={preferences.application_status or '*'}; new_only={preferences.new_only}; "
        f"current_run_only={preferences.current_run_only}; "
        f"latest_run={latest_run.id if latest_run else '-'}"
        f"{' (PARTIAL - incomplete collection)' if latest_run and latest_run.status == 'partial' else ''}"
    )
    if not items:
        print_section("Explained Inbox", [summary, "", "No inbox vacancies match the current preferences/filters."])
        return 2
    lines = [
        summary
    ]
    for index, item in enumerate(items, start=1):
        flags = []
        if item.is_new_in_run:
            flags.append("new")
        if item.is_current_run:
            flags.append("current-run")
        status = item.application_status.value if item.application_status else "-"
        lines.extend(
            [
                "",
                f"[{index}] {item.title}",
                f"    Score     : {colorize_score(item.latest_score)}",
                f"    Fit       : {colorize_fit_label(item.latest_fit_label) if item.latest_fit_label else '-'}",
                f"    Status    : {status}",
                f"    Source    : {item.source_name}",
                f"    Source ID : {item.source_id}",
                f"    Company   : {item.company or '-'}",
                f"    Location  : {item.location or '-'}",
                f"    Seen      : first_run={item.first_seen_run_id or '-'} | last_run={item.last_seen_run_id or '-'} | {'/'.join(flags) or '-'}",
                f"    Explain   : {item.explanation or '-'}",
                f"    Matched   : {', '.join(item.matched_points) if item.matched_points else '-'}",
                f"    Missing   : {', '.join(item.missing_points) if item.missing_points else '-'}",
                f"    URL       : {item.source_url}",
            ]
        )
    print_section("Explained Inbox", lines)
    return 0


def _format_local_due(database: DatabaseManager, due_at_utc: str | None) -> str:
    if not due_at_utc:
        return "-"
    timezone_name = ActionService(database).resolve_user_timezone()
    return f"{utc_iso_to_local_datetime(due_at_utc, timezone_name)} ({timezone_name})"


def print_actions(database: DatabaseManager, *, include_completed: bool = True) -> int:
    actions = database.list_action_items(include_completed=include_completed)
    if not actions:
        print("No actions found.")
        return 2
    lines = []
    for action in actions:
        lines.extend(
            [
                f"[{action.id}] {action.title}",
                f"    State     : {action.state.value}",
                f"    Due local : {_format_local_due(database, action.due_at_utc)}",
                f"    Due UTC   : {action.due_at_utc or '-'}",
                f"    Notes     : {action.notes or '-'}",
                f"    Vacancy   : {action.vacancy_source_url}",
            ]
        )
    print_section("Actions", lines)
    return 0


def print_today(database: DatabaseManager, args: argparse.Namespace) -> int:
    preferences = resolve_cli_inbox_preferences(database, args)
    latest_run = database.get_latest_inbox_run()
    inbox_items = database.query_inbox(preferences=preferences, new_only=True)
    reminders = database.query_action_reminders()
    lines = [
        "New recommended vacancies:",
        (
            f"Latest inbox run: {latest_run.id} ({latest_run.status.upper()} - incomplete collection)"
            if latest_run and latest_run.status == "partial"
            else f"Latest inbox run: {latest_run.id if latest_run else '-'}"
        ),
    ]
    if inbox_items:
        for item in inbox_items[:20]:
            score = item.latest_score if item.latest_score is not None else "-"
            lines.append(f"  - {item.title} | score={score} | fit={item.latest_fit_label or '-'} | {item.source_url}")
    else:
        lines.append("  - none")
    lines.append("")
    lines.append("Due/overdue actions:")
    if reminders:
        for reminder in reminders:
            action = reminder.action
            lines.append(
                f"  - [{action.id}] {reminder.reminder_state}: {action.title} | due_local={_format_local_due(database, action.due_at_utc)} | due_utc={action.due_at_utc} | {action.vacancy_source_url}"
            )
    else:
        lines.append("  - none")
    print_section("Today", lines)
    return 0 if inbox_items or reminders else 2


def print_status_history(database: DatabaseManager, args: argparse.Namespace) -> int:
    target_url = resolve_vacancy_url(database, args.vacancy_url, args.vacancy_id, args.vacancy_source)
    events = database.list_application_status_events(target_url)
    if not events:
        print("No status history found.")
        return 2
    lines = []
    for event in events:
        lines.append(
            f"[{event.id}] {event.changed_at} | {event.previous_status.value if event.previous_status else '-'} -> {event.new_status.value} | origin={event.origin.value} | kind={event.kind.value}"
        )
        if event.reason:
            lines.append(f"    reason={event.reason}")
        if event.note:
            lines.append(f"    note={event.note}")
    print_section("Status History", lines)
    return 0


def run_database_command(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    database = DatabaseManager(workspace / args.db)
    database.initialize()
    tracker = ApplicationTracker(database)
    action_service = ActionService(database)
    report_writer = ReportFileWriter()

    try:
        if args.inbox:
            return print_inbox(database, args)

        if _inbox_preference_update_requested(args) and not args.today:
            preferences = resolve_cli_inbox_preferences(database, args)
            print_section(
                "Inbox Preferences Updated",
                [
                    f"Minimum score        : {preferences.minimum_score}",
                    f"Hide below threshold : {preferences.hide_below_threshold}",
                    f"Sort                 : {preferences.sort_by}",
                    f"Source filter        : {preferences.source_name or '*'}",
                    f"Fit filter           : {preferences.fit_label or '*'}",
                    f"Status filter        : {preferences.application_status or '*'}",
                    f"New only             : {preferences.new_only}",
                    f"Current run only     : {preferences.current_run_only}",
                ],
            )
            return 0

        if args.today:
            return print_today(database, args)

        if args.update_action:
            if args.action_id is None:
                raise ValueError("Provide --action-id with --update-action.")
            action = action_service.update_action(
                args.action_id,
                title=args.action_title or None,
                notes=args.action_notes or None,
                local_due_at=args.action_due or None,
                clear_due=args.clear_action_due,
                fold=args.action_fold,
            )
            print_section(
                "Action Updated",
                [
                    f"[{action.id}] {action.title}",
                    f"    State   : {action.state.value}",
                    f"    Due local : {_format_local_due(database, action.due_at_utc)}",
                    f"    Due UTC : {action.due_at_utc or '-'}",
                    f"    Notes   : {action.notes or '-'}",
                    f"    Vacancy : {action.vacancy_source_url}",
                ],
            )
            return 0

        if args.action_title:
            target_url = resolve_vacancy_url(
                database,
                args.vacancy_url,
                args.vacancy_id,
                args.vacancy_source,
            )
            action = action_service.create_action(
                vacancy_source_url=target_url,
                title=args.action_title,
                notes=args.action_notes,
                local_due_at=args.action_due or None,
                fold=args.action_fold,
            )
            print_section(
                "Action Created",
                [
                    f"[{action.id}] {action.title}",
                    f"    State   : {action.state.value}",
                    f"    Due local : {_format_local_due(database, action.due_at_utc)}",
                    f"    Due UTC : {action.due_at_utc or '-'}",
                    f"    Notes   : {action.notes or '-'}",
                    f"    Vacancy : {action.vacancy_source_url}",
                ],
            )
            return 0

        if args.complete_action or args.reopen_action:
            if args.action_id is None:
                raise ValueError("Provide --action-id for --complete-action or --reopen-action.")
            action = (
                action_service.complete_action(args.action_id)
                if args.complete_action
                else action_service.reopen_action(args.action_id)
            )
            print_section(
                "Action Updated",
                [
                    f"[{action.id}] {action.title}",
                    f"    State   : {action.state.value}",
                    f"    Due local : {_format_local_due(database, action.due_at_utc)}",
                    f"    Due UTC : {action.due_at_utc or '-'}",
                    f"    Vacancy : {action.vacancy_source_url}",
                ],
            )
            return 0

        if args.list_actions:
            return print_actions(database)

        if args.list_status_history:
            return print_status_history(database, args)

        if args.list_vacancies:
            items = database.list_vacancies_with_latest_scores()
            if not items:
                print("No saved vacancies found.")
                return 2

            lines = []
            for index, item in enumerate(items, start=1):
                score = colorize_score(item.latest_score)
                fit = colorize_fit_label(item.latest_fit_label) if item.latest_fit_label else "-"
                status = item.application_status.value if item.application_status else "-"
                lines.extend(
                    [
                        f"[{index}] {item.title}",
                        f"    Score     : {score}",
                        f"    Fit       : {fit}",
                        f"    Status    : {status}",
                        f"    Source    : {item.source_name}",
                        f"    Source ID : {item.source_id}",
                        f"    Company   : {item.company}",
                        f"    Location  : {item.location or '-'}",
                        f"    URL       : {item.source_url}",
                        "",
                    ]
                )
            print_section("Saved Vacancies", lines)
            return 0

        if args.list_tracked:
            items = database.list_tracked_applications()
            if not items:
                print("No tracked applications found.")
                return 2

            lines = []
            for index, item in enumerate(items, start=1):
                score = str(item.latest_score) if item.latest_score is not None else "-"
                fit = item.latest_fit_label or "-"
                note = item.notes or "-"
                lines.extend(
                    [
                        f"[{index}] {item.title}",
                        (
                            f"    source={item.source_name} | source_id={item.source_id} "
                            f"| company={item.company} | status={item.status.value} "
                            f"| score={score} | fit={fit}"
                        ),
                        f"    note={note}",
                        f"    url={item.source_url}",
                    ]
                )
            print_section("Tracked Applications", lines)
            return 0

        if args.export_tracked:
            items = database.list_tracked_applications()
            if not items:
                print("No tracked applications found.")
                return 2

            rows = []
            for item in items:
                vacancy = database.get_vacancy(item.source_url)
                application = database.get_application_record(item.source_url)
                if vacancy is None or application is None:
                    continue
                rows.append((vacancy, database.get_latest_analysis(item.source_url), application))

            report_path = report_writer.write_tracked_applications_report(
                workspace / args.export_tracked,
                rows,
            )
            print(f"Tracked applications report written to: {report_path}")
            return 0

        if args.show_vacancy:
            target_url = resolve_vacancy_url(
                database,
                args.vacancy_url,
                args.vacancy_id,
                args.vacancy_source,
            )
            vacancy = database.get_vacancy(target_url)
            if vacancy is None:
                raise ValueError(f"Vacancy not found in the database: {target_url}")

            analysis = database.get_latest_analysis(target_url)
            application = database.get_application_record(target_url)
            preview = vacancy.raw_text.strip().replace("\n", " ").replace("\r", " ")
            preview = " ".join(preview.split())
            raw_preview = preview[:300] + ("..." if len(preview) > 300 else "")
            print_section(
                "Vacancy Details",
                [vacancy.title]
                + format_field("Score", colorize_score(analysis.score if analysis else None))
                + format_field(
                    "Fit",
                    colorize_fit_label(analysis.fit_label.value) if analysis else "-",
                )
                + format_field(
                    "Status",
                    application.status.value if application else "Not tracked",
                )
                + format_field("Source", vacancy.source_name)
                + format_field("Source ID", vacancy.source_id)
                + format_field("Company", vacancy.company or "-")
                + format_field("Location", vacancy.location or "-")
                + format_field("Salary", vacancy.salary_text or "-")
                + format_list_block("Requirements", vacancy.requirements)
                + format_list_block("Responsibilities", vacancy.responsibilities)
                + format_text_block("Explanation", analysis.explanation if analysis else "-")
                + format_text_block("Raw Preview", raw_preview or "-")
                + format_field("URL", vacancy.source_url),
            )
            return 0

        if args.status or args.note is not None:
            target_url = resolve_vacancy_url(
                database,
                args.vacancy_url,
                args.vacancy_id,
                args.vacancy_source,
            )
            if not database.has_vacancy(target_url):
                raise ValueError(f"Vacancy not found in the database: {target_url}")

            latest_analysis_id = database.get_latest_analysis_id(target_url)
            tracker.ensure_record(
                vacancy_source_url=target_url,
                analysis_id=latest_analysis_id,
                notes="Created from manual CLI tracking.",
            )

            if args.note is not None:
                tracker.update_notes(target_url, args.note)

            if args.status:
                desired_status = parse_application_status(args.status)
                existing = database.get_application_record(target_url)
                if existing is not None and existing.status != desired_status:
                    if args.status_correction_reason:
                        tracker.set_status(
                            target_url,
                            desired_status,
                            analysis_id=latest_analysis_id,
                            notes=existing.notes,
                            origin=ApplicationStatusOrigin.CLI,
                            reason=args.status_correction_reason,
                        )
                    else:
                        tracker.update_status(
                            target_url,
                            desired_status,
                            origin=ApplicationStatusOrigin.CLI,
                        )

            vacancy = database.get_vacancy(target_url)
            application = database.get_application_record(target_url)
            latest_score = database.list_vacancies_with_latest_scores()
            latest_item = next(
                (item for item in latest_score if item.source_url == target_url),
                None,
            )
            score = str(latest_item.latest_score) if latest_item and latest_item.latest_score is not None else "-"
            fit = latest_item.latest_fit_label if latest_item and latest_item.latest_fit_label else "-"
            note = application.notes if application and application.notes else "-"
            status = application.status.value if application else "-"
            print_section(
                "Application Updated",
                [vacancy.title if vacancy else args.vacancy_url]
                + format_field("Score", score)
                + format_field("Fit", fit)
                + format_field("Status", status)
                + format_field("Source", vacancy.source_name if vacancy else "-")
                + format_field("Source ID", vacancy.source_id if vacancy else "-")
                + format_field("Note", note)
                + format_field("URL", target_url),
            )
            return 0

        return 1
    except ValueError as error:
        print(f"Error: {error}")
        return 1
    finally:
        database.close()


def _process_vacancy_url(
    *,
    source: VacancySource,
    url: str,
    args: argparse.Namespace,
    profile: UserProfile,
    database: DatabaseManager,
    extraction_service: VacancyAIEnrichmentService,
    analysis_service: VacancyAnalysisService,
    report_rows: list[tuple[Vacancy, VacancyAnalysis, ApplicationRecord | None]],
    title: str,
    collection_run_id: int | None = None,
) -> bool:
    canonical_url = canonicalize_source_url(url)
    if not args.refresh and database.has_vacancy(canonical_url):
        database.record_vacancy_observation(
            canonical_url,
            collection_run_id=collection_run_id,
            source_name=source.name,
            original_source_url=url,
        )
        print(safe_console_text(f"[{ts()}] {title} SKIP | already processed | {canonical_url}"))
        return False

    delay_seconds = max(0, int(getattr(source, "vacancy_request_delay_seconds", 0) or 0))
    if delay_seconds:
        print(
            safe_console_text(
                f"[{ts()}] {source.name} waiting {delay_seconds}s before vacancy fetch | {url}"
            )
        )
        time.sleep(delay_seconds)

    html = source.fetch_vacancy_page(url)
    vacancy = source.parse_vacancy(html, url)
    vacancy = extraction_service.enrich(vacancy)
    if not vacancy.title:
        raise ValueError("could not parse vacancy title")

    vacancy.source_url = canonicalize_source_url(vacancy.source_url)
    analysis = analysis_service.analyze(vacancy, profile)
    _analysis_id, stored_application = database.save_processed_vacancy(
        vacancy=vacancy,
        analysis=analysis,
        collection_run_id=collection_run_id,
        original_source_url=url,
        application_origin=ApplicationStatusOrigin.SYSTEM,
        application_note=f"Created during the Job Seeker CLI run from {source.name}.",
    )
    report_rows.append((vacancy, analysis, stored_application))

    batch_status = stored_application.status.value if stored_application else "Not tracked"
    print_section(
        title,
        [vacancy.title]
        + format_field("Source", vacancy.source_name)
        + format_field("Score", colorize_score(analysis.score))
        + format_field("Fit", colorize_fit_label(analysis.fit_label.value))
        + format_field("Company", vacancy.company or "-")
        + format_field("Location", vacancy.location or "-")
        + format_field("Status", batch_status)
        + format_field("URL", vacancy.source_url),
    )
    return True


def _is_sqlite_lock_error(error: Exception) -> bool:
    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message


def _sleep_before_source_request(
    source: VacancySource,
    *,
    delay_attribute: str,
    label: str,
    url: str,
) -> None:
    delay_seconds = max(0, int(getattr(source, delay_attribute, 0) or 0))
    if not delay_seconds:
        return
    print(
        safe_console_text(
            f"[{ts()}] {source.name} waiting {delay_seconds}s before {label} fetch | {url}"
        )
    )
    time.sleep(delay_seconds)


def _run_source_batch(
    source: VacancySource,
    *,
    args: argparse.Namespace,
    cfg: dict,
    workspace: Path,
    profile: UserProfile,
    collection_run_id: int | None = None,
) -> SourceBatchResult:
    result = SourceBatchResult(source_name=source.name, report_rows=[])
    database = DatabaseManager(workspace / args.db)

    try:
        extraction_service = build_extraction_service(
            args.openai_model,
            use_openai=args.analysis_strategy == "ai",
        )
        analysis_service = build_analysis_service(args.analysis_strategy, args.openai_model)
        keywords = resolve_source_search_keywords(source.name, args, cfg)
        if args.listing_url:
            keywords = [args.keyword]
        listing_urls: list[str] = []
        page_urls: list[str] = []
        seen_urls: set[str] = set()

        try:
            for keyword in keywords:
                print(
                    safe_console_text(
                        f"[{ts()}] {source.name} collecting listings "
                        f"| keyword={keyword!r}"
                    )
                )
                keyword_listing_urls, keyword_page_urls = source.collect_vacancy_urls(
                    keyword=keyword,
                    listing_url=args.listing_url,
                    max_pages=args.max_pages,
                    before_listing_fetch=lambda page_url: _sleep_before_source_request(
                        source,
                        delay_attribute="listing_request_delay_seconds",
                        label="listing",
                        url=page_url,
                    ),
                )
                page_urls.extend(keyword_page_urls)
                for vacancy_url in keyword_listing_urls:
                    if vacancy_url in seen_urls:
                        continue
                    seen_urls.add(vacancy_url)
                    listing_urls.append(vacancy_url)
                    if len(listing_urls) >= args.limit:
                        break
                if len(listing_urls) >= args.limit:
                    break
        except Exception as error:  # noqa: BLE001 - one source should not stop all sources
            result.failed_count += 1
            print(safe_console_text(f"[{ts()}] {source.name} LISTING ERROR | {error}"))
            return result

        result.total_pages = len(page_urls)
        source_limit = min(len(listing_urls), args.limit)
        print(
            f"[{ts()}] {source.name} batch started | pages={len(page_urls)} "
            f"| collected={len(listing_urls)} | keywords={len(keywords)} "
            f"| configured_max_pages={args.max_pages}"
        )

        for index, url in enumerate(listing_urls[: args.limit], start=1):
            result.attempted_count += 1
            try:
                _process_vacancy_url(
                    source=source,
                    url=url,
                    args=args,
                    profile=profile,
                    database=database,
                    extraction_service=extraction_service,
                    analysis_service=analysis_service,
                    report_rows=result.report_rows,
                    title=f"Processed Vacancy {source.name} {index}/{source_limit}",
                    collection_run_id=collection_run_id,
                )
                result.observed_count += 1
            except Exception as error:  # noqa: BLE001 - batch mode should continue
                result.failed_count += 1
                error_label = "STORAGE LOCK ERROR" if _is_sqlite_lock_error(error) else "ERROR"
                print(
                    safe_console_text(
                        f"[{ts()}] [{source.name} {index}/{source_limit}] {error_label} | {url} | {error}"
                    )
                )
        return result
    finally:
        close_source_resources([source])
        database.close()


def _execute_source_batches(
    sources: list[VacancySource],
    worker: Callable[[VacancySource], SourceBatchResult],
) -> list[SourceBatchResult]:
    if len(sources) == 1:
        return [worker(sources[0])]

    print(f"[{ts()}] Starting {len(sources)} source workers in parallel.")
    results: list[SourceBatchResult] = []
    with ThreadPoolExecutor(
        max_workers=len(sources),
        thread_name_prefix="job-source",
    ) as executor:
        futures = [executor.submit(worker, source) for source in sources]
        for source, future in zip(sources, futures):
            try:
                results.append(future.result())
            except Exception as error:  # noqa: BLE001 - one worker should not stop the run
                print(
                    safe_console_text(
                        f"[{ts()}] {source.name} SOURCE WORKER ERROR | {error}"
                    )
                )
                results.append(
                    SourceBatchResult(
                        source_name=source.name,
                        report_rows=[],
                        failed_count=1,
                    )
                )
    return results



def _collection_terminal_status(
    source_results: list[SourceBatchResult],
    report_rows: list[tuple[Vacancy, VacancyAnalysis, ApplicationRecord | None]],
) -> str:
    failed_count = sum(result.failed_count for result in source_results)
    if failed_count == 0:
        return "completed"
    if report_rows or any(result.observed_count for result in source_results):
        return "partial"
    return "failed"

def _send_telegram_batch_summary(
    *,
    cfg: dict,
    source_results: list[SourceBatchResult],
    report_rows: list[tuple[Vacancy, VacancyAnalysis, ApplicationRecord | None]],
) -> bool:
    source_names = [result.source_name for result in source_results]
    attempted_count = sum(result.attempted_count for result in source_results)
    failed_count = sum(result.failed_count for result in source_results)
    max_vacancies = int(_cfg_get(cfg, "telegram", "max_vacancies", default=10))
    notify_when_empty = _cfg_bool(
        cfg,
        "telegram",
        "notify_when_empty",
        default=False,
    )
    try:
        sent_count = TelegramNotifier.from_env().send_daily_summary(
            report_rows,
            source_names=source_names,
            attempted_count=attempted_count,
            failed_count=failed_count,
            max_vacancies=max_vacancies,
            notify_when_empty=notify_when_empty,
        )
    except TelegramNotificationError as error:
        print(safe_console_text(f"[{ts()}] TELEGRAM ERROR | {error}"))
        return False

    if sent_count:
        print(f"[{ts()}] Telegram summary sent | messages={sent_count}")
    else:
        print(f"[{ts()}] Telegram summary skipped | no new vacancies")
    return True


def run_batch(args: argparse.Namespace, cfg: dict | None = None) -> int:
    load_dotenv_if_present()
    cfg = cfg or {}

    workspace = Path.cwd()
    data_dir = workspace / "sample_data"
    profile = ProfileFileReader().read(workspace / args.profile)
    sources = resolve_sources(
        args.enabled_sources,
        data_dir=data_dir,
        source_options=resolve_source_options(cfg),
    )
    if args.listing_url and len(sources) > 1:
        raise ValueError("--listing-url can only be used when exactly one source is enabled.")

    database = DatabaseManager(workspace / args.db)
    database.initialize()
    try:
        collection_run = database.begin_collection_run()
    except CollectionRunAlreadyActive as error:
        print(safe_console_text(f"[{ts()}] COLLECTION RUN ACTIVE | {error}"))
        database.close()
        return 3
    finally:
        database.close()
    report_writer = ReportFileWriter()

    source_results = _execute_source_batches(
        sources,
        lambda source: _run_source_batch(
            source,
            args=args,
            cfg=cfg,
            workspace=workspace,
            profile=profile,
            collection_run_id=collection_run.id,
        ),
    )
    report_rows = [
        row
        for source_result in source_results
        for row in source_result.report_rows
    ]
    failed_count = sum(result.failed_count for result in source_results)
    attempted_count = sum(result.attempted_count for result in source_results)
    total_pages = sum(result.total_pages for result in source_results)

    terminal_database = DatabaseManager(workspace / args.db)
    terminal_status = _collection_terminal_status(source_results, report_rows)
    terminal_database.finish_collection_run(
        collection_run.id,
        status=terminal_status,
        source_summary={
            result.source_name: {
                "attempted": result.attempted_count,
                "failed": result.failed_count,
                "observed": result.observed_count,
                "saved": len(result.report_rows),
                "pages": result.total_pages,
            }
            for result in source_results
        },
        error_summary={
            result.source_name: result.failed_count
            for result in source_results
            if result.failed_count
        },
    )
    terminal_database.close()

    report_path = report_writer.write_report(workspace / args.export, report_rows)
    notification_ok = True
    if getattr(args, "daily_run", False):
        notification_ok = _send_telegram_batch_summary(
            cfg=cfg,
            source_results=source_results,
            report_rows=report_rows,
        )

    print(
        f"\n[{ts()}] Vacancy batch finished. "
        f"sources={','.join(source.name for source in sources)} "
        f"processed={attempted_count} saved={len(report_rows)} "
        f"failed={failed_count} pages={total_pages} db={workspace / args.db}"
    )
    print(f"Report written to: {report_path}")
    if not notification_ok:
        return 1
    return 0 if report_rows else 2


def run_import(args: argparse.Namespace, cfg: dict | None = None) -> int:
    load_dotenv_if_present()
    cfg = cfg or {}

    workspace = Path.cwd()
    data_dir = workspace / "sample_data"
    urls = load_import_urls(args, workspace)
    if not urls:
        raise ValueError("Provide at least one URL via --import-urls or --import-urls-file.")

    sources = resolve_sources(
        args.enabled_sources,
        data_dir=data_dir,
        source_options=resolve_source_options(cfg),
    )
    profile = ProfileFileReader().read(workspace / args.profile)
    extraction_service = build_extraction_service(
        args.openai_model,
        use_openai=args.analysis_strategy == "ai",
    )
    analysis_service = build_analysis_service(args.analysis_strategy, args.openai_model)
    database = DatabaseManager(workspace / args.db)
    database.initialize()
    report_writer = ReportFileWriter()

    report_rows: list[tuple[Vacancy, VacancyAnalysis, ApplicationRecord | None]] = []
    failed_count = 0
    processed_urls = urls[: args.limit]

    try:
        print(
            f"[{ts()}] URL import started | urls={len(urls)} "
            f"| processing={len(processed_urls)}"
        )

        for index, url in enumerate(processed_urls, start=1):
            try:
                source = resolve_source_for_import_url(
                    url,
                    sources,
                )
                _process_vacancy_url(
                    source=source,
                    url=url,
                    args=args,
                    profile=profile,
                    database=database,
                    extraction_service=extraction_service,
                    analysis_service=analysis_service,
                    report_rows=report_rows,
                    title=f"Imported Vacancy {index}/{len(processed_urls)}",
                )
            except Exception as error:  # noqa: BLE001 - import should continue
                failed_count += 1
                print(safe_console_text(f"[{ts()}] [{index}/{len(processed_urls)}] ERROR | {url} | {error}"))

        report_path = report_writer.write_report(workspace / args.export, report_rows)
    finally:
        close_source_resources(sources)
        database.close()

    print(
        f"\n[{ts()}] URL import finished. "
        f"processed={len(processed_urls)} saved={len(report_rows)} "
        f"failed={failed_count} db={workspace / args.db}"
    )
    print(f"Report written to: {report_path}")
    return 0 if report_rows else 2


DEMO_DB_RELATIVE = "demo_data/demo.db"
DEMO_PROFILE = "sample_data/active_profile.json"


def _demo_namespace(db_path: str) -> argparse.Namespace:
    """Argument set that drives run_batch against the bundled sample fixtures.

    Everything is pinned to the offline path: the ``sample`` source (local HTML
    fixtures) and ``rule`` scoring (deterministic, no AI backend), so the result
    is byte-reproducible and needs no network, API key, or CLI login.
    """
    return argparse.Namespace(
        config="",
        profile=DEMO_PROFILE,
        db=db_path,
        export="demo_data/demo_report.md",
        daily_run=False,
        keyword="python",
        keywords="python",
        listing_url="",
        limit=10,
        max_pages=1,
        source="live",
        sources="sample",
        cvbankas=False,
        analysis_strategy="rule",
        openai_model="gpt-4.1-mini",
        refresh=True,
        import_urls="",
        import_urls_file="",
        enabled_sources=["sample"],
        search_keywords=["python"],
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


def run_demo(args: argparse.Namespace) -> int:
    """Offline, reproducible demo: seed a throwaway DB from sample fixtures.

    Deliberately isolated from the user's real config and database — it writes
    only under ``demo_data/`` and re-seeds from scratch on every invocation.
    """
    workspace = Path.cwd()
    if not (workspace / "sample_data" / "listings.html").exists():
        print(
            safe_console_text(
                "Demo mode must be run from the project root: the bundled "
                "sample_data/ fixtures were not found in the current directory."
            )
        )
        return 1

    demo_dir = workspace / "demo_data"
    demo_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(demo_dir / "demo.db")
    # Re-seed from scratch so the demo is deterministic run-to-run and a stale
    # collection-run lease from an interrupted run can never block it.
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(db_path + suffix).unlink(missing_ok=True)

    print(safe_console_text("Job Seeker Intelligence — offline demo"))
    print(safe_console_text("  source=sample (local fixtures)  scoring=rule (deterministic)"))
    print(safe_console_text(f"  demo database: {db_path}\n"))

    exit_code = run_batch(_demo_namespace(db_path), cfg={})
    # run_batch returns 2 when no *new* rows were written; for the demo an empty
    # delta is still a successful seed, so only genuine failures propagate.
    if exit_code not in (0, 2):
        return exit_code

    if getattr(args, "web", False):
        from .web import run_web

        print(safe_console_text("\nOpening the dashboard on the demo data…"))
        return run_web(
            db_path,
            host=args.web_host,
            port=args.web_port,
            cfg={},
            profile_path=DEMO_PROFILE,
        )

    print(
        safe_console_text(
            "\nDemo data ready. Explore it with:\n"
            f"  python main.py --db {db_path} --inbox\n"
            f"  python main.py --db {db_path} --web    (browse in a browser)\n"
            "  python main.py --demo --web             (re-seed and open the dashboard)"
        )
    )
    return 0


def main() -> int:
    configure_console_encoding()
    load_dotenv_if_present()
    args = parse_args()
    if args.demo:
        return run_demo(args)
    open_tui_by_default = len(sys.argv) == 1
    cli_specified_source = _cli_option_present("--source") or _cli_option_present("--cvbankas")
    cli_specified_sources = _cli_option_present("--sources")
    args.config = resolve_config_path(args.config)
    cfg = load_yaml_config(args.config) if args.config else {}

    if not _cli_option_present("--profile"):
        args.profile = _cfg_get(cfg, "profile", default=args.profile)
    cli_specified_db = _cli_option_present("--db")
    if not cli_specified_db:
        args.db = _cfg_get(cfg, "db", default=args.db)
    args.db = str(
        resolve_database_path(
            args.db,
            config_path=args.config if args.config and not cli_specified_db else None,
        )
    )
    print(safe_console_text(f"Using database: {args.db}"))
    if not _cli_option_present("--export"):
        args.export = _cfg_get(cfg, "export", default=args.export)
    args.search_keywords = resolve_search_keywords(args, cfg)
    args.keyword = args.search_keywords[0]
    if not _cli_option_present("--listing-url"):
        args.listing_url = _cfg_get(
            cfg,
            "search",
            "listing_url",
            default=_cfg_get(cfg, "source", "listing_url", default=args.listing_url),
        )
    if not _cli_option_present("--limit"):
        args.limit = _cfg_get(
            cfg,
            "search",
            "limit",
            default=_cfg_get(cfg, "source", "limit", default=args.limit),
        )
    if not _cli_option_present("--max-pages"):
        args.max_pages = _cfg_get(
            cfg,
            "search",
            "max_pages",
            default=_cfg_get(cfg, "source", "max_pages", default=args.max_pages),
        )
    if not _cli_option_present("--analysis-strategy"):
        args.analysis_strategy = _cfg_get(
            cfg, "analysis_strategy", default=args.analysis_strategy
        )
    if not _cli_option_present("--openai-model"):
        args.openai_model = _cfg_get(cfg, "openai_model", default=args.openai_model)

    args.enabled_sources = resolve_enabled_source_names(
        args,
        cfg,
        cli_specified_source=cli_specified_source,
        cli_specified_sources=cli_specified_sources,
    )

    if args.telegram_chat_id:
        try:
            chats = discover_telegram_chats(os.getenv("TELEGRAM_BOT_TOKEN", ""))
        except TelegramNotificationError as error:
            print(safe_console_text(f"Telegram error: {error}"))
            return 1
        if not chats:
            print("No Telegram chats found. Send /start to the bot and run this command again.")
            return 2
        print("Recent Telegram chats:")
        for chat in chats:
            print(f"  {chat.chat_id}  {chat.label}")
        return 0

    if args.telegram_test:
        try:
            TelegramNotifier.from_env().send_text(
                f"<b>Job Seeker test</b>\nTelegram notifications are working.\n{ts()}"
            )
        except TelegramNotificationError as error:
            print(safe_console_text(f"Telegram error: {error}"))
            return 1
        print("Telegram test message sent.")
        return 0

    if args.web:
        from .web import run_web

        return run_web(
            args.db,
            host=args.web_host,
            port=args.web_port,
            cfg=cfg,
            profile_path=args.profile,
        )

    if args.tui or open_tui_by_default:
        from .tui import run_tui

        return run_tui(args, cfg)

    if (args.vacancy_url or args.vacancy_id) and args.list_vacancies:
        raise SystemExit("Vacancy selectors cannot be combined with --list-vacancies.")
    if (args.vacancy_url or args.vacancy_id) and args.list_tracked:
        raise SystemExit("Vacancy selectors cannot be combined with --list-tracked.")
    if args.vacancy_url and args.vacancy_id:
        raise SystemExit("Use either --vacancy-url or --vacancy-id, not both.")
    if args.vacancy_source and not args.vacancy_id:
        raise SystemExit("--vacancy-source can only be used together with --vacancy-id.")
    if args.status_correction_reason and not args.status:
        raise SystemExit("--status-correction-reason can only be used together with --status.")
    if args.inbox_hide_below_threshold and args.inbox_show_below_threshold:
        raise SystemExit("Use only one of --inbox-hide-below-threshold or --inbox-show-below-threshold.")
    if args.inbox_current_run_only is True and args.inbox_all_runs is True:
        raise SystemExit("Use only one of --inbox-current-run-only or --inbox-all-runs.")
    if args.complete_action and args.reopen_action:
        raise SystemExit("Use only one of --complete-action or --reopen-action.")
    if args.clear_action_due and not args.update_action:
        raise SystemExit("--clear-action-due can only be used with --update-action.")
    if (args.action_due or args.action_notes) and not (args.action_title or args.update_action):
        raise SystemExit("--action-due and --action-notes require --action-title or --update-action.")
    if args.action_title and not args.update_action and not (args.vacancy_url or args.vacancy_id):
        raise SystemExit("Provide --vacancy-url or --vacancy-id when creating an action.")
    import_requested = bool(args.import_urls or args.import_urls_file)
    database_command_requested = (
        args.list_vacancies
        or args.inbox
        or _inbox_preference_update_requested(args)
        or args.today
        or args.list_tracked
        or args.list_actions
        or args.action_title
        or args.update_action
        or args.complete_action
        or args.reopen_action
        or args.list_status_history
        or args.show_vacancy
        or args.export_tracked
        or args.status
        or args.status_correction_reason
        or args.note is not None
    )
    if import_requested and database_command_requested:
        raise SystemExit("URL import cannot be combined with database inspection commands.")

    if database_command_requested:
        return run_database_command(args)
    if import_requested:
        return run_import(args, cfg)

    exit_code = run_batch(args, cfg)
    if args.daily_run and exit_code == 2:
        return 0
    return exit_code
