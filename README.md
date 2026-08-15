# Job Seeker Intelligence

Multi-source job discovery, ranking, and application tracking for a local-first job search workflow.

The project collects vacancies from several job boards or pasted direct URLs, normalizes them into a shared model, stores them in SQLite, ranks them with deterministic rules or optional AI analysis, and exposes the results through a loopback-only web dashboard, a Rich terminal UI, a command-line interface, Markdown exports, and Telegram summaries.

> **New here?** The easiest way to use this tool is the browser dashboard. Do the one-time [Quick Start](#quick-start) install, then set up the [Always-On Web Dashboard](#always-on-web-dashboard-windows) and open `http://127.0.0.1:8000/`. Everything below is ordered from the easiest way to use the app to the deepest technical detail.

## Always-On Web Dashboard (Windows)

The friendliest way to run everything is the local web dashboard, kept always available on your own PC so it comes back after a reboot. It starts at logon, **runs as you** (not `SYSTEM`) so your `claude`/`codex` CLI login is available to the AI backends, and stays loopback-only — nothing is exposed to the LAN or internet.

> First time? Install the project once via [Quick Start](#quick-start) below, then run one of the install scripts here.

**Option A — Startup folder (no admin required, recommended).** Drops a hidden `.vbs` launcher into your Startup folder; it starts the dashboard at logon with no console window.

```powershell
# Install (starts at logon; also launches it right away):
powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_startup.ps1 -Port 8000

# Remove:
powershell -ExecutionPolicy Bypass -File scripts\uninstall_dashboard_startup.ps1
```

**Option B — Scheduled Task (requires an elevated / "Run as administrator" PowerShell).** Adds restart-on-failure on top of start-at-logon.

```powershell
# Install (run from an elevated PowerShell):
powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_service.ps1 -Port 8000

# Remove:
powershell -ExecutionPolicy Bypass -File scripts\uninstall_dashboard_service.ps1
```

Then open **http://127.0.0.1:8000/** in your browser.

- `scripts\run_dashboard.ps1` is the launcher both options call (also runnable by hand); it logs to `logs\dashboard.log`.
- Startup takes several seconds while the database opens; allow ~10 s after logon before the port answers.
- It starts at **logon**, not at the pre-login screen — that is required so the server runs under your account (with the CLI login). With Windows auto-login this is effectively "at power-on".
- The launcher relaxes `$ErrorActionPreference` to `Continue` before starting the server, because uvicorn writes its logs to stderr and under `Stop` the first stderr line would be promoted to a terminating error that kills the server before it binds.

## Quick Start

Requirements:

- Python 3.12 or newer;
- Chrome or Playwright Chromium for browser-based collection (HH, Startup Jobs, EU Remote Jobs).

```powershell
git clone https://github.com/Yashelly/job-application-agent.git
cd job-application-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium
job-seeker
```

The editable installation adds the `job-seeker` command. `python main.py` remains available for a zero-surprise local launch.

Copy and adjust the example configuration when needed:

```powershell
Copy-Item config\cvbankas.example.yaml config\cvbankas.local.yaml
```

The application automatically prefers `config/cvbankas.local.yaml`. TUI changes are persisted there between launches.

## Local Web Dashboard

The dashboard exposes saved vacancies, inbox preferences, applications, status history, actions, reminders, search/import, CV→profile, scheduling, and settings through a browser UI. To run it by hand (instead of the always-on setup):

```powershell
python main.py --web
```

By default it binds to `127.0.0.1:8000`. Custom binds must remain loopback-only; wildcard, LAN, and public hosts are rejected before Uvicorn starts:

```powershell
python main.py --web --web-host 127.0.0.1 --web-port 8765
```

The web dashboard has feature parity with the TUI. Its pages are:

- **Today** (`/today`) and **Vacancies** (`/vacancies`, `/vacancy?url=...`): the explained inbox and vacancy detail.
- **Search** (`/search`): pick sources, keywords, limits, pages, and analysis strategy, then run a collection in the background.
- **Profile** (`/profile`): shows the active profile and AI backend, lets you **upload a CV** (`.pdf/.docx/.txt/.md`) to build a profile, and **paste vacancy URLs to import**.
- **Applications** (`/applications`): tracked applications plus create/complete/reopen **actions & reminders**.
- **Schedule** (`/schedule`): turn on a daily collection + Telegram summary at a chosen local time, or run it now.
- **Settings** (`/settings`): shared inbox filters/sort, timezone, and the AI backend switcher.
- **Jobs** (`/jobs/{id}`): live progress page for a running search/import/daily job (polls a JSON log endpoint); one job runs at a time.

Mutating form posts use POST/redirect/get and are protected with:

- loopback-only Host validation;
- matching loopback Origin validation;
- a SameSite strict CSRF cookie and hidden form token (the CV upload uses a multipart-aware variant of the same check);
- escaped template output and validation of external vacancy links.

The dashboard is intentionally local. It is not an internet-facing multi-user service. Long-running searches run in an in-process background thread; the uploaded CV is written to a temporary file that is deleted right after text extraction. Building a profile from a CV needs an AI backend (`openai`, `claude_cli`, or `codex_cli`); the offline `rule`/`demo` backends show guidance instead.

## Build a Profile from Your CV

Instead of hand-editing the profile JSON, let an AI backend build it from your CV.

- **Web:** open **Profile** (`/profile`), upload a `.pdf`, `.docx`, `.txt`, or `.md`, review the extracted profile, and save it (optionally replacing your active profile).
- **TUI:** choose menu item `10`, point it at your CV file, review, and save. You are asked where to save each time (a `profile_from_cv.json` review file by default, or the active profile path to replace it).

The backend fills in roles, skills, must-have/nice-to-have skills, locations, and excluded keywords. This feature requires an AI backend — `openai` (with `OPENAI_API_KEY`), `claude_cli`, or `codex_cli` (your subscription CLI). The offline `rule`/`demo` backends cannot read a CV, and the UI will say so. PDF and DOCX parsing use `pypdf` and `python-docx`.

## Daily Telegram Summary and Scheduling

The daily run collects the configured sources once, keeps only vacancies that were not already in SQLite, and sends the highest-scoring new results to Telegram.

**One-time Telegram setup:**

1. Create a bot with [BotFather](https://t.me/BotFather) and copy its token.
2. Send `/start` to the new bot.
3. Add the token to `.env`:

```text
TELEGRAM_BOT_TOKEN=your-bot-token
```

4. Discover your chat ID, add it to `.env`, and test delivery:

```powershell
job-seeker --telegram-chat-id
```

```text
TELEGRAM_CHAT_ID=123456789
```

```powershell
job-seeker --telegram-test
job-seeker --daily-run
```

Telegram behavior is configured in YAML:

```yaml
telegram:
  max_vacancies: 10
  notify_when_empty: false
```

### Scheduling from the dashboard (recommended, no Windows task)

If you run the [always-on dashboard](#always-on-web-dashboard-windows), schedule the daily run from the browser. Open **Schedule** (`/schedule`) and set:

- a run time (24-hour, local) and an on/off switch;
- sources, keywords, per-source limits, and the analysis strategy;
- a **Run daily job now** button to trigger it immediately.

The dashboard's in-process scheduler fires the job at most once per day at or after the chosen time (catching up if the dashboard was starting), skips the run if a collection is already active, and persists its settings next to the database in `scheduler.json`. Because it lives inside the always-on dashboard, it needs no admin rights and no separate scheduled task.

### Scheduling with a Windows task (alternative)

```powershell
.\scripts\install_daily_task.ps1 -At "09:00"
```

The task is named `JobSeekerDaily`, runs while the current Windows user is signed in, prevents overlapping runs, and writes output to `logs/daily.log`. It runs below normal CPU priority and records the complete Python/Playwright process tree every 30 seconds in `logs/resources.log`.

```powershell
Start-ScheduledTask -TaskName "JobSeekerDaily"
Get-ScheduledTaskInfo -TaskName "JobSeekerDaily"
Get-Content .\logs\daily.log -Wait
.\scripts\uninstall_daily_task.ps1
```

> Use **either** the dashboard scheduler **or** the Windows task, not both, so the daily run does not fire twice. If you switch to the dashboard scheduler, disable the Windows task: `.\scripts\uninstall_daily_task.ps1` (or `Disable-ScheduledTask -TaskName "JobSeekerDaily"`).

A stale `running` collection run (for example from an interrupted or force-killed run) makes every later run exit with `COLLECTION RUN ACTIVE` (exit code 3). Mark the lingering `collection_runs` row as `failed` and daily runs resume.

## AI Analysis Backends

The shipped example configuration uses the offline `rule` strategy. To use AI-backed extraction and analysis, choose `--analysis-strategy ai`. The backend is selected with the `AI_BACKEND` environment variable (via a local `.env` file, which is excluded from Git):

| `AI_BACKEND` | What it uses | Auth |
| --- | --- | --- |
| `claude_cli` | Local `claude` CLI (Claude Code) | Your Claude subscription login — no per-token API billing |
| `codex_cli` | Local `codex` CLI | Your ChatGPT (Codex) subscription login |
| `openai` | OpenAI API | `OPENAI_API_KEY` |
| `rule` | Offline deterministic scoring against your profile keywords (no AI) | None |
| `demo` | Offline showcase client (rule scoring + a small "AI-assisted" boost) | None |
| _(empty)_ | `openai` if a key is set, otherwise honest `rule` scoring | — |

```text
# Use your Claude subscription instead of an API key:
AI_BACKEND=claude_cli
# Optional: pin a model (leave empty for the CLI default)
CLAUDE_CLI_MODEL=
```

You can also switch the backend at runtime from the web dashboard (**Settings** or **Profile** page) — the selector updates `AI_BACKEND` for the running process, so search, import, and CV→profile all use the new backend immediately.

The CLI backends require the `claude` / `codex` CLI to be installed and already logged in on the machine that runs this tool. They shell out per vacancy, so they are slower than the API but bill against your existing subscription. AI mode still falls back to rule-based scoring if a backend fails, so tests and the sample workflow remain offline. The OpenAI model is controlled via `--openai-model`; CLI-backend models via `CLAUDE_CLI_MODEL` / `CODEX_CLI_MODEL`.

## Rich Terminal UI

Running without arguments opens the Rich terminal UI. It provides:

1. search/import workflow with quick-start and custom presets;
2. source selection, per-source keyword packs, limits, pages, strategy, model, and config persistence;
3. progress output and an in-memory saved log for the last run;
4. saved vacancy tables with scores, fit labels, statuses, and shortened clickable links;
5. vacancy details with latest analysis, status history, and action summaries;
6. direct URL import;
7. explained inbox with shared CLI/TUI/web preferences;
8. Today view with new recommendations and due/overdue reminders;
9. action creation, editing, completion, reopening, and DST-aware due-time prompts;
10. guarded application status changes with corrective-reason prompts when a transition is not normally allowed;
11. build a profile from a CV (menu item `10`, see [above](#build-a-profile-from-your-cv)).

Application statuses are `Saved`, `Applied`, `Interview`, `Offer`, `Rejected`, and `Withdrawn`.

## CLI Workflow

The CLI can run searches, import URLs, inspect stored data, and mutate application state without opening the TUI.

Search-related options include:

```powershell
job-seeker --sources cvbankas,hh,justjoin --keywords "n8n;AI automation" --limit 5 --max-pages 2
job-seeker --source sample --limit 2 --export exports\sample_report.md
job-seeker --import-urls-file imports\vacancy_urls.txt --analysis-strategy rule
```

Direct-URL examples use CVbankas only as a supported-provider format. Replace the URL with a real vacancy URL and make sure its source is enabled.

Database and inbox commands include:

```powershell
job-seeker --list-vacancies
job-seeker --show-vacancy --vacancy-url "https://www.cvbankas.lt/automation-specialist-1234567"
job-seeker --inbox --inbox-min-score 80 --inbox-hide-below-threshold --save-inbox-preferences
job-seeker --inbox --inbox-source sample --inbox-fit High --inbox-status applied --inbox-new-only
job-seeker --clear-inbox-filters --save-inbox-preferences
```

Application tracking commands include:

```powershell
job-seeker --vacancy-url "https://www.cvbankas.lt/automation-specialist-1234567" --status applied --note "Applied with tailored CV"
job-seeker --vacancy-url "https://www.cvbankas.lt/automation-specialist-1234567" --status saved --status-correction-reason "Undo accidental applied mark"
job-seeker --vacancy-url "https://www.cvbankas.lt/automation-specialist-1234567" --list-status-history
job-seeker --list-tracked
```

Normal status transitions are guarded. Corrective jumps, such as moving a rejected application back to saved, require `--status-correction-reason` and are recorded as corrective audit events.

Action and reminder commands include:

```powershell
job-seeker --vacancy-url "https://www.cvbankas.lt/automation-specialist-1234567" --action-title "Follow up" --action-due "2026-08-08T10:00:00" --action-notes "Send short email"
job-seeker --action-id 1 --update-action --action-title "Follow up again" --clear-action-due
job-seeker --action-id 1 --complete-action
job-seeker --action-id 1 --reopen-action
job-seeker --list-actions
job-seeker --today
```

Exit code `0` means useful output or a completed run with report rows, `2` commonly means no matching rows/new vacancies, and `3` means another collection run already owns the same database lease.

## Main Entry Points

| Command | Purpose |
| --- | --- |
| `python main.py` or `job-seeker` | Open the Rich TUI by default. |
| `job-seeker --tui` | Open the Rich TUI explicitly. |
| `job-seeker --web` | Start the local dashboard on `127.0.0.1:8000`. |
| `job-seeker --source sample --limit 2` | Run the offline sample workflow. |
| `job-seeker --sources cvbankas,justjoin --limit 10` | Run selected sources from the CLI. |
| `job-seeker --import-urls "https://www.cvbankas.lt/automation-specialist-1234567"` | Import direct vacancy URLs. |
| `job-seeker --inbox` | Show the explained recommendation inbox. |
| `job-seeker --today` | Show new inbox items plus due/overdue actions. |
| `job-seeker --list-vacancies` | List saved vacancies and latest scores. |
| `job-seeker --list-tracked` | List tracked applications and statuses. |
| `job-seeker --export-tracked exports\tracked.md` | Export tracked applications to Markdown. |
| `job-seeker --daily-run` | Run once and send a Telegram summary of new vacancies. |

Use `job-seeker --help` for the full option list.

## Why It Exists

Automation and applied AI roles are difficult to find with one generic search query. The same type of work may be advertised as workflow automation, RPA, AI integration, internal tools, no-code, low-code, or under tools such as n8n, Make, Zapier, Power Automate, or UiPath.

Job Seeker Intelligence solves this by:

- using shared YAML configuration with source-specific keyword packs;
- collecting vacancies from regional, remote, startup, and direct-URL sources;
- running enabled sources in parallel while keeping requests sequential inside each source;
- preserving source identity while canonicalizing URLs for duplicate detection;
- enriching parser output into one vacancy model before analysis;
- skipping already processed URLs by default, with `--refresh` available when needed;
- ranking vacancies with local rules or optional AI-backed extraction and scoring;
- tracking applications, status history, actions, reminders, and inbox preferences in SQLite;
- exporting self-contained Markdown reports that another AI can re-analyze independently.

## Current Sources

| Source | CLI ID | Collection method | Search behavior |
| --- | --- | --- | --- |
| CVbankas | `cvbankas` | HTML parsing | Lithuanian and English automation/AI keywords |
| HH.ru | `hh` | Playwright browser session | Remote roles outside Russia and Belarus, with throttled requests |
| JustJoin.it | `justjoin` | HTML parsing | English automation, AI, integration, and tooling keywords |
| Startup Jobs | `startup_jobs` | Playwright browser session | Startup-oriented AI and automation searches |
| EU Remote Jobs | `euremotejobs` | Playwright browser session | Remote European roles with throttled requests |
| Sample fixtures | `sample` | Local HTML files | Offline parser/workflow demo |
| Direct URLs | — | Batch URL import | Newline, space, or comma-separated vacancy links |

Job-board markup and access rules change over time. Each source is isolated behind an adapter so a broken provider can produce a partial run instead of stopping the entire application. Keywords, listing pages, vacancy pages, and configured delays remain sequential inside each individual source.

Use `--sources` for the IDs in the table. The legacy `--source` option accepts only `live`, `cvbankas`, or `sample`.

**Browser mode for Cloudflare-fronted sources.** `startup_jobs` and `euremotejobs` sit behind Cloudflare, which returns `403` / a "Just a moment…" JS challenge to the plain HTTP fetcher — the block is by request fingerprint, not by IP. They are configured to fetch through a real headless Chromium (Playwright), which runs the JS challenge and passes. Because that Cloudflare setup challenges *reused* browser contexts, these sources use `browser_fresh_context: true` (a new context per page, so each request looks like a first visit). This works but is slower than plain HTTP, and a site that later enables an interactive CAPTCHA (e.g. Turnstile) could still block a headless browser. Per-source options live under `sources.options.<id>` in the config (`fetch_mode`, `browser_headless`, `browser_fresh_context`, …).

## Application Lifecycle, Inbox, and Runs

Every collection run is stored in `collection_runs` with `running`, `completed`, `partial`, or `failed` status. A per-database lease prevents overlapping runs against the same SQLite file. Each observed vacancy records first-seen and last-seen timestamps/run IDs, and URL canonicalization removes tracking parameters such as `utm_*` for duplicate identity. Original URLs are retained through alias data when canonicalization or legacy migration needs it.

The explained inbox combines vacancy data, latest analysis, application status, run membership, and shared preferences. Preferences can filter by minimum score, hide/show below threshold, sort by score/newest/title/company, source, fit label, status, new-only, and current-run-only. **The same preference row is used by CLI, TUI, and web** — this is what the **Shared inbox and reminder settings** on the Settings page controls, so a change made anywhere applies everywhere.

Partial runs are visible in CLI/TUI/web so a user can tell that recommendations came from an incomplete collection.

## Actions, Reminders, Timezones, and DST

Actions are local follow-up tasks attached to vacancies. They have a title, notes, optional due time, `open`/`completed` state, completion timestamp, and UTC persistence boundary.

User-facing due times are entered as local ISO datetimes, for example `2026-08-08T17:30:00`. The application stores them as normalized UTC `Z` instants and renders them back through the configured or detected IANA timezone. First-run timezone discovery is display-only until the user confirms/saves it; on Windows, known system timezone IDs are mapped to IANA names such as `Europe/Vilnius` when offsets match.

DST edge cases are handled explicitly:

- nonexistent local times during spring-forward transitions are rejected;
- ambiguous fall-back times require `--action-fold 0` or `--action-fold 1` in the CLI;
- the TUI prompts for earlier/later fold selection when needed;
- web action forms accept a fold value when supplied.

The Today views show new recommended vacancies plus due or overdue open actions.

## Architecture

```mermaid
flowchart LR
    UI["Rich TUI"] --> CFG["YAML configuration"]
    CLI["CLI"] --> CFG
    WEB["Loopback FastAPI dashboard"] --> DB[("SQLite")]
    CFG --> SRC["Source adapters"]
    URLS["Direct URL import"] --> SRC
    SRC --> NORM["Normalized Vacancy model"]
    NORM --> EXT["Parser + optional AI extraction"]
    EXT --> SCORE["Rule-based or AI analysis"]
    SCORE --> DB
    DB --> RUNS["Collection runs and inbox"]
    DB --> TRACK["Applications, status events, actions"]
    DB --> REPORT["Markdown reports"]
    REPORT --> REAI["Independent AI re-analysis"]
```

Main ownership boundaries:

- `src/cvbankas_tracker/sources/`: provider-specific listing discovery and page loading (`browser_fetch.py` provides the shared Playwright fetcher for Cloudflare-fronted sources);
- `parser.py` and `extraction.py`: HTML parsing and structured vacancy enrichment;
- `analysis.py`: deterministic and AI-backed scoring strategies;
- `ai_cli.py`: shared `claude`/`codex` subscription-CLI runners;
- `profile_builder.py`: CV text extraction and AI-built profile generation;
- `storage.py`: SQLite bootstrap, migrations, WAL mode, canonical URLs, runs, inbox, applications, events, and actions;
- `tracking.py`: status transition rules, corrective updates, timezone discovery, and reminder time conversion;
- `tui.py`: interactive Rich workflow and shared preference/action/status operations;
- `web.py`, `web_jobs.py`, `web_scheduler.py`: loopback FastAPI dashboard, background job runner, and in-process daily scheduler, with CSRF/Host/Origin guards;
- `io_utils.py`: human-readable and AI-ready Markdown exports;
- `telegram.py` and `scripts/`: notification path, dashboard launcher, and Windows scheduled-task support.

## Data and Bootstrap Behavior

Local runtime files are intentionally not committed:

- `job_seeker.db` and its backups/journals;
- `.env`;
- `.browser_profiles/`;
- `config/scheduler.json`;
- generated files under `exports/`;
- local imports under `imports/` unless intentionally added.

The database is created automatically on first launch. Relative database paths from YAML config are anchored to the config file directory; relative paths without a config are anchored to the project entrypoint directory. The supplied example config declares `db: job_seeker.db`, so an automatic-config launch uses `config/job_seeker.db`; pass `--db job_seeker.db` to use a project-root database instead.

On startup, the CLI, TUI, and local web dashboard bootstrap the SQLite schema, enable WAL mode, serialize migrations with a small lock file, and run integrity/foreign-key checks. If an existing database needs a schema migration, a timestamped backup is created next to the database before migration, for example `job_seeker.db.20260808T120000Z.bak`. If integrity or foreign-key checks fail, bootstrap stops before serving or mutating data; restore from a backup or repair the database and retry.

Generated vacancy reports contain:

- source, company, location, salary, and exact vacancy URL;
- extracted requirements and responsibilities;
- preliminary software analysis;
- application status when tracked;
- a prompt instructing an external AI to ignore the preliminary score and rank vacancies independently.

## Testing and CI

Run the test suite locally:

```powershell
python -m unittest discover -s tests -v
```

To reproduce CI's isolated dependency check, run:

```powershell
python scripts\verify_clean_deps.py --install
```

The tests cover parsers, source adapters (including browser-mode wiring), storage bootstrap and migrations, canonical URL behavior, collection-run lifecycle and leases, inbox preferences, status-transition audit events, action/reminder timezone and DST behavior, CLI database commands, TUI adapters, web dashboard security/rendering/scheduler, subscription-CLI runners, CV profile building, Telegram formatting, dependency pins, and end-to-end sample workflows.

GitHub Actions runs on pushes and pull requests with Python 3.12. The CI workflow verifies clean dependency resolution via `scripts/verify_clean_deps.py --install`, installs the project editable, and runs `python -m unittest discover -s tests -v` with `OPENAI_API_KEY` empty so offline behavior remains tested.

## Known Constraints

- Public job-board HTML and anti-bot behavior can change without notice; browser-mode sources can break if a board adds an interactive CAPTCHA.
- Browser collection (HH, Startup Jobs, EU Remote Jobs) is deliberately slow to reduce request pressure and requires Playwright/Chromium.
- Search limits apply per source, so a run may stop before exhausting every configured keyword.
- Some boards expose incomplete company, salary, location, or description data.
- The SQLite database is local and designed for a single user, not concurrent multi-user web access.
- The web dashboard is loopback-only and has no login system.
- AI analysis is optional, network/API-key or CLI-login dependent, and not required for tests.
- No automated submit-application flow is implemented; tracking records what the user does elsewhere.

## Realistic Roadmap

- source health metrics and clearer run-history summaries in the UI;
- stronger cross-source duplicate detection beyond URL canonicalization;
- richer saved searches and table filters in TUI/web;
- full-database HTML/Markdown export from the TUI;
- more robust import validation and per-source diagnostics;
- packaged Windows release with documented setup;
- optional external calendar/email integrations for reminders, after the local workflow remains stable.
