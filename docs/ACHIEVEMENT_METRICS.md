# Achievement, Product-Metrics, and Engineering-Metrics Audit

Audit date: 2026-09-13  
Branch and revision: `codex/achievement-metrics-audit` at `929911a`, with the working-tree changes listed below  
Machine-readable evidence: `docs/achievement_metrics_results.json`  
Primary local dataset: ignored `config/job_seeker.db`  
Benchmark case SHA-256: `fc74cc9492d147bbeab85648a123ef793ac634422fa7e66163019d02d3496e81`

This report measures the current working tree. It does not treat README prose as evidence and does not present synthetic capacity as real usage. Local database/log facts are not Git-portable. Performance is machine-specific.

Evidence classes:

- **Measured**: command, test, read-only database query, log parse, or operating-system query run during this audit.
- **Derived**: an exact calculation from measured repository/runtime facts.
- **Benchmark**: a new reproducible synthetic or temporary-database experiment.
- **Historical**: ignored local database/log/task history.
- **Not established**: the repository lacks a defensible denominator, label set, timestamp, or observation.

# System Map

```text
7 external boards + direct URLs + offline sample
  -> HTTP / embedded JSON / Playwright retrieval
  -> one Vacancy model
  -> canonical URL identity and duplicate checks
  -> optional AI field enrichment
  -> AI fit evaluation or deterministic fallback
  -> transactional SQLite persistence
  -> CLI / Rich TUI / FastAPI dashboard / reports / Telegram
```

The production path has **7 practical automated stages**: retrieval; parse/normalize; canonical identity/deduplication; optional enrichment; fit evaluation; persistence; and presentation/notification. Sources execute concurrently across adapters and sequentially within each source. Source and vacancy exceptions are retained as partial-run evidence rather than discarding unaffected work.

Once configuration and credentials exist, a scheduled run needs no human action during those seven stages. Human work remains for initial configuration, reviewing results, and applying to jobs. The current Windows task state does not prove reliable unattended operation; see Reliability.

# Master Metrics Table

| Metric | Result | Category | Evidence | Reproduction command | Confidence | CV value |
|---|---:|---|---|---|---|---|
| External job-board integrations | 7 | Integration | Derived from unique registry adapter instances; aliases/sample excluded | `python scripts/achievement_metrics.py --skip-performance` | High | Exceptional |
| Enabled external sources in primary local config | 6 | Product | Measured from `config/cvbankas.local.yaml` | `python scripts/achievement_metrics.py --skip-performance` | High locally | Strong |
| External retrieval types | 3 browser; 4 non-browser HTTP/feed; 0 other | Integration | Derived from adapters plus current fetch-mode config | `python scripts/achievement_metrics.py --skip-performance` | High locally | Strong |
| Normalized schemas | 1 `Vacancy` model for all 8 adapters | Engineering | Direct source contract | `rg -n "class Vacancy|class VacancySource" src/cvbankas_tracker` | High | Strong |
| External service integrations | 11: 7 boards, 3 model providers, Telegram | Integration | Derived from code paths | `python scripts/achievement_metrics.py --skip-performance` | High | Strong |
| User-visible dashboard search latency | p50 1.162 s; p95 2.698 s; max 3.497 s across 60 observations | Performance | New offline web-flow benchmark | `python scripts/achievement_metrics.py` | High for this machine/workload | Strong |
| Complex cold-query latency | p50 1.227 s; p95 1.700 s; max 1.810 s across 5 runs, 80 raw/66 usable rows | Performance | New dashboard-to-render benchmark | `python scripts/achievement_metrics.py` | Medium | Strong |
| Representative end-to-end funnel | 80 raw -> 66 normalized -> 66 unique -> 66 eligible -> 37 relevant -> 37 shortlisted | Product | New offline complex-query benchmark, repeated 5 times | `python scripts/achievement_metrics.py` | Medium | Strong |
| Representative end-to-end time | 1.227 s median; 65.2 raw or 53.8 unique listings/s | Performance | Derived from the same five cold complex runs | `python scripts/achievement_metrics.py` | Medium | Strong |
| Representative manual-review reduction | 53.75% raw-to-shortlist; 2.16:1 compression | Product | 80 raw to 37 score-45+ saved results | `python scripts/achievement_metrics.py` | Medium | Strong |
| Active local corpus | 2,449 URL-unique vacancies across all 7 external sources | Scale | Historical read-only DB snapshot | `python scripts/achievement_metrics.py --skip-performance` | High locally | Strong |
| Active Medium+ review snapshot | 304/2,449; 87.59% reduction; 8.06:1 compression | Product | Historical latest stored scores, threshold >=45 | `python scripts/achievement_metrics.py --skip-performance` | High counts; Medium interpretation | Strong |
| Historical observation consolidation | 2,675 observations -> 2,449 rows; 226 repeats; 8.45% reduction | Scale | Historical DB | `python scripts/achievement_metrics.py --skip-performance` | High locally | Useful |
| Data completeness | 100% title/company/source/URL; 77.99% location; 84.12% salary | Product | Historical DB, denominator 2,449 | `python scripts/achievement_metrics.py --skip-performance` | High locally | Strong |
| Automated tests | 364/364 passed in 58.151 s, 31 files | Engineering | Measured under Coverage.py | `python -m coverage run --data-file=tmp/.coverage.achievement_audit --source=src/cvbankas_tracker -m unittest discover -s tests -p 'test_*.py'` | High | Strong supporting evidence |
| Production statement coverage | 77%; 6,883 statements, 1,584 missed | Engineering | Coverage.py | `python -m coverage report --data-file=tmp/.coverage.achievement_audit --include='src/cvbankas_tracker/*'` | High | Useful |
| Critical-core coverage | 99% aggregate; AI CLI 100%, analysis 99%, storage 99% | Engineering | 1,312 statements, 12 missed | `python -m coverage report --data-file=tmp/.coverage.achievement_audit --include='src/cvbankas_tracker/*'` | High | Strong |
| Source adapters exercised by tests | 8/8 including sample | Engineering | Source tests plus workflow/demo tests | `python -m unittest discover -s tests -p 'test_*.py'` | High | Strong |
| Single-source failure containment | 7/7 positions; all 6 unaffected results preserved in every case | Reliability | New controlled fault injection | `python scripts/achievement_metrics.py --skip-performance` | High | Exceptional |
| Multi-source failure containment | 4/4 patterns, including all-seven failure | Reliability | New controlled fault injection | `python scripts/achievement_metrics.py --skip-performance` | High | Strong |
| AI provider/output fault containment | 8/8 valid stored analyses; 6 fallbacks; 0 invalid rows | Reliability | New controlled fault injection | `python scripts/achievement_metrics.py --skip-performance` | High | Exceptional |
| Concurrent canonical identity | 16/16 writes, 8 workers -> 1 vacancy/application/analysis/event; integrity `ok`; 0 FK errors | Reliability | New temporary-DB benchmark | `python scripts/achievement_metrics.py --skip-performance` | High | Exceptional |
| Synthetic canonicalization throughput | 115,938 URLs/s median over 7 x 50,000 | Performance | New local benchmark | `python scripts/achievement_metrics.py` | High locally | Strong with context |
| Synthetic deterministic scoring throughput | 25,288 vacancies/s median over 7 x 20,000 | Performance | New local benchmark on current audit-influenced working tree | `python scripts/achievement_metrics.py` | Medium | Useful |
| Rule analysis plus atomic persistence | 67.79 vacancies/s; 500 rows in 7.375 s median over 3 DBs | Performance | New production-style transaction benchmark | `python scripts/achievement_metrics.py` | High locally | Useful |
| Joined inbox query | 11.959 ms median for 500 rows; 75 timed queries after warmups | Performance | New temporary-DB benchmark | `python scripts/achievement_metrics.py` | High locally | Strong |
| Canonical URL dedup quality | precision 100% (7/7), recall 70% (7/10), F1 82.35% | AI Quality | 20-record synthetic benchmark | `python scripts/achievement_metrics.py --skip-performance` | Medium | Strong only with recall stated |
| Development relevance regression | 20/20, precision/recall/F1 100% | AI Quality | Author-labeled set used to tune rules | `python scripts/achievement_metrics.py --skip-performance` | Low externally | Do not use |
| Development work-mode regression | 18/18 exact | AI Quality | Author-labeled set used to tune rules | `python scripts/achievement_metrics.py --skip-performance` | Low externally | Do not use |
| Development eligibility regression | 12/12, including 2/2 country-only rejections | AI Quality | Author-labeled set used to tune rules | `python scripts/achievement_metrics.py --skip-performance` | Low externally | Do not use |
| Development seniority regression | 18/18 across six labels | AI Quality | Author-labeled set used to tune rules | `python scripts/achievement_metrics.py --skip-performance` | Low externally | Do not use |
| Log-backed product history | 59 completion summaries; 27,518 attempted details; 3,829 report rows; 4,705 failures | Scale | Historical ignored logs, deduplicated by timestamp+DB | `python scripts/achievement_metrics.py --skip-performance` | Medium | Useful with caveat |
| Recorded AI fallback events | 285 log occurrences | Reliability | Historical local logs | `python scripts/achievement_metrics.py --skip-performance` | Medium locally | Strong interview evidence |
| CV-Online cached detail reuse | 3,726 item attempts from 50 listing pages used embedded payload cache | Performance | Code path plus historical run summaries | `python scripts/achievement_metrics.py --skip-performance` | High | Exceptional |
| Terminal run outcomes | 15 completed, 8 partial, 14 failed; 1 currently running | Reliability | 38 DB run rows; 37 terminal | `python scripts/achievement_metrics.py --skip-performance` | High locally | Do not use as success claim |
| Longest completed-only streak | 12 runs | Reliability | Historical DB in run-ID order | `python scripts/achievement_metrics.py --skip-performance` | High locally | Useful |
| Installed Windows daily task | Installed but disabled; last result 3; dashboard task absent | Reliability | Read-only `Get-ScheduledTask` probe | `Get-ScheduledTask -TaskName 'JobSeekerDaily','JobSeekerDashboard' -ErrorAction SilentlyContinue` | High locally | Do not use |

# Engineering Metrics

- **Architecture:** 8 adapter objects (7 external plus sample) share one protocol and one normalized domain model. Four registry aliases are excluded from the source count.
- **Interfaces:** one argparse entry point with 65 options, a Rich TUI, and a FastAPI dashboard with 32 routes (14 GET, 18 POST) and 10 rendered workflows.
- **Background execution:** 3 job kinds (search, import, daily), one-active-job enforcement, cooperative pause/resume/cancel, 256,000-character in-memory log cap, 20 completed jobs retained, and 50 durable log files retained.
- **Persistence:** 11 domain tables and 13 explicit indexes; WAL, foreign keys, integrity checks, backup-before-migration, transaction-scoped writes, and a per-database collection lease.
- **AI boundary:** 3 providers (OpenAI API, Claude CLI, Codex CLI), 3 model-backed operations, defensive normalization, and deterministic vacancy-analysis fallback.
- **Security boundaries:** loopback-only bind, Host/Origin/CSRF checks, safe external URL schemes, 5 MiB CV upload cap, 24,000-character prompt cap, contained profile paths, explicit subscription-CLI no-tool/read-only flags, and bounded request timeouts.
- **Verification:** 364 current tests versus 334 at committed `HEAD`; 30 tests are working-tree additions from the audit/hardening work and must not be described as pre-audit coverage.

The suite has no formal unit/integration/e2e markers. Any exact split would be an audit-invented taxonomy, so only the total is reported. Cross-boundary coverage does exist through temporary SQLite databases, FastAPI TestClient, subprocess CLI tests, background threads, migrations, and mocked network/provider boundaries.

Historical active-database row inventory:

| Table | Rows | Table | Rows |
|---|---:|---|---:|
| vacancies | 2,449 | analyses | 2,496 |
| collection_run_observations | 2,675 | collection_runs | 38 |
| applications | 782 | application_status_events | 783 |
| telegram_summary_outbox | 14 | settings | 3 |
| collection_run_leases | 1 | action_items | 0 |
| vacancy_url_aliases | 0 |  |  |

The sample integration contains two bundled vacancy-detail fixtures. There are no versioned migration files or schema-version rows; migration behavior is embedded in the self-migrating bootstrap, so an exact migration count is not meaningful.

# Product Metrics

| Product capability | Quantitative metric possible? | Current value | How verified | CV value |
|---|---|---:|---|---|
| Multi-source discovery | Yes | 7 external boards | Registry object identity and source implementations | Exceptional |
| Unified normalization | Yes | 7 external sources -> 1 model | Source protocol and parser return types | Strong |
| Dashboard search | Yes, offline | 60 searches; p50 1.162 s, p95 2.698 s | POST-to-render benchmark | Strong |
| AI-assisted search latency | Not currently | External model/network latency not benchmarked | Credentials and stable live corpus absent | Do not claim |
| Fit filtering/ranking | Snapshot only | 2,449 -> 304 Medium+ | Latest stored score >=45 | Strong with caveat |
| Deduplication | Yes, synthetic | 100% precision, 70% recall, 82.35% F1 | Frozen 20-record URL set | Strong with full metrics |
| Automatic ingestion | Structurally yes | 7 configured scheduler sources | Scheduler JSON and code | Useful; runtime state weak |
| Background workflows | Yes | search, import, daily | Job-manager call sites | Useful |
| Dashboard workflows | Yes | 10 rendered workflows | Jinja template inventory | Useful |
| Saved state/history | Yes | 2,449 vacancies, 2,496 analyses, 782 application projections, 38 runs | Local DB | Strong locally |
| Browser automation | Yes | 3 configured external sources | Source options | Strong |
| Direct URL ingestion | Yes | 1 separate path across enabled adapters | `run_import` and route/CLI | Useful |
| Notifications | Yes | 1 Telegram integration, delivery retry/outbox | Code and tests | Useful |
| Failure-degraded results | Yes | 11/11 source fault patterns contained | Fault injection | Exceptional |
| Model failure fallback | Yes | 8/8 conditions contained | Fault injection | Exceptional |
| Unattended completion | Implementation only | 7 automated stages, 0 in-run manual actions after setup | Pipeline trace | Useful; do not claim reliability |
| Field completeness | Yes | 100% titles/companies; 77.99% locations; 84.12% salary | Local DB | Strong |
| Freshness/discovery speed | No | Not established | Publication and ingestion timestamps are insufficient | Do not claim |

# Product Funnel

## Representative end-to-end benchmark

Five cold complex-query runs used the real FastAPI search route, background job manager, production collection loop, real bundled HTML parsers, rule scorer, transactional SQLite layer, and rendered `/vacancies` page. A synthetic source supplied deterministic data and made no network/model calls.

```text
80 raw source URLs
-> 66 parsed/normalized listings
-> 66 canonical identities
-> 66 compatible with the active profile's work-mode rules
-> 37 score >=45 (Medium+)
-> 37 auto-saved shortlist rows
```

- Raw-to-shortlist reduction: **43/80 = 53.75%**.
- Raw-to-shortlist compression: **2.16:1**.
- Unique-to-shortlist reduction: **29/66 = 43.94%**.
- Unique-to-shortlist compression: **1.78:1**.
- Median total time: **1.227 s**.
- Median throughput: **65.2 raw listings/s** or **53.8 normalized unique listings/s**.

This is a synthetic, two-fixture offline workload. It proves the application path and bounded local performance, not live-board diversity or external-AI speed.

## Historical current-corpus funnel

```text
2,675 run observations
-> 2,449 canonical URL identities with latest analyses
-> eligibility: not stored as a historical stage, therefore unknown
-> 304 score >=45 (Medium+)
-> 20 score >=75 (High)
```

The Medium+ threshold gives an **87.59% reduction** and **8.06:1 compression**. This is a workload snapshot, not independent proof that all 2,145 removed listings are irrelevant. The 782 application rows cannot be used as a shortlist count because auto-save settings varied historically.

# Performance Benchmarks

Environment: Python 3.12.2; Windows 11 build 26200; Intel64 Family 6 Model 154 Stepping 3. Values are local medians/distributions, not cross-machine guarantees.

## User-perceived search

Timing begins immediately before `POST /search/start`, includes the production background job and repeated `/jobs/{id}/log` polling, and ends after `/vacancies` returns rendered HTML.

| Query class | Keywords | Cold result rows | Cold p50 | Cold p95 | Cold max | Warm p50 | Warm p95 | Warm max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Simple | 1 | 10 | 0.200 s | 0.210 s | 0.220 s | 0.200 s | 0.220 s | 0.220 s |
| Normal | 2 | 18 | 0.350 s | 0.650 s | 0.720 s | 0.340 s | 0.450 s | 0.480 s |
| Multi-constraint | 4 | 34 | 0.650 s | 2.090 s | 2.440 s | 0.670 s | 1.290 s | 1.430 s |
| Complex | 8 | 66 | 1.227 s | 1.700 s | 1.810 s | 1.160 s | 1.960 s | 2.160 s |
| Adversarial | 12 | 98 | 2.037 s | 2.514 s | 2.618 s | 2.598 s | 3.354 s | 3.497 s |
| Profile-driven | 12 | 98 | 1.815 s | 2.535 s | 2.695 s | 1.798 s | 2.687 s | 2.755 s |

Across all **60 observations**, p50 was **1.162 s**, p95 **2.698 s**, and maximum **3.497 s**. Across 30 cold runs, p50 was **1.216 s** and p95 **2.540 s**. Across 30 warm runs, p50 was **1.161 s** and p95 **2.772 s**. Warm is not uniformly faster because it still collects/polls/renders and Windows scheduling/I/O variance dominates some runs.

There are only five cold and five warm repetitions per class, so per-class p95 is descriptive and p99 is not statistically meaningful. The claim “AI-assisted search under 2 seconds even for complex queries” is **not defensible**: model latency was not measured, and adversarial/profile-driven local p95 exceeded two seconds even though this complex-class p95 was 1.700 seconds.

## End-to-end stage timing

Median inclusive stage time across the five cold complex runs:

| Stage | Median |
|---|---:|
| Source collection + fixture fetch | 0.0012 s |
| Parse and normalize | 0.0088 s |
| Deterministic analysis | 0.0057 s |
| Atomic SQLite persistence | 0.7714 s |
| Final inbox query + HTML render | 0.0149 s |
| Total POST-to-render | 1.2271 s |

The unallocated remainder is background-thread startup, collection orchestration, duplicate checks, report generation, HTTP polling, and timing overhead.

## Local component benchmarks

| Workflow | Dataset and runs | Result |
|---|---|---:|
| Canonicalize + set-deduplicate | 7 x 50,000 URLs -> 5,000 identities | 0.4313 s median; 115,938 URLs/s |
| Deterministic scoring | 7 x 20,000 vacancies | 0.7909 s median; 25,288/s |
| Rule analysis + atomic persistence | 3 fresh DBs x 500 rows | 7.3755 s median; 67.79/s |
| Joined inbox query | 3 DBs x 25 timed queries after 5 warmups | 11.959 ms median for 500 rows |

The deterministic scoring implementation in the starting working tree already contained an audit-informed fast path, so that throughput is a current-state benchmark, not an uncontaminated before/after achievement.

# Reliability / Failure Injection

## Source isolation

- **7/7** possible single-source failure positions returned `partial`, recorded the fault, and preserved all six unaffected results.
- **4/4** multi-source patterns behaved as specified: two early failures, two late failures, four alternating failures, and all seven failures. The all-failed case returned `failed`; every degraded case preserved all survivors.
- Historical run 26 retained 1,502 report rows while one source produced 2,402 item failures. This corroborates partial-progress retention but is not a desirable success rate.

## AI analysis boundary

Eight conditions were injected: unavailable provider, timeout, malformed-JSON exception, empty response, non-object response, schema-invalid values, extreme values, and rate-limit-like failure.

- **8/8** produced a valid persisted `VacancyAnalysis`.
- **6/8** invoked deterministic fallback.
- **2/8** were normalized into bounded valid AI results.
- **0/8** stored an invalid score or fit label.

This covers vacancy fit analysis only. AI enrichment and CV profile building do not share the same fallback guarantee.

## Database idempotency/concurrency

Sixteen tracking-parameter variants of one URL were written through eight workers:

- 16/16 writes completed;
- 1 canonical vacancy, 1 application, 1 exact analysis, and 1 initial status event remained;
- `PRAGMA integrity_check = ok` and foreign-key violations = 0.

This is a thread-level temporary-DB result, not a multi-process linearizability proof. Analysis still occurs before persistence.

## Scheduler and unattended history

- Local in-process scheduler configuration: enabled, seven sources, 19:00, last persisted status `running` from 2026-09-11. That stale-looking state is not proof of a live successful scheduler.
- Windows `JobSeekerDaily`: installed but **Disabled**, last result **3**, last run 2026-08-14 22:28:26; `JobSeekerDashboard` was absent.
- DB outcomes: **15 completed + 8 partial + 14 failed + 1 running**. Among 37 terminal runs, 23 were completed/partial (**62.16%**) and 15 completed (**40.54%**). These figures should not be marketed as reliability.
- Longest completed-only streak: 12 runs; longest completed-or-partial streak: 14.
- Logs: 59 deduplicated completion summaries, 27,518 attempted details, 3,829 report rows, 4,705 recorded failures, and 285 explicit AI-fallback events.

Logs do not contain a complete scheduled-start denominator over a guaranteed retention interval, so no scheduled success rate, uptime, or days-unattended claim is established.

# Evaluation Quality

## Deduplication

The frozen set has 20 URLs, 11 expected groups, and 10 expected duplicate pairs. It includes exact duplicates, tracking variants, query ordering, fragments, trailing slashes, cross-source equivalents, metadata variants, and similar-but-distinct jobs.

- Predicted identities: 14.
- True detected pairs: 7/10.
- Incorrect merges: 0.
- Missed pairs: 3.
- Precision: 7/7 = **100%**.
- Recall: 7/10 = **70%**.
- F1: **82.35%**.
- Raw URL reduction: **30%**.

The misses are the architectural boundary: canonical URL identity does not attempt semantic cross-board entity resolution.

## Relevance, work mode, eligibility, and seniority

| Evaluation | Cases | Result | FP | FN | Status |
|---|---:|---:|---:|---:|---|
| Relevance at score >=45 | 20 (10 positive, 10 negative) | 100% accuracy/precision/recall/F1 | 0 | 0 | Development regression; do not publish |
| Work mode | 18 | 18/18 exact | n/a | n/a | Development regression; do not publish |
| Eligibility | 12 (6 positive, 6 negative) | 12/12; 2/2 country-only rejections | 0 | 0 | Development regression; do not publish |
| Seniority | 18 across six labels | 18/18 exact | n/a | n/a | Development regression; do not publish |

Expected outcomes were written before the first evaluation, but the cases were authored after implementation inspection and were then used to harden the rules. They are not held out. There is one labeler and no agreement statistic. A private-label evaluation protocol and candidate-set builder now exist, but no independent labels were available; held-out relevance, work-mode, eligibility, and seniority quality are therefore **not established**.

# Source Coverage

There are exactly **7 external job-board integrations**, **6 enabled in the primary local source config**, **7 listed in the in-process scheduler config**, and **8 adapters when the offline sample is included**. “Enabled” is a configuration fact, not proof that every source is live-operational at this moment.

| Source | Type | Enabled | Retrieval method | Validation method | Active URL identities |
|---|---|---:|---|---|---:|
| CVbankas | Non-browser HTTP/feed | Yes | HTTP HTML detail parsing | source/parser/collector tests + DB history | 309 |
| CVMarket | Non-browser HTTP/feed | Yes | HTTP HTML + JSON-LD | 6 source tests + 44 DB rows | 44 |
| CV-Online | Non-browser HTTP/feed | No | embedded public JSON full feed + payload cache | 5 source tests + run history | 1,292 |
| HH.ru | Browser-configured | Yes | Playwright; HTTP fallback | 12 source tests + browser wiring + history | 262 |
| JustJoin.it | Non-browser HTTP/feed | Yes | HTTP HTML | 16 shared additional-source tests + history | 312 |
| Startup Jobs | Browser-configured | Yes | Playwright; HTTP fallback | additional-source/browser tests + history | 145 |
| EU Remote Jobs | Browser-configured | Yes | Playwright; HTTP fallback | additional-source/browser tests + history | 85 |
| Sample | Other/local | No | bundled HTML fixtures | demo/workflow/parser tests | 0 |

External type totals: **3 browser-configured, 4 non-browser HTTP/feed, 0 other**. If sample is included, “other/local” becomes 1.

All seven external sources contribute active DB rows. URL-identity shares of the 2,449-row local corpus are: CV-Online 52.76%, JustJoin.it 12.74%, CVbankas 12.62%, HH.ru 10.70%, Startup Jobs 5.92%, EU Remote Jobs 3.47%, CVMarket 1.80%.

Historical source-summary evidence:

| Source | Run entries | Entries with zero recorded failures | Attempted | Observed | Failed | Report rows |
|---|---:|---:|---:|---:|---:|---:|
| CVbankas | 27 | 23 | 885 | 734 | 151 | 666 |
| CVMarket | 4 | 0 | 1,453 | 0 | 1,453 | 0 |
| CV-Online | 14 | 13 | 3,726 | 1,324 | 2,402 | 1,324 |
| EU Remote Jobs | 13 | 10 | 33 | 30 | 3 | 30 |
| HH.ru | 22 | 11 | 110 | 109 | 11 | 56 |
| JustJoin.it | 23 | 17 | 786 | 700 | 87 | 642 |
| Startup Jobs | 13 | 9 | 419 | 419 | 4 | 419 |

These fields do not support a universal success-rate calculation: listing-stage failures can increment `failed` without entering `attempted`, while `observed` describes successful item processing. CVMarket's 44 active rows came through history not represented as successful source-summary entries. Per-source latency was not recorded. The in-process scheduler configuration names all seven sources, but the installed Windows daily task is disabled.

Exact normalized company/title/location grouping found no cross-source groups. This does not establish zero overlap; formatting differences make that test too strict. Semantic unique contribution per source is not established.

# Data Completeness

Denominator: 2,449 active vacancy rows.

| Field | Non-empty | Completeness |
|---|---:|---:|
| source name, source ID, URL, title, company | 2,449 each | 100% each |
| location | 1,910 | 77.99% |
| salary text | 2,060 | 84.12% |
| raw text | 2,262 | 92.36% |
| requirements | 2,176 | 88.85% |
| responsibilities | 2,252 | 91.96% |

Publication date, normalized work model, normalized seniority, and persisted eligibility are not columns in the current vacancy schema, so completeness for those fields cannot be calculated.

# Top Engineering Achievements

## 1. Seven-source failure isolation

- **Raw result:** 7/7 single-source positions and 4/4 multi-source patterns contained; every unaffected result preserved.
- **Why it matters:** One board cannot erase usable work from the rest of a collection.
- **Conservative CV wording:** Built a seven-source ingestion pipeline with per-source failure isolation validated at every single-source position.
- **Strong CV wording:** Engineered a parallel seven-source pipeline that preserved 100% of unaffected results in 11/11 controlled fault scenarios.
- **README wording:** Source workers convert adapter faults into partial-run evidence while retaining all successful source results.
- **Interview evidence:** Explain `ThreadPoolExecutor`, ordered future collection, `SourceBatchResult`, and partial/failed state rules.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 2. Defensive AI failure containment

- **Raw result:** 8/8 provider/output faults yielded valid stored analyses; six fallback paths; zero invalid rows.
- **Why it matters:** Model failure does not corrupt the user-visible recommendation state.
- **Conservative CV wording:** Added defensive model-output validation and deterministic fallback verified across eight fault modes.
- **Strong CV wording:** Contained 8/8 simulated AI provider/output failures with zero invalid analysis rows persisted.
- **README wording:** Vacancy analysis normalizes hostile output and falls back to deterministic scoring on provider/build failures.
- **Interview evidence:** Explain provider parsing, result normalization, build-time validation, fallback observability, and excluded AI operations.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 3. Concurrent canonical identity

- **Raw result:** 16/16 writes through eight workers collapsed to one vacancy/application/analysis/event; integrity `ok`, zero FK errors.
- **Why it matters:** Duplicate concurrent observations do not fragment core identity.
- **Conservative CV wording:** Preserved one canonical vacancy/application identity under an eight-worker duplicate-write test.
- **Strong CV wording:** Collapsed 16/16 concurrent tracking-URL writes to one integrity-clean SQLite identity.
- **README wording:** Canonical URL keys, transactions, and exact-analysis reuse keep concurrent retries idempotent.
- **Interview evidence:** Explain URL canonicalization, `ON CONFLICT`, WAL, path locks, exact payload reuse, and limits of thread-only testing.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 4. Critical-core verification

- **Raw result:** 364/364 tests passed; 77% production coverage; 99% aggregate coverage for AI CLI, analysis, and storage.
- **Why it matters:** The highest-risk model and persistence boundaries have unusually dense executable verification.
- **Conservative CV wording:** Maintained 364 passing tests with 99% coverage across the critical AI-analysis and persistence core.
- **Strong CV wording:** Built a 364-test suite covering 99% of critical AI/persistence statements and all eight source adapters.
- **README wording:** Coverage.py reports 77% overall and 99% across AI CLI, analysis, and storage on 364 passing tests.
- **Interview evidence:** Explain why there is no honest exact unit/integration/e2e split and disclose 30 working-tree audit additions.
- **Reproduction:** coverage commands below.

## 5. Transactional migration/integrity boundary

- **Raw result:** 11 tables, 13 indexes, WAL, FK/integrity checks, backup-before-migration, and migration/idempotency tests passing.
- **Why it matters:** The local data store detects corruption and protects upgrades before serving state.
- **Conservative CV wording:** Implemented a self-migrating SQLite store with WAL, backup, integrity, foreign-key, and lease safeguards.
- **Strong CV wording:** Hardened an 11-table SQLite state layer with serialized backed-up migrations and startup integrity enforcement.
- **README wording:** Bootstrap serializes migrations, backs up before schema changes, then rejects failed integrity/FK checks.
- **Interview evidence:** Explain bootstrap locking, backup timing, migration tests, collection leases, and lack of versioned migration files.
- **Reproduction:** full tests plus `python scripts/achievement_metrics.py --skip-performance`

## 6. Local web security enforcement

- **Raw result:** loopback bind + Host + Origin + CSRF + scheme/path/upload/prompt boundaries, with adversarial tests.
- **Why it matters:** A local dashboard still treats browser and file inputs as untrusted.
- **Conservative CV wording:** Enforced loopback, request-origin, CSRF, file-path, and upload boundaries on a local FastAPI dashboard.
- **Strong CV wording:** Built a defense-in-depth local web boundary with tested Host/Origin/CSRF and file/input containment.
- **README wording:** Every mutation crosses loopback/Host/Origin/CSRF checks; uploads and profile paths are bounded.
- **Interview evidence:** Explain threat assumptions, multipart CSRF, path traversal, URL schemes, and why this is not a security audit certification.
- **Reproduction:** `python -m unittest tests.test_g005_web_dashboard tests.test_web_frontend tests.test_lifecycle_hardening`

## 7. Bounded background execution

- **Raw result:** 3 job kinds, one-active-job guard, pause/resume/cancel, 256k memory-log cap, 20 job and 50 file retention bounds.
- **Why it matters:** Long-running local collection remains controllable and cannot grow logs indefinitely.
- **Conservative CV wording:** Added bounded background search/import/daily jobs with cooperative control and durable log retention.
- **Strong CV wording:** Implemented three controllable background workflows with explicit concurrency and memory/disk retention bounds.
- **README wording:** The dashboard runs one bounded job at a time with cooperative cancellation and pruned durable logs.
- **Interview evidence:** Explain boundary-level cancellation, worker sharing, status transitions, and log-file race/retention behavior.
- **Reproduction:** `python -m unittest tests.test_web_frontend tests.test_web_scheduler`

## 8. Deterministic local throughput

- **Raw result:** 25,288 scores/s and 115,938 URL canonicalizations/s medians across seven-run synthetic workloads.
- **Why it matters:** Local rule and identity logic are negligible compared with I/O/model latency.
- **Conservative CV wording:** Benchmarked deterministic scoring at 25.3k vacancies/s and URL normalization at 115.9k URLs/s locally.
- **Strong CV wording:** Sustained median local throughput of 25.3k evaluations/s and 115.9k canonicalizations/s over seven-run benchmarks.
- **README wording:** Reproducible synthetic benchmarks isolate local CPU paths from I/O and models.
- **Interview evidence:** Explain warmups, fixed data, medians, machine dependence, and audit-informed fast-path contamination.
- **Reproduction:** `python scripts/achievement_metrics.py`

## 9. Conservative duplicate identity

- **Raw result:** 100% precision, 70% recall, 82.35% F1 on 10 labeled duplicate pairs; zero false merges.
- **Why it matters:** The system favors avoiding destructive false merges while removing URL noise.
- **Conservative CV wording:** Validated canonical URL deduplication at 100% precision and 70% recall on an adversarial synthetic set.
- **Strong CV wording:** Achieved zero false merges and 82.35% F1 across exact, tracking, query, and cross-source duplicate cases.
- **README wording:** Canonical identity strips tracking noise but deliberately does not claim semantic entity resolution.
- **Interview evidence:** Explain pairwise denominators, three missed semantic pairs, and why precision alone would mislead.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 10. Provider-independent analysis architecture

- **Raw result:** 3 providers and one deterministic fallback behind one analysis protocol; 3 model-backed operations.
- **Why it matters:** The application is not coupled to one vendor or authentication path.
- **Conservative CV wording:** Unified OpenAI, Claude CLI, and Codex CLI analysis behind one provider contract with deterministic fallback.
- **Strong CV wording:** Designed a three-provider AI boundary with defensive normalization and an always-available vacancy-analysis fallback.
- **README wording:** Switch providers at runtime without changing downstream vacancy analysis/persistence code.
- **Interview evidence:** Explain protocol shape, structured prompts, CLI timeouts/no-tool flags, normalization, and different fallback guarantees.
- **Reproduction:** `rg -n "AnalysisClient|fallback_strategy|AI_BACKEND" src/cvbankas_tracker`

# Top Product Achievements

## 1. Seven-board product breadth

- **Raw result:** 7 independently usable external boards: CVbankas, CVMarket, CV-Online, HH.ru, JustJoin.it, Startup Jobs, EU Remote Jobs.
- **Why it matters:** A user searches heterogeneous boards through one workflow.
- **Conservative CV wording:** Integrated seven job boards behind one normalized vacancy workflow.
- **Strong CV wording:** Unified seven browser-, HTML-, and feed-based job boards into one local discovery product.
- **README wording:** Seven external adapters normalize into one `Vacancy` model; aliases and sample data are excluded from the count.
- **Interview evidence:** Explain retrieval differences, per-source throttling, alias exclusion, and direct URL ingestion.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 2. Multi-source local corpus

- **Raw result:** 2,449 active canonical vacancy rows across all seven boards, each with a latest analysis.
- **Why it matters:** The product has processed a nontrivial real local corpus rather than only fixtures.
- **Conservative CV wording:** Consolidated and scored 2,449 locally retained vacancies from seven job sources.
- **Strong CV wording:** Built a seven-source local intelligence corpus of 2,449 analyzed job listings.
- **README wording:** The audited local database contains 2,449 URL identities and complete latest-analysis coverage.
- **Interview evidence:** Explain local/private evidence, source shares, canonical versus semantic uniqueness, and snapshot date.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 3. Dashboard search responsiveness

- **Raw result:** p50 1.162 s, p95 2.698 s, max 3.497 s over 60 complete POST-to-render searches.
- **Why it matters:** It measures the user's wait, not an internal function call.
- **Conservative CV wording:** Measured 1.16 s median and 2.70 s p95 local search-to-render latency across 60 dashboard runs.
- **Strong CV wording:** Delivered a 1.16 s median end-to-end local search experience across six realistic query classes.
- **README wording:** Offline dashboard benchmark covers request, background job, parsing, scoring, SQLite, polling, query, and HTML render.
- **Interview evidence:** Explain synthetic source limits, cold/warm pairing, distributions, result counts, and absence of model/network latency.
- **Reproduction:** `python scripts/achievement_metrics.py`

## 4. End-to-end shortlist generation

- **Raw result:** 80 raw -> 66 unique -> 37 shortlisted in 1.227 s median across five cold complex runs.
- **Why it matters:** It describes a complete user workflow with input, reduction, output, and time.
- **Conservative CV wording:** Produced a 37-role shortlist from 80 raw listings in 1.23 s median on a reproducible offline workflow.
- **Strong CV wording:** Cut an 80-listing complex search to 37 reviewable roles in a 1.23 s median end-to-end dashboard flow.
- **README wording:** The benchmark traverses the real web/job/parser/scorer/SQLite/render path with deterministic offline data.
- **Interview evidence:** Explain all funnel denominators and why two fixtures limit external validity.
- **Reproduction:** `python scripts/achievement_metrics.py`

## 5. Review-queue compression

- **Raw result:** 2,449 stored listings -> 304 Medium+; 87.59% reduction, 8.06:1 compression.
- **Why it matters:** It quantifies how much material a user can avoid reviewing at the selected threshold.
- **Conservative CV wording:** Reduced a 2,449-listing local corpus to a 304-item Medium+ review queue using explainable scores.
- **Strong CV wording:** Compressed a 2,449-role corpus by 87.59% into an explained 304-item review queue.
- **README wording:** Latest-score thresholding yields 304 Medium+ and 20 High-fit rows from 2,449 URL identities.
- **Interview evidence:** Explain threshold semantics and why compression is not independently validated recall.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 6. CV-Online request elimination

- **Raw result:** 3,726 recorded item attempts reused payloads from 50 listing pages instead of issuing one detail request each.
- **Why it matters:** The product reduces latency/load on a full-feed source.
- **Conservative CV wording:** Reused embedded listing payloads to eliminate per-item detail requests in a full-feed integration.
- **Strong CV wording:** Eliminated 3,726 per-vacancy fetches in recorded CV-Online runs using cached embedded feed payloads.
- **README wording:** The adapter caches page payloads during collection and serves detail parsing from that cache.
- **Interview evidence:** Trace `_listing_payloads`, canonical keys, incremental collection, and why the count is code/history-derived rather than packet-captured.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 7. Failure-degraded discovery

- **Raw result:** 100% of unaffected outputs survived 11/11 controlled source-failure patterns.
- **Why it matters:** Users still receive results when boards fail independently.
- **Conservative CV wording:** Preserved results from healthy sources across 11 controlled degraded-run scenarios.
- **Strong CV wording:** Continued returning every unaffected source result across all single- and multi-board fault scenarios tested.
- **README wording:** Partial runs expose missing-source evidence without discarding successful board results.
- **Interview evidence:** Explain degraded status, all-failed behavior, per-item versus source failures, and historical corroboration.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 8. Three user interfaces, one state model

- **Raw result:** CLI, Rich TUI, FastAPI dashboard; 10 rendered workflows; 32 web routes; one SQLite database.
- **Why it matters:** Users can operate the same product interactively, graphically, or in scripts.
- **Conservative CV wording:** Exposed one job-search domain through CLI, terminal UI, and a 10-workflow web dashboard.
- **Strong CV wording:** Delivered three interfaces over one transactional state model, including 10 dashboard workflows and 32 routes.
- **README wording:** Interfaces remain thin over shared collection, tracking, and inbox services.
- **Interview evidence:** Trace shared services and distinguish UI routes from independent product features.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 9. Complete core identity fields

- **Raw result:** 2,449/2,449 rows have source, ID, URL, title, and company; 92.36% have raw text.
- **Why it matters:** Every stored item is identifiable and displayable even when optional board fields are missing.
- **Conservative CV wording:** Maintained 100% title/company/source/URL completeness across a 2,449-listing local corpus.
- **Strong CV wording:** Normalized seven sources with complete core identity fields across all 2,449 retained listings.
- **README wording:** Core fields are complete; optional location, salary, requirements, and responsibilities retain explicit completeness rates.
- **Interview evidence:** Explain field definitions, non-empty SQL, and why publication/work-mode completeness is unavailable.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

## 10. Operational scale with observable fallback

- **Raw result:** 59 completion summaries, 27,518 attempted details, 3,829 output rows, 285 logged AI fallbacks.
- **Why it matters:** The system records degraded AI behavior during repeated real local use.
- **Conservative CV wording:** Processed 27k+ log-backed vacancy attempts with durable run summaries and explicit AI-fallback logging.
- **Strong CV wording:** Operated across 27,518 recorded vacancy attempts while surfacing 285 model fallback events for diagnosis.
- **README wording:** Durable job logs preserve completion summaries, partial failures, and AI fallback reasons.
- **Interview evidence:** Explain ignored local evidence, log deduplication, retention gaps, and why it is not a success-rate denominator.
- **Reproduction:** `python scripts/achievement_metrics.py --skip-performance`

# Top Five Overall CV Achievements

1. **Seven-source resilient ingestion:** 7 external boards; all unaffected outputs preserved in 11/11 source-failure scenarios.
2. **AI containment:** 8/8 simulated provider/output faults produced valid stored analyses with zero invalid rows.
3. **End-to-end product result:** 80 raw listings -> 37 shortlisted in 1.23 s median across five complex offline dashboard runs.
4. **Real local scale and compression:** 2,449 analyzed vacancies -> 304 Medium+ review items, an 87.59% reduction.
5. **Concurrent integrity:** 16/16 eight-worker duplicate writes retained one canonical identity with integrity `ok` and zero FK violations.

# Best Balanced Set for a One-Page CV

Use four, not five overlapping engineering claims:

1. **Product breadth:** Unified seven heterogeneous job boards behind one normalized job-discovery workflow.
2. **Product performance:** Generated 37 shortlist rows from 80 raw listings in 1.23 s median on a reproducible end-to-end dashboard benchmark.
3. **AI/reliability:** Contained 8/8 simulated provider/output fault modes with deterministic fallback/normalization and zero invalid analysis rows stored.
4. **Product scale/efficiency:** Reduced a real 2,449-listing local corpus to 304 Medium+ review items (87.59% reduction), with the caveat that relevance recall is not independently labeled.

Three of the four are recruiter-readable without opening the repository. Use the seven-source failure-isolation claim instead of the compression claim when targeting reliability/platform roles.

# Limitations / Metrics Not Established

| Metric | Why it cannot currently be established |
|---|---|
| Live operational source count | Six are enabled, but no safe contemporaneous seven-source crawl was performed; recent per-source denominators are inconsistent and CVMarket history contains failures. |
| Live per-source latency/p95 | Run summaries do not record source start/end times; a new live crawl would depend on changing sites and could create load. |
| External AI-assisted search p50/p95 | No controlled provider credential/model/network benchmark was available. |
| Held-out relevance precision/recall/F1 | No independently labeled untouched set; the synthetic set became tuning data. |
| Held-out work-model/location/eligibility/seniority quality | Same label contamination and single-labeler limitation. |
| Manual-label agreement | Only one audit labeler. |
| Semantic cross-board duplicates and unique contribution | Identity is URL-based; no real labeled entity-resolution set. |
| Production dedup precision/recall | The measured set is synthetic, not sampled/verified production duplicates. |
| Ranking quality/top-N precision | No graded judgments or click/application outcome labels. |
| EU/EEA/timezone eligibility quality | No complete persisted classifier contract or labeled corpus. |
| Freshness/discovery delay and stale rate | Publication and ingestion timestamps are not sufficiently reliable/stored. |
| Model calls avoided/cache hit rate | No model-invocation counter or model-output cache. |
| Tokens/cost per listing | No token accounting; current prices are not stored with runs. |
| Scheduled-run success rate/uptime/days unattended | Logs lack a complete scheduled-start denominator; installed task is disabled. |
| Retry success rate | Retries are tested but historical attempts/outcomes are not consistently paired. |
| Before/after speed/quality gains | Git history lacks comparable historical benchmark artifacts. |
| Exact migration count | Migrations are embedded in bootstrap with no version ledger/files. |
| Exact unit/integration/e2e counts | The suite has no formal markers. |
| p99 search latency | 60 total observations and 10 per class are insufficient for a stable tail estimate. |

# Metrics That Should Not Be Used

- **100% relevance/work-mode/eligibility/seniority accuracy:** the cases were used to tune the rules; these are regression results, not holdout evidence.
- **“AI-assisted search under two seconds”:** external AI was not timed; adversarial/profile-driven p95 exceeded two seconds and overall p95 was 2.698 s.
- **62.16% usable terminal-run rate or 40.54% complete rate:** these expose reliability work to do; partial semantics and retained failures make them poor achievement claims.
- **“Seven sources currently operational”:** seven are implemented, six enabled in the primary config, and current live operability was not proven.
- **782 shortlisted jobs:** applications were created under different historical auto-save settings and are not a stable shortlist definition.
- **364 tests by itself:** test count is supporting evidence only, and 30 current tests were added during audit/hardening work.
- **25.3k rule scores/s without context:** it excludes network/model/storage and the working tree already contained an audit-informed fast path.
- **100% dedup precision alone:** recall is 70%; omitting it would conceal semantic misses.
- **Source clean-run percentages:** `failed` and `attempted` do not share a clean universal denominator across listing/item stages.
- **59 successful scheduled runs:** the logs contain completion summaries, not a complete schedule-start denominator.

# Benchmark Contamination and Leakage

- The public benchmark cases were authored after implementation inspection, so case selection is not blind.
- Labels were fixed before first execution and the SHA-256 is recorded; labels were not changed afterward.
- The relevance/work-mode/eligibility/seniority cases were used to harden production rules. They are now development regression data.
- The current working tree already contained those production rule changes and an analysis-performance fast path when this audit continuation began. No production behavior was changed while adding the new web/funnel/completeness measurements.
- The web benchmark uses two real bundled HTML fixtures behind a synthetic deterministic source. Reusing fixtures limits content diversity.
- Viewing per-case errors consumed the public set for tuning. A fresh independently labeled private set is required for a public quality claim.
- The private-label runner defaults to aggregate-only output and hash-checks case/label correspondence; no private labels were available during this audit.

# New Measurement Tooling

Metrics requiring new code:

- repository, source, test, route, job, table/index, database, log, scheduler, and completeness inventories;
- canonical dedup precision/recall/F1;
- development relevance/work-mode/eligibility/seniority evaluation;
- single/multi-source and AI fault injection;
- concurrent idempotency/integrity;
- URL/scoring/persistence/inbox performance;
- 60-run user-perceived dashboard search distribution;
- five-run end-to-end funnel, stage timing, throughput, and review compression;
- private-label blind-set construction/evaluation protocol.

# Files Added or Modified

Audit/evaluation artifacts present in the working tree:

- `.gitignore`
- `benchmarks/achievement_metrics_cases.json`
- `benchmarks/blind_inputs.template.json`
- `benchmarks/blind_labels.private.example.json`
- `docs/ACHIEVEMENT_METRICS.md`
- `docs/BLIND_EVALUATION.md`
- `docs/achievement_metrics_results.json`
- `scripts/achievement_metrics.py`
- `scripts/build_blind_candidate_set.py`
- `scripts/evaluate_blind_set.py`
- `scripts/refine_eligible_ai_report.py`
- `src/cvbankas_tracker/ai_cli.py`
- `src/cvbankas_tracker/analysis.py`
- `src/cvbankas_tracker/storage.py`
- `tests/test_ai_cli.py`
- `tests/test_analysis.py`
- `tests/test_blind_evaluation.py`
- `tests/test_storage_and_io.py`

Pre-existing local runtime/tool artifacts visible to Git but not treated as audit deliverables: `.omx/` and `config/job_logs/`.

# Reproduction Commands

Run from the repository root.

## Full quantitative audit, performance, web latency, and funnel

```powershell
python scripts/achievement_metrics.py
```

## Structural/database/log/evaluation/fault metrics without timing workloads

```powershell
python scripts/achievement_metrics.py --skip-performance
```

## Full current test suite and coverage

```powershell
$env:OPENAI_API_KEY=''
python -m coverage run --data-file=tmp/.coverage.achievement_audit --source=src/cvbankas_tracker -m unittest discover -s tests -p 'test_*.py'
python -m coverage report --data-file=tmp/.coverage.achievement_audit --include='src/cvbankas_tracker/*'
```

Expected current test result: `Ran 364 tests in 58.151s` and `OK`. Runtime varies. Expected current coverage: 77% overall; AI CLI 100%, analysis 99%, storage 99%.

## Lint

```powershell
python -m ruff check src tests scripts
```

## Benchmark-case integrity

```powershell
Get-FileHash benchmarks/achievement_metrics_cases.json -Algorithm SHA256
```

Expected: `FC74CC9492D147BBEAB85648A123EF793AC634422FA7E66163019D02D3496E81`.

## Current versus committed test-method inventory

```powershell
(rg -n '^\s*def test_' tests -g 'test_*.py' | Measure-Object).Count
(git grep -n -E '^\s*def test_' HEAD -- 'tests/test_*.py' | Measure-Object).Count
```

Expected: current 364, committed `HEAD` 334.

## Windows scheduled-task evidence

```powershell
Get-ScheduledTask -TaskName 'JobSeekerDaily','JobSeekerDashboard' -ErrorAction SilentlyContinue |
  ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    [pscustomobject]@{
      TaskName=$_.TaskName
      State=$_.State
      LastRunTime=$info.LastRunTime
      NextRunTime=$info.NextRunTime
      LastTaskResult=$info.LastTaskResult
    }
  }
```

## Create a future independently labeled candidate set

```powershell
python scripts/build_blind_candidate_set.py --database config/job_seeker.db --profile sample_data/active_profile.json --per-category 120 --dedup-pairs 100
python scripts/evaluate_blind_set.py --cases benchmarks/private/blind_inputs.json --labels benchmarks/private/blind_labels.json
```

# Additional Product Achievement Discovered

The strongest unrequested metric is **CV-Online payload reuse**: 3,726 recorded vacancy attempts were served from embedded payloads collected across 50 feed pages, avoiding a separate detail request for each item. Unlike the classification percentages, this combines a direct code path with real historical counts and does not depend on subjective labels.
