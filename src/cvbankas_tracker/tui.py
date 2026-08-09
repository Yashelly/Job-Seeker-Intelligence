from __future__ import annotations

import argparse
import copy
import io
import re
import sys
import textwrap
import threading
import webbrowser
from collections.abc import Iterable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from .main import parse_search_keywords, run_batch, run_import
from .models import (
    ActionItem,
    ActionReminderItem,
    ActionState,
    ApplicationStatus,
    ApplicationStatusOrigin,
    InboxItem,
    InboxPreferences,
    VacancyListItem,
)
from .storage import DatabaseManager
from .tracking import ActionService, ApplicationTracker, utc_iso_to_local_datetime

CANONICAL_SOURCES = (
    "cvbankas",
    "hh",
    "startup_jobs",
    "justjoin",
    "euremotejobs",
    "sample",
)

TUI_STATUS_OPTIONS = (
    ApplicationStatus.SAVED,
    ApplicationStatus.APPLIED,
    ApplicationStatus.INTERVIEW,
    ApplicationStatus.OFFER,
    ApplicationStatus.REJECTED,
    ApplicationStatus.WITHDRAWN,
)

QUICK_START_KEYWORDS = (
    "AI automation",
    "n8n",
    "automation specialist",
    "no-code",
    "RPA",
)

QUICK_START_SOURCE_KEYWORDS = {
    "cvbankas": (
        "AI automation",
        "dirbtinio intelekto",
        "Power Automate",
        "programuotojas",
        "scraping",
        "analitikas",
        "automatikos",
    ),
    "hh": (
        "специалист по автоматизации",
        "инженер автоматизации",
        "специалист ИИ",
        "n8n",
        "Power Automate",
    ),
    "justjoin": (
        "AI automation",
        "n8n",
        "automation specialist",
        "no-code",
        "RPA",
    ),
}


@dataclass(frozen=True, slots=True)
class SearchPreset:
    name: str
    description: str
    sources: tuple[str, ...]
    keywords: tuple[str, ...] = ()
    source_keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)
    limit: int | None = None
    max_pages: int | None = None
    use_config_keywords: bool = False


PRESETS = (
    SearchPreset(
        name="Quick start",
        description="Small remote scan with source-specific keywords.",
        sources=("cvbankas", "hh", "justjoin"),
        keywords=QUICK_START_KEYWORDS,
        source_keywords=QUICK_START_SOURCE_KEYWORDS,
        limit=10,
        max_pages=1,
    ),
    SearchPreset(
        name="Config defaults",
        description="Use source-specific keywords from config/cvbankas.local.yaml.",
        sources=(),
        use_config_keywords=True,
    ),
    SearchPreset(
        name="CVbankas AI automation",
        description="Lithuanian and English AI automation roles.",
        sources=("cvbankas",),
        keywords=(
            "dirbtinio intelekto",
            "dirbtinio intelekto sprendimu inzinierius",
            "automatizavimo specialistas",
            "Automation Developer",
            "AI engineer",
            "AI automation",
            "n8n",
            "Make.com",
            "Zapier",
        ),
        limit=25,
        max_pages=2,
    ),
    SearchPreset(
        name="HH RU automation",
        description="Russian AI, RPA, no-code and integration roles.",
        sources=("hh",),
        keywords=(
            "специалист по автоматизации",
            "инженер автоматизации",
            "разработчик автоматизации",
            "специалист ИИ",
            "инженер ИИ",
            "RPA разработчик",
            "n8n",
            "Power Automate",
            "no-code",
            "low-code",
        ),
        limit=25,
        max_pages=2,
    ),
    SearchPreset(
        name="Remote startup EN",
        description="English remote/startup sources for AI workflow automation.",
        sources=("startup_jobs", "justjoin", "euremotejobs"),
        keywords=(
            "AI automation",
            "AI Workflow Engineer",
            "Automation Specialist",
            "No-Code Automation Specialist",
            "Process Automation Developer",
            "n8n",
            "Make.com",
            "Zapier",
            "AI agents",
            "LLM automation",
        ),
        limit=20,
        max_pages=2,
    ),
    SearchPreset(
        name="Tools stack",
        description="Search by tools and platforms instead of position titles.",
        sources=("cvbankas", "hh", "startup_jobs", "justjoin", "euremotejobs"),
        keywords=(
            "n8n",
            "Make.com",
            "Zapier",
            "Power Automate",
            "UiPath",
            "Airtable",
            "Retool",
            "HubSpot",
            "Claude Code",
            "Codex",
            "LangChain",
            "LangGraph",
        ),
        limit=30,
        max_pages=2,
    ),
)


@dataclass(slots=True)
class TuiState:
    profile: str
    db: str
    export: str
    openai_model: str
    analysis_strategy: str
    enabled_sources: list[str]
    limit: int
    max_pages: int
    refresh: bool = False
    selected_preset: str = "Quick start"
    use_config_keywords: bool = False
    search_keywords: list[str] = field(default_factory=list)
    source_keywords: dict[str, list[str]] = field(default_factory=dict)


ANSI_ESCAPE_PATTERNS = (
    re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"),
    re.compile(r"\x1b\].*?(?:\x07|\x1b\\)"),
    re.compile(r"\x1b[@-Z\\-_]"),
)
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def configure_terminal_encoding() -> None:
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def clean_terminal_text(value: str) -> str:
    for pattern in ANSI_ESCAPE_PATTERNS:
        value = pattern.sub("", value)
    value = value.replace("\r", "\n")
    return CONTROL_CHAR_PATTERN.sub("", value)


class LiveLogWriter(io.StringIO):
    def __init__(self, stream=None) -> None:
        super().__init__()
        self._stream = stream or sys.__stdout__
        self._pending = ""
        self._lock = threading.RLock()

    def write(self, value: str) -> int:
        with self._lock:
            clean_value = clean_terminal_text(value)
            super().write(clean_value)
            self._pending += clean_value
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", maxsplit=1)
                if line.strip():
                    self._write_live_line(line)
        return len(value)

    def flush(self) -> None:
        with self._lock:
            if self._pending.strip():
                self._write_live_line(self._pending)
            self._pending = ""
            super().flush()

    def _write_live_line(self, line: str) -> None:
        self._stream.write(f"{clean_terminal_text(line)}\n")
        self._stream.flush()


def _cfg_get(cfg: dict, *keys: str, default=None):
    current = cfg
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _cfg_set(cfg: dict, *keys: str, value) -> None:
    current = cfg
    for key in keys[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[keys[-1]] = value


def _parse_source_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _unique_sources(source_names: Iterable[str]) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for source_name in source_names:
        normalized = source_name.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        sources.append(normalized)
    return sources


def available_source_names(cfg: dict) -> list[str]:
    configured = _parse_source_names(_cfg_get(cfg, "sources", "enabled", default=[]))
    return _unique_sources([*configured, *CANONICAL_SOURCES])


def config_enabled_sources(cfg: dict, fallback: Iterable[str]) -> list[str]:
    configured = _parse_source_names(_cfg_get(cfg, "sources", "enabled", default=[]))
    return _unique_sources(configured or fallback)


def build_initial_state(args: argparse.Namespace, cfg: dict) -> TuiState:
    quick_preset = PRESETS[0]
    keywords = list(quick_preset.keywords)
    args_db = Path(str(args.db)).expanduser()
    state = TuiState(
        profile=str(_cfg_get(cfg, "profile", default=args.profile)),
        db=str(args.db if args_db.is_absolute() else _cfg_get(cfg, "db", default=args.db)),
        export=str(_cfg_get(cfg, "export", default=args.export)),
        openai_model=str(_cfg_get(cfg, "openai_model", default=args.openai_model)),
        analysis_strategy=str(
            _cfg_get(cfg, "analysis_strategy", default=args.analysis_strategy)
        ),
        enabled_sources=list(quick_preset.sources),
        limit=int(quick_preset.limit or _cfg_get(cfg, "search", "limit", default=args.limit)),
        max_pages=int(
            quick_preset.max_pages or _cfg_get(cfg, "search", "max_pages", default=args.max_pages)
        ),
        search_keywords=keywords,
        source_keywords={
            source_name: list(source_keywords)
            for source_name, source_keywords in quick_preset.source_keywords.items()
        },
    )
    return restore_tui_state_from_config(state, cfg)


def restore_tui_state_from_config(state: TuiState, cfg: dict) -> TuiState:
    tui_cfg = _cfg_get(cfg, "tui", default={})
    if not isinstance(tui_cfg, dict) or not tui_cfg:
        return state

    state.selected_preset = str(tui_cfg.get("selected_preset", state.selected_preset))
    state.use_config_keywords = bool(tui_cfg.get("use_config_keywords", state.use_config_keywords))
    state.refresh = bool(tui_cfg.get("refresh", state.refresh))

    enabled_sources = _parse_source_names(
        tui_cfg.get("enabled_sources", _cfg_get(cfg, "sources", "enabled", default=[]))
    )
    if enabled_sources:
        state.enabled_sources = enabled_sources

    keywords = parse_search_keywords(
        tui_cfg.get("search_keywords", _cfg_get(cfg, "search", "keywords", default=[]))
    )
    if keywords:
        state.search_keywords = keywords

    source_keywords = tui_cfg.get("source_keywords", {})
    if isinstance(source_keywords, dict):
        restored_source_keywords = {}
        for source_name, keywords_value in source_keywords.items():
            parsed_keywords = parse_search_keywords(keywords_value)
            if parsed_keywords:
                restored_source_keywords[str(source_name)] = parsed_keywords
        state.source_keywords = restored_source_keywords

    limit = tui_cfg.get("limit", _cfg_get(cfg, "search", "limit", default=state.limit))
    max_pages = tui_cfg.get(
        "max_pages",
        _cfg_get(cfg, "search", "max_pages", default=state.max_pages),
    )
    try:
        state.limit = int(limit)
    except (TypeError, ValueError):
        pass
    try:
        state.max_pages = int(max_pages)
    except (TypeError, ValueError):
        pass

    state.analysis_strategy = str(
        tui_cfg.get("analysis_strategy", _cfg_get(cfg, "analysis_strategy", default=state.analysis_strategy))
    )
    return state


def apply_tui_state_to_config(cfg: dict, state: TuiState) -> dict:
    saved_cfg = copy.deepcopy(cfg)
    saved_cfg["profile"] = state.profile
    saved_cfg["db"] = state.db
    saved_cfg["export"] = state.export
    saved_cfg["openai_model"] = state.openai_model
    saved_cfg["analysis_strategy"] = state.analysis_strategy
    _cfg_set(saved_cfg, "sources", "enabled", value=list(state.enabled_sources))
    _cfg_set(saved_cfg, "search", "keywords", value=list(state.search_keywords))
    _cfg_set(saved_cfg, "search", "limit", value=int(state.limit))
    _cfg_set(saved_cfg, "search", "max_pages", value=int(state.max_pages))
    saved_cfg["tui"] = {
        "selected_preset": state.selected_preset,
        "use_config_keywords": bool(state.use_config_keywords),
        "enabled_sources": list(state.enabled_sources),
        "search_keywords": list(state.search_keywords),
        "source_keywords": {
            source_name: list(keywords)
            for source_name, keywords in state.source_keywords.items()
        },
        "limit": int(state.limit),
        "max_pages": int(state.max_pages),
        "analysis_strategy": state.analysis_strategy,
        "refresh": bool(state.refresh),
    }
    return saved_cfg


def save_tui_config(config_path: str | Path, cfg: dict, state: TuiState) -> dict:
    if not config_path:
        return cfg

    path = Path(config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)

    saved_cfg = apply_tui_state_to_config(cfg, state)
    path.write_text(
        yaml.safe_dump(
            saved_cfg,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    return saved_cfg


def build_runner_args(
    state: TuiState,
    *,
    import_urls: str = "",
) -> argparse.Namespace:
    keywords = state.search_keywords or ["automation"]
    return argparse.Namespace(
        config="",
        profile=state.profile,
        db=state.db,
        export=state.export,
        keyword=keywords[0],
        keywords="\n".join(keywords),
        listing_url="",
        limit=state.limit,
        max_pages=state.max_pages,
        source="live",
        sources=",".join(state.enabled_sources),
        cvbankas=False,
        analysis_strategy=state.analysis_strategy,
        openai_model=state.openai_model,
        refresh=state.refresh,
        import_urls=import_urls,
        import_urls_file="",
        enabled_sources=list(state.enabled_sources),
        search_keywords=list(keywords),
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


def build_runner_config(cfg: dict, state: TuiState) -> dict:
    run_cfg = copy.deepcopy(cfg)
    if state.use_config_keywords:
        return run_cfg

    run_cfg.setdefault("sources", {})
    run_cfg["sources"].setdefault("keywords", {})
    for source_name in state.enabled_sources:
        run_cfg["sources"]["keywords"][source_name] = list(
            state.source_keywords.get(source_name) or state.search_keywords
        )
    return run_cfg


def parse_status_choice(value: str) -> ApplicationStatus:
    normalized = value.strip().lower()
    for status in TUI_STATUS_OPTIONS:
        if status.value.lower() == normalized:
            return status
    raise ValueError(f"Unsupported status: {value}")


class JobSeekerTui:
    def __init__(self, args: argparse.Namespace, cfg: dict) -> None:
        configure_terminal_encoding()
        self.args = args
        self.cfg = cfg
        self.state = build_initial_state(args, cfg)
        self.console = Console(legacy_windows=False)
        self.last_log = ""
        self.config_path = str(getattr(args, "config", "") or "")

    def run(self) -> int:
        while True:
            self.console.clear()
            self._render_home()
            choice = Prompt.ask(
                "Action",
                choices=("1", "2", "3", "4", "5", "6", "7", "8", "9", "q"),
                default="5",
                console=self.console,
            )
            if choice == "1":
                self._choose_sources()
            elif choice == "2":
                self._choose_preset()
            elif choice == "3":
                self._edit_settings()
            elif choice == "4":
                self._run_search()
            elif choice == "5":
                self._show_vacancies()
            elif choice == "6":
                self._import_urls()
            elif choice == "7":
                self._show_last_log()
            elif choice == "8":
                self._show_inbox()
            elif choice == "9":
                self._show_today_and_actions()
            else:
                return 0

    def _render_home(self) -> None:
        self.console.print(self._summary_panel())
        self.console.print(self._main_menu_table())
        items = self._load_items()
        if items:
            self.console.print(self._vacancy_table(items[:10], title="Latest saved vacancies"))
        else:
            self.console.print(
                Panel(
                    "No vacancies in the database yet. Run Search or Import URLs.",
                    title="Vacancies",
                    border_style="yellow",
                )
            )

    def _summary_panel(self) -> Panel:
        keyword_text = "config source keywords"
        if not self.state.use_config_keywords:
            keyword_text = ", ".join(self.state.search_keywords[:6])
            if len(self.state.search_keywords) > 6:
                keyword_text += f" (+{len(self.state.search_keywords) - 6})"

        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="bold cyan")
        summary.add_column()
        summary.add_row("Preset", self.state.selected_preset)
        summary.add_row("Sources", ", ".join(self.state.enabled_sources) or "-")
        summary.add_row("Keywords", keyword_text or "-")
        summary.add_row("Limit/pages", f"{self.state.limit} per source / {self.state.max_pages}")
        summary.add_row(
            "Execution",
            f"parallel ({len(self.state.enabled_sources)} source workers)",
        )
        summary.add_row("Analysis", self.state.analysis_strategy)
        summary.add_row("Database", self.state.db)
        if "hh" in self.state.enabled_sources:
            hh_options = _cfg_get(self.cfg, "sources", "options", "hh", default={})
            hh_mode = hh_options.get("fetch_mode", "http") if isinstance(hh_options, dict) else "http"
            summary.add_row("HH mode", str(hh_mode))
        return Panel(summary, title="Job Seeker TUI", border_style="cyan")

    def _main_menu_table(self) -> Table:
        table = Table(box=box.SIMPLE, expand=True, show_header=False)
        table.add_column("Key", style="bold green", width=4)
        table.add_column("Action")
        table.add_row("1", "Choose sources with checkboxes")
        table.add_row("2", "Choose preset")
        table.add_row("3", "Settings: limit, pages, strategy, custom keywords")
        table.add_row("4", "Start search")
        table.add_row("5", "Vacancy table, details, status")
        table.add_row("6", "Import pasted vacancy URLs")
        table.add_row("7", "Show last run log")
        table.add_row("8", "Explained inbox and preferences")
        table.add_row("9", "Today, actions, reminders")
        table.add_row("q", "Quit")
        return table

    def _choose_sources(self) -> None:
        all_sources = available_source_names(self.cfg)
        while True:
            self.console.clear()
            table = Table(title="Sources", box=box.SIMPLE, expand=True)
            table.add_column("#", justify="right", width=4)
            table.add_column("Use", width=5)
            table.add_column("Source")
            selected = set(self.state.enabled_sources)
            for index, source_name in enumerate(all_sources, start=1):
                table.add_row(str(index), "[x]" if source_name in selected else "[ ]", source_name)
            self.console.print(table)
            raw = Prompt.ask(
                "Toggle numbers, 'a' all, 'n' none, Enter done",
                default="",
                console=self.console,
            )
            if not raw:
                if self.state.enabled_sources:
                    self._save_config()
                    return
                self.console.print("[red]Choose at least one source.[/red]")
                self._pause()
                continue
            normalized = raw.strip().lower()
            if normalized == "a":
                self.state.enabled_sources = list(all_sources)
                continue
            if normalized == "n":
                self.state.enabled_sources = []
                continue
            for token in re.split(r"[\s,;]+", raw):
                if not token.isdigit():
                    continue
                index = int(token)
                if not 1 <= index <= len(all_sources):
                    continue
                source_name = all_sources[index - 1]
                if source_name in self.state.enabled_sources:
                    self.state.enabled_sources.remove(source_name)
                else:
                    self.state.enabled_sources.append(source_name)

    def _choose_preset(self) -> None:
        self.console.clear()
        table = Table(title="Presets", box=box.SIMPLE, expand=True)
        table.add_column("#", justify="right", width=4)
        table.add_column("Preset", style="bold")
        table.add_column("Sources")
        table.add_column("Description")
        configured_sources = config_enabled_sources(self.cfg, self.state.enabled_sources)
        for index, preset in enumerate(PRESETS, start=1):
            sources = ", ".join(preset.sources or tuple(configured_sources))
            table.add_row(str(index), preset.name, sources, preset.description)
        table.add_row(str(len(PRESETS) + 1), "Custom keywords", "current sources", "Paste your own keyword list.")
        self.console.print(table)

        choice = IntPrompt.ask(
            "Preset number",
            default=1,
            console=self.console,
        )
        if choice == len(PRESETS) + 1:
            self._edit_custom_keywords()
            return
        if not 1 <= choice <= len(PRESETS):
            self.console.print("[red]Unknown preset.[/red]")
            self._pause()
            return

        preset = PRESETS[choice - 1]
        self.state.selected_preset = preset.name
        self.state.use_config_keywords = preset.use_config_keywords
        if preset.sources:
            self.state.enabled_sources = list(preset.sources)
        else:
            self.state.enabled_sources = configured_sources
        if preset.keywords:
            self.state.search_keywords = list(preset.keywords)
        self.state.source_keywords = {
            source_name: list(source_keywords)
            for source_name, source_keywords in preset.source_keywords.items()
        }
        if preset.limit is not None:
            self.state.limit = preset.limit
        if preset.max_pages is not None:
            self.state.max_pages = preset.max_pages
        self._save_config()

    def _edit_settings(self) -> None:
        self.console.clear()
        self.console.print(self._summary_panel())
        self.state.limit = IntPrompt.ask(
            "Limit per source",
            default=self.state.limit,
            console=self.console,
        )
        self.state.max_pages = IntPrompt.ask(
            "Max listing pages per keyword",
            default=self.state.max_pages,
            console=self.console,
        )
        self.state.analysis_strategy = Prompt.ask(
            "Analysis strategy",
            choices=("ai", "rule"),
            default=self.state.analysis_strategy,
            console=self.console,
        )
        self.state.refresh = Confirm.ask(
            "Reprocess already saved vacancies",
            default=self.state.refresh,
            console=self.console,
        )
        if Confirm.ask("Edit custom keywords", default=False, console=self.console):
            self._edit_custom_keywords()
        self._save_config()

    def _edit_custom_keywords(self) -> None:
        raw = self._prompt_multiline(
            "Paste keywords separated by comma, semicolon or newline. Blank line finishes."
        )
        keywords = parse_search_keywords(raw)
        if not keywords:
            self.console.print("[red]No keywords entered.[/red]")
            self._pause()
            return
        self.state.search_keywords = keywords
        self.state.source_keywords = {}
        self.state.use_config_keywords = False
        self.state.selected_preset = "Custom keywords"
        self._save_config()

    def _run_search(self) -> None:
        if not self.state.enabled_sources:
            self.console.print("[red]Choose at least one source first.[/red]")
            self._pause()
            return

        runner_args = build_runner_args(self.state)
        run_cfg = build_runner_config(self.cfg, self.state)
        output = LiveLogWriter()
        self.console.clear()
        self.console.rule("[bold cyan]Search live log")
        self.console.print("Press Ctrl+C to stop the current run.\n", style="dim")
        try:
            with redirect_stdout(output):
                exit_code = run_batch(runner_args, run_cfg)
        except KeyboardInterrupt:
            exit_code = 130
            output.write("Stopped by user.\n")
        except Exception as error:  # noqa: BLE001 - TUI should keep running
            exit_code = 1
            output.write(f"ERROR | {error}\n")
        finally:
            output.flush()

        self.last_log = clean_terminal_text(output.getvalue())
        style = "green" if exit_code == 0 else "yellow"
        self.console.print(
            Panel(self._run_summary(self.last_log), title="Search finished", border_style=style)
        )
        self._pause()

    def _import_urls(self) -> None:
        if not self.state.enabled_sources:
            self.console.print("[red]Choose at least one source first.[/red]")
            self._pause()
            return
        raw = self._prompt_multiline(
            "Paste vacancy URLs separated by newlines, spaces or commas. Blank line finishes."
        )
        if not raw.strip():
            return

        runner_args = build_runner_args(self.state, import_urls=raw)
        run_cfg = build_runner_config(self.cfg, self.state)
        output = LiveLogWriter()
        self.console.clear()
        self.console.rule("[bold cyan]Import live log")
        self.console.print("Press Ctrl+C to stop the current import.\n", style="dim")
        try:
            with redirect_stdout(output):
                exit_code = run_import(runner_args, run_cfg)
        except KeyboardInterrupt:
            exit_code = 130
            output.write("Stopped by user.\n")
        except Exception as error:  # noqa: BLE001 - TUI should keep running
            exit_code = 1
            output.write(f"ERROR | {error}\n")
        finally:
            output.flush()

        self.last_log = clean_terminal_text(output.getvalue())
        style = "green" if exit_code == 0 else "yellow"
        self.console.print(
            Panel(self._run_summary(self.last_log), title="Import finished", border_style=style)
        )
        self._pause()

    def _show_vacancies(self) -> None:
        while True:
            self.console.clear()
            items = self._load_items()
            if not items:
                self.console.print("[yellow]No saved vacancies found.[/yellow]")
                self._pause()
                return
            self.console.print(self._vacancy_table(items, title="Saved vacancies"))
            raw = Prompt.ask(
                "Vacancy # details, 'o #' open, 's #' status, Enter back",
                default="",
                console=self.console,
            )
            if not raw:
                return
            if raw.lower().startswith("o"):
                index = self._index_from_text(raw[1:], len(items))
                if index is not None:
                    webbrowser.open(items[index].source_url)
                    self.console.print("[green]Opened URL in browser.[/green]")
                    self._pause()
                continue
            if raw.lower().startswith("s"):
                index = self._index_from_text(raw[1:], len(items))
                if index is not None:
                    self._change_status(items[index])
                continue
            index = self._index_from_text(raw, len(items))
            if index is not None:
                self._show_details(items[index])

    def _show_inbox(self) -> None:
        while True:
            self.console.clear()
            database = self._open_database()
            try:
                preferences = database.get_inbox_preferences()
                items = database.query_inbox(preferences=preferences)
                latest_run = database.get_latest_inbox_run()
            finally:
                database.close()
            self.console.print(self._inbox_preferences_panel(preferences, latest_run_status=latest_run.status if latest_run else None))
            if items:
                self.console.print(self._inbox_table(items, title="Explained inbox"))
            else:
                self.console.print("[yellow]No inbox vacancies match the current preferences.[/yellow]")
            raw = Prompt.ask(
                "Action: prefs, details #, status #, open #, back",
                default="back",
                console=self.console,
            ).strip()
            if not raw or raw.lower() == "back":
                return
            if raw.lower() == "prefs":
                self._edit_inbox_preferences()
                continue
            command, _, remainder = raw.partition(" ")
            index_text = remainder if command.lower() in {"details", "status", "open"} else raw
            index = self._index_from_text(index_text, len(items))
            if index is None:
                continue
            item = self._vacancy_list_item_from_inbox(items[index])
            if command.lower() == "open":
                webbrowser.open(item.source_url)
                self.console.print("[green]Opened URL in browser.[/green]")
                self._pause()
            elif command.lower() == "status":
                self._change_status(item)
            else:
                self._show_details(item)

    def _edit_inbox_preferences(self) -> None:
        database = self._open_database()
        try:
            current = database.get_inbox_preferences()
        finally:
            database.close()
        minimum_score = IntPrompt.ask(
            "Minimum score threshold",
            default=current.minimum_score,
            console=self.console,
        )
        hide = Confirm.ask(
            "Hide below-threshold vacancies",
            default=current.hide_below_threshold,
            console=self.console,
        )
        sort_by = Prompt.ask(
            "Sort by",
            choices=("score", "newest", "title", "company"),
            default=current.sort_by,
            console=self.console,
        )
        source_name = Prompt.ask(
            "Source filter (blank for all)",
            default=current.source_name,
            console=self.console,
        ).strip()
        fit_label = Prompt.ask(
            "Fit filter: High, Medium, Low, or blank",
            default=current.fit_label,
            console=self.console,
        ).strip()
        application_status = Prompt.ask(
            "Application status filter (Saved/Applied/Interview/Offer/Rejected/Withdrawn or blank)",
            default=current.application_status,
            console=self.console,
        ).strip()
        new_only = Confirm.ask(
            "Show only vacancies new in the selected/latest run",
            default=current.new_only,
            console=self.console,
        )
        current_run_only = Confirm.ask(
            "Limit inbox to latest completed/partial run",
            default=current.current_run_only,
            console=self.console,
        )
        database = self._open_database()
        try:
            database.save_inbox_preferences(
                InboxPreferences(
                    minimum_score=minimum_score,
                    hide_below_threshold=hide,
                    sort_by=sort_by,
                    source_name=source_name,
                    fit_label=fit_label,
                    application_status=application_status,
                    new_only=new_only,
                    current_run_only=current_run_only,
                )
            )
        except ValueError as error:
            self.console.print(f"[red]Invalid inbox preferences: {error}[/red]")
            self._pause()
            return
        finally:
            database.close()
        self.console.print("[green]Inbox preferences saved for CLI/TUI/web.[/green]")
        self._pause()

    def _show_today_and_actions(self) -> None:
        while True:
            self.console.clear()
            database = self._open_database()
            try:
                preferences = database.get_inbox_preferences()
                latest_run = database.get_latest_inbox_run()
                inbox_items = database.query_inbox(preferences=preferences, new_only=True)
                actions = database.list_action_items()
                reminders = database.query_action_reminders()
                timezone_name = ActionService(database).resolve_user_timezone()
            finally:
                database.close()
            if latest_run and latest_run.status == "partial":
                self.console.print("[yellow]Latest inbox run is PARTIAL - results came from an incomplete collection.[/yellow]")
            self.console.print(self._today_table(inbox_items, reminders, timezone_name=timezone_name))
            self.console.print(self._actions_table(actions, title="Actions / reminders", timezone_name=timezone_name))
            raw = Prompt.ask(
                "Action: create, edit #, complete #, reopen #, back",
                default="back",
                console=self.console,
            ).strip()
            if not raw or raw.lower() == "back":
                return
            if raw.lower() == "create":
                self._create_action()
                continue
            command, _, remainder = raw.partition(" ")
            index = self._index_from_text(remainder, len(actions))
            if index is None:
                continue
            database = self._open_database()
            service = ActionService(database)
            try:
                if command.lower() == "complete":
                    service.complete_action(actions[index].id)
                    self.console.print("[green]Action completed.[/green]")
                elif command.lower() == "reopen":
                    service.reopen_action(actions[index].id)
                    self.console.print("[green]Action reopened.[/green]")
                elif command.lower() == "edit":
                    self._edit_action(actions[index].id)
                    return
            except ValueError as error:
                self.console.print(f"[red]{error}[/red]")
            finally:
                database.close()
            self._pause()

    def _create_action(self) -> None:
        items = self._load_items()
        if not items:
            self.console.print("[yellow]No saved vacancies found.[/yellow]")
            self._pause()
            return
        self.console.print(self._vacancy_table(items[:20], title="Choose vacancy for action"))
        index = self._index_from_text(Prompt.ask("Vacancy #", console=self.console), len(items[:20]))
        if index is None:
            return
        title = Prompt.ask("Action title", console=self.console).strip()
        due = Prompt.ask(
            "Due local datetime (YYYY-MM-DDTHH:MM:SS, blank for none)",
            default="",
            console=self.console,
        ).strip()
        notes = Prompt.ask("Notes", default="", console=self.console)
        database = self._open_database()
        service = ActionService(database)
        try:
            fold = self._prompt_dst_fold_if_needed(service, due or None)
            action = service.create_action(
                vacancy_source_url=items[index].source_url,
                title=title,
                notes=notes,
                local_due_at=due or None,
                fold=fold,
            )
            self.console.print(f"[green]Created action #{action.id}: {action.title}[/green]")
        except ValueError as error:
            self.console.print(f"[red]{error}[/red]")
        finally:
            database.close()
        self._pause()

    def _prompt_dst_fold_if_needed(self, service: ActionService, due: str | None) -> int | None:
        if not due:
            return None
        try:
            service.local_due_to_utc(due)
            return None
        except ValueError as error:
            if "Ambiguous local time requires" not in str(error):
                raise
        choice = Prompt.ask(
            "Ambiguous DST local time; choose earlier or later occurrence",
            choices=("earlier", "later"),
            default="earlier",
            console=self.console,
        )
        return 0 if choice == "earlier" else 1

    def _edit_action(self, action_id: int) -> None:
        database = self._open_database()
        try:
            current = database.get_action_item(action_id)
        finally:
            database.close()
        if current is None:
            self.console.print(f"[red]Action not found: {action_id}[/red]")
            self._pause()
            return
        title = Prompt.ask("Title", default=current.title, console=self.console).strip()
        notes = Prompt.ask("Notes", default=current.notes, console=self.console)
        due = Prompt.ask(
            "Due local datetime (YYYY-MM-DDTHH:MM:SS, blank keeps current)",
            default="",
            console=self.console,
        ).strip()
        clear_due = Confirm.ask("Clear due time", default=False, console=self.console)
        database = self._open_database()
        service = ActionService(database)
        try:
            fold = None if clear_due else self._prompt_dst_fold_if_needed(service, due or None)
            action = service.update_action(
                action_id,
                title=title,
                notes=notes,
                local_due_at=due or None,
                clear_due=clear_due,
                fold=fold,
            )
            self.console.print(f"[green]Updated action #{action.id}: {action.title}[/green]")
        except ValueError as error:
            self.console.print(f"[red]{error}[/red]")
        finally:
            database.close()
        self._pause()

    def _show_details(self, item: VacancyListItem) -> None:
        database = self._open_database()
        try:
            vacancy = database.get_vacancy(item.source_url)
            analysis = database.get_latest_analysis(item.source_url)
            application = database.get_application_record(item.source_url)
            history = database.list_application_status_events(item.source_url)
            actions = database.list_action_items(vacancy_source_url=item.source_url)
        finally:
            database.close()

        if vacancy is None:
            self.console.print("[red]Vacancy disappeared from database.[/red]")
            self._pause()
            return

        requirements = self._format_list(vacancy.requirements)
        responsibilities = self._format_list(vacancy.responsibilities)
        raw_preview = " ".join(vacancy.raw_text.split())[:900]
        details = Table.grid(padding=(0, 2))
        details.add_column(style="bold cyan", no_wrap=True)
        details.add_column(ratio=1)
        details.add_row("Title", vacancy.title)
        details.add_row("Company", vacancy.company or "-")
        details.add_row("Location", vacancy.location or "-")
        details.add_row("Salary", vacancy.salary_text or "-")
        details.add_row("Source", f"{vacancy.source_name} / {vacancy.source_id}")
        details.add_row("Score", str(analysis.score) if analysis else "-")
        details.add_row("Fit", analysis.fit_label.value if analysis else "-")
        details.add_row("Status", application.status.value if application else "Not tracked")
        details.add_row("History", self._status_history_text(history))
        details.add_row("Actions", self._action_summary_text(actions))
        details.add_row("Requirements", requirements)
        details.add_row("Responsibilities", responsibilities)
        details.add_row("Explanation", analysis.explanation if analysis else "-")
        details.add_row("Raw", raw_preview or "-")
        details.add_row("URL", self._link_text(vacancy.source_url, vacancy.source_url))

        while True:
            self.console.clear()
            self.console.print(Panel(details, title="Vacancy details", border_style="cyan"))
            choice = Prompt.ask(
                "Action: status, action, open, back",
                choices=("status", "action", "open", "back"),
                default="back",
                console=self.console,
            )
            if choice == "status":
                self._change_status(item)
                return
            if choice == "action":
                self._create_action_for_item(item)
                return
            if choice == "open":
                webbrowser.open(vacancy.source_url)
                self.console.print("[green]Opened URL in browser.[/green]")
                self._pause()
                return
            return

    def _create_action_for_item(self, item: VacancyListItem) -> None:
        title = Prompt.ask("Action title", console=self.console).strip()
        due = Prompt.ask(
            "Due local datetime (YYYY-MM-DDTHH:MM:SS, blank for none)",
            default="",
            console=self.console,
        ).strip()
        notes = Prompt.ask("Notes", default="", console=self.console)
        database = self._open_database()
        service = ActionService(database)
        try:
            action = service.create_action(
                vacancy_source_url=item.source_url,
                title=title,
                notes=notes,
                local_due_at=due or None,
            )
            self.console.print(f"[green]Created action #{action.id}: {action.title}[/green]")
        except ValueError as error:
            self.console.print(f"[red]{error}[/red]")
        finally:
            database.close()
        self._pause()

    def _change_status(self, item: VacancyListItem) -> None:
        choices = [status.value.lower() for status in TUI_STATUS_OPTIONS]
        current_status = (
            item.application_status.value.lower()
            if item.application_status in TUI_STATUS_OPTIONS
            else "saved"
        )
        choice = Prompt.ask(
            "New status",
            choices=tuple(choices),
            default=current_status,
            console=self.console,
        )
        desired_status = parse_status_choice(choice)
        database = self._open_database()
        try:
            tracker = ApplicationTracker(database)
            latest_analysis_id = database.get_latest_analysis_id(item.source_url)
            tracker.ensure_record(
                item.source_url,
                analysis_id=latest_analysis_id,
                notes="Created from TUI.",
                origin=ApplicationStatusOrigin.TUI,
            )
            try:
                tracker.update_status(
                    item.source_url,
                    desired_status,
                    origin=ApplicationStatusOrigin.TUI,
                )
            except ValueError as error:
                self.console.print(f"[yellow]{error}[/yellow]")
                if not Confirm.ask(
                    "Use corrective reassignment with an audit reason?",
                    default=False,
                    console=self.console,
                ):
                    self._pause()
                    return
                reason = Prompt.ask(
                    "Correction reason",
                    default="",
                    console=self.console,
                ).strip()
                if not reason:
                    self.console.print("[red]Correction reason is required.[/red]")
                    self._pause()
                    return
                tracker.set_status(
                    item.source_url,
                    desired_status,
                    analysis_id=latest_analysis_id,
                    notes="Changed from TUI.",
                    origin=ApplicationStatusOrigin.TUI,
                    reason=reason,
                )
        finally:
            database.close()
        self.console.print(f"[green]Status changed to {desired_status.value}.[/green]")
        self._pause()

    def _show_last_log(self) -> None:
        self.console.clear()
        if not self.last_log:
            self.console.print("[yellow]No run log yet.[/yellow]")
        else:
            self.console.print(
                Panel(
                    clean_terminal_text(self.last_log[-6000:]),
                    title="Last run log",
                    border_style="cyan",
                )
            )
        self._pause()

    def _save_config(self) -> None:
        if not self.config_path:
            return
        try:
            self.cfg = save_tui_config(self.config_path, self.cfg, self.state)
        except Exception as error:  # noqa: BLE001 - TUI should stay usable
            self.console.print(f"[yellow]Could not save config: {error}[/yellow]")
            self._pause()

    def _load_items(self) -> list[VacancyListItem]:
        database = self._open_database()
        try:
            return database.list_vacancies_with_latest_scores()
        finally:
            database.close()

    def _open_database(self) -> DatabaseManager:
        database = DatabaseManager(Path.cwd() / self.state.db)
        return database

    def _inbox_preferences_panel(self, preferences: InboxPreferences, *, latest_run_status: str | None = None) -> Panel:
        visibility = "hidden" if preferences.hide_below_threshold else "shown"
        partial_note = "\nLatest run: PARTIAL - incomplete collection" if latest_run_status == "partial" else ""
        return Panel(
            (
                f"Minimum score: {preferences.minimum_score}\n"
                f"Below threshold: {visibility}\n"
                f"Sort: {preferences.sort_by}\n"
                f"Source filter: {preferences.source_name or '*'}\n"
                f"Fit filter: {preferences.fit_label or '*'}\n"
                f"Status filter: {preferences.application_status or '*'}\n"
                f"New only: {preferences.new_only}\n"
                f"Current run only: {preferences.current_run_only}"
                f"{partial_note}"
            ),
            title="Shared inbox preferences",
            border_style="cyan",
        )

    def _inbox_table(self, items: list[InboxItem], *, title: str) -> Table:
        table = Table(title=title, box=box.SIMPLE_HEAVY, expand=True)
        table.add_column("#", justify="right", width=4)
        table.add_column("Score", justify="right", width=6)
        table.add_column("Fit", width=8)
        table.add_column("New", width=7)
        table.add_column("Status", width=10)
        table.add_column("Title", ratio=2, overflow="ellipsis")
        table.add_column("Company", ratio=1, overflow="ellipsis")
        table.add_column("Why", ratio=2, overflow="ellipsis")
        for index, item in enumerate(items, start=1):
            table.add_row(
                str(index),
                "-" if item.latest_score is None else str(item.latest_score),
                item.latest_fit_label or "-",
                "yes" if item.is_new_in_run else "-",
                item.application_status.value if item.application_status else "-",
                self._clip(item.title, 64),
                self._clip(item.company, 28),
                self._clip(item.explanation, 80),
            )
        return table

    def _today_table(
        self,
        inbox_items: list[InboxItem],
        reminders: list[ActionReminderItem],
        *,
        timezone_name: str = "UTC",
    ) -> Table:
        table = Table(title="Today", box=box.SIMPLE_HEAVY, expand=True)
        table.add_column("Type", width=12)
        table.add_column("State", width=12)
        table.add_column("Item", ratio=2, overflow="ellipsis")
        table.add_column("When/Score", width=20, overflow="ellipsis")
        if not inbox_items and not reminders:
            table.add_row("empty", "-", "No new recommendations or due actions.", "-")
        for item in inbox_items[:10]:
            table.add_row(
                "vacancy",
                item.latest_fit_label or "-",
                self._clip(item.title, 70),
                "-" if item.latest_score is None else str(item.latest_score),
            )
        for reminder in reminders:
            action = reminder.action
            table.add_row(
                "action",
                reminder.reminder_state,
                self._clip(action.title, 70),
                self._local_due_text(action.due_at_utc, timezone_name),
            )
        return table

    def _actions_table(self, actions: list[ActionItem], *, title: str, timezone_name: str = "UTC") -> Table:
        table = Table(title=title, box=box.SIMPLE_HEAVY, expand=True)
        table.add_column("#", justify="right", width=4)
        table.add_column("ID", justify="right", width=5)
        table.add_column("State", width=10)
        table.add_column("Due local", width=28)
        table.add_column("Title", ratio=2, overflow="ellipsis")
        table.add_column("Vacancy", ratio=1, overflow="ellipsis")
        if not actions:
            table.add_row("-", "-", "-", "-", "No actions yet.", "-")
        for index, action in enumerate(actions, start=1):
            table.add_row(
                str(index),
                str(action.id),
                action.state.value,
                self._local_due_text(action.due_at_utc, timezone_name),
                self._clip(action.title, 70),
                self._short_url(action.vacancy_source_url),
            )
        return table

    @staticmethod
    def _local_due_text(due_at_utc: str | None, timezone_name: str) -> str:
        if not due_at_utc:
            return "-"
        return f"{utc_iso_to_local_datetime(due_at_utc, timezone_name)} ({timezone_name})"

    @staticmethod
    def _vacancy_list_item_from_inbox(item: InboxItem) -> VacancyListItem:
        return VacancyListItem(
            source_name=item.source_name,
            source_id=item.source_id,
            source_url=item.source_url,
            title=item.title,
            company=item.company,
            location=item.location,
            latest_score=item.latest_score,
            latest_fit_label=item.latest_fit_label,
            application_status=item.application_status,
        )

    @staticmethod
    def _status_history_text(events) -> str:
        if not events:
            return "-"
        parts = []
        for event in events[-5:]:
            previous = event.previous_status.value if event.previous_status else "-"
            parts.append(f"{previous} -> {event.new_status.value} ({event.origin.value}/{event.kind.value})")
        return "\n".join(parts)

    @staticmethod
    def _action_summary_text(actions: list[ActionItem]) -> str:
        if not actions:
            return "-"
        open_count = sum(1 for action in actions if action.state == ActionState.OPEN)
        completed_count = sum(1 for action in actions if action.state == ActionState.COMPLETED)
        return f"{open_count} open, {completed_count} completed"

    def _vacancy_table(self, items: list[VacancyListItem], *, title: str) -> Table:
        table = Table(title=title, box=box.SIMPLE_HEAVY, expand=True)
        table.add_column("#", justify="right", width=4, no_wrap=True)
        table.add_column("Score", justify="right", width=6, no_wrap=True)
        table.add_column("Fit", width=8, no_wrap=True, overflow="ellipsis")
        table.add_column("Status", width=10, no_wrap=True, overflow="ellipsis")
        table.add_column("Source", width=12, no_wrap=True, overflow="ellipsis")
        table.add_column("Title", ratio=2, no_wrap=True, overflow="ellipsis")
        table.add_column("Company", ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column("Location", ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column("Link", ratio=1, no_wrap=True, overflow="ellipsis")
        for index, item in enumerate(items, start=1):
            table.add_row(
                str(index),
                "-" if item.latest_score is None else str(item.latest_score),
                item.latest_fit_label or "-",
                item.application_status.value if item.application_status else "-",
                item.source_name,
                self._clip(item.title, 70),
                self._clip(item.company, 32),
                self._clip(item.location, 24),
                self._link_text(item.source_url, self._short_url(item.source_url)),
            )
        return table

    def _prompt_multiline(self, message: str) -> str:
        self.console.print(Panel(message, border_style="cyan"))
        lines: list[str] = []
        while True:
            line = self.console.input("> ")
            if not line.strip():
                break
            lines.append(line)
        return "\n".join(lines)

    def _short_log(self, log_text: str) -> Group:
        log_text = clean_terminal_text(log_text)
        lines = [line for line in log_text.strip().splitlines() if line.strip()]
        if not lines:
            return Group(Text("No output."))
        interesting = lines[-18:]
        return Group(*[Text(line) for line in interesting])

    def _run_summary(self, log_text: str) -> Group:
        log_text = clean_terminal_text(log_text)
        lines = [line for line in log_text.strip().splitlines() if line.strip()]
        if not lines:
            return Group(Text("No output."))

        summary_lines: list[str] = []
        for line in lines:
            if (
                "Vacancy batch finished." in line
                or "URL import finished." in line
                or line.startswith("Report written to:")
                or line.startswith("ERROR |")
                or " LISTING ERROR | " in line
            ):
                summary_lines.append(line)

        if not summary_lines:
            summary_lines = lines[-5:]

        return Group(*[Text(line) for line in summary_lines[-8:]])

    def _pause(self) -> None:
        self.console.input("\nPress Enter to continue...")

    @staticmethod
    def _format_list(values: list[str], max_items: int = 8) -> str:
        if not values:
            return "-"
        visible = values[:max_items]
        text = "\n".join(f"- {value}" for value in visible)
        if len(values) > max_items:
            text += f"\n- ... +{len(values) - max_items} more"
        return text

    @staticmethod
    def _index_from_text(value: str, max_count: int) -> int | None:
        value = value.strip()
        if not value.isdigit():
            return None
        index = int(value)
        if not 1 <= index <= max_count:
            return None
        return index - 1

    @staticmethod
    def _clip(value: str, max_length: int) -> str:
        value = " ".join((value or "-").split())
        if len(value) <= max_length:
            return value
        return textwrap.shorten(value, width=max_length, placeholder="...")

    @staticmethod
    def _short_url(url: str, max_length: int = 44) -> str:
        parsed = urlparse(url)
        if not parsed.netloc:
            return textwrap.shorten(url, width=max_length, placeholder="...")
        compact = f"{parsed.netloc}{parsed.path}"
        return textwrap.shorten(compact, width=max_length, placeholder="...")

    @staticmethod
    def _link_text(url: str, label: str) -> Text:
        text = Text(label or url)
        if url:
            text.stylize(f"link {url}")
        return text


def run_tui(args: argparse.Namespace, cfg: dict) -> int:
    tui = JobSeekerTui(args, cfg)
    database = DatabaseManager(Path.cwd() / tui.state.db)
    database.initialize()
    service = ActionService(database)
    timezone_name = service.resolve_user_timezone()
    # A piped/non-interactive launch cannot provide an explicit confirmation.
    # Keep the detected timezone display-only in that case so commands such as
    # `printf 'q\\n' | python main.py` can still open and quit the TUI safely.
    if (
        tui.console.is_interactive
        and database.get_user_timezone_confirmation() is None
        and Confirm.ask(
            f"Use local timezone '{timezone_name}' for reminder due-time display?",
            default=True,
            console=tui.console,
        )
    ):
        service.confirm_user_timezone(timezone_name)
    database.close()
    return tui.run()
