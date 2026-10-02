# Career registry edge-case review

Date: 2026-10-01. Branch: `codex/career-registry-edge-cases`.

Scope: company registry import/edit/export, public career collectors, shared vacancy processing, storage, and daily scheduling. Existing unrelated AI and evaluation changes were preserved.

## Fixed defects

| Failure | Result after the fix | Regression coverage |
| --- | --- | --- |
| Short Teamtailor RSS descriptions refetched the feed instead of the detail page | Detail pages supply complete descriptions; a failed detail marks collection partial | `test_career_edge_cases.py`, `test_career_feeds.py` |
| Hidden empty-state templates or failed details claimed a successful zero-job scan | Failed, malformed, hidden, or incomplete listings cannot claim a complete empty result | `test_career_edge_cases.py`, `test_career_html.py` |
| A fallback page hid primary-source failure | Working fallback results are retained and the company remains partial | `test_career_edge_cases.py` |
| Additional boards replaced the primary board | The primary and additional boards are collected, with repeated board requests deduplicated | `test_career_edge_cases.py`, `test_career_discovery_integration.py` |
| Changing a board retained hidden old routing settings | Old boards, API URLs, region, language, filters, and employer overrides are cleared unless explicitly replaced | `test_career_edge_cases.py`, `test_companies.py` |
| A concurrent edit, disable, rename, or locale change accepted old scan results | A monotonic source revision rejects stale scans and discards stale discoveries | `test_career_edge_cases.py` |
| A source edit after discovery could update a known job or save stale AI results | Revision guards execute first inside the same SQLite write transaction, before observations, aliases, vacancies, analyses, or applications | `test_career_pipeline.py` |
| A status-write failure aborted collection of other companies | The failure is reported and other companies continue | `test_career_edge_cases.py` |
| Shared boards silently selected the wrong employer | Explicit board employer labels select the canonical employer; conflicting employers produce partial checks | `test_career_edge_cases.py` |
| An existing incomplete registry table bypassed migration | Missing scan columns and actual identity guarantees are repaired, with backup gating and preserved rows | `test_career_schema.py` |
| A crawl had no aggregate resource budget | Request, response-byte, elapsed-time, and unique-job budgets retain discovered jobs and mark unchecked counts unknown | `test_career_budget.py`, `test_career_edge_cases.py` |
| Redirects drained unbounded bodies and bypassed request accounting | Redirect bodies are closed without draining; every hop is budgeted; streamed final responses check byte/time limits between chunks | `test_career_network_limits.py` |
| An empty legacy schedule enabled every source | The existing scheduler defaults remain the fallback | `test_web_scheduler.py` |
| Partial and failed runs appeared completed | Batch exit codes follow terminal outcomes; the scheduler records partial results and prevents unintended same-day repeats | `test_career_pipeline.py`, `test_web_scheduler.py` |

## Validation

- Full suite: `python -m unittest discover -s tests` — **493 tests passed**.
- Ruff on scoped source/test files, Python compilation, and `git diff --check` passed.
- Independent code-review lane: **APPROVE**, 25 scoped files reviewed, 154 relevant tests passed, no remaining findings.
- Independent architecture lane: **CLEAR**; atomic revision-write boundaries and network budgets verified independently.
- Live public check of Tesonet's configured boards: **233 unique URLs**, attributed as Tesonet 3, Nord Security 142, Surfshark 22, and Oxylabs 66. Four listing requests, no source errors or incomplete reasons. This was discovery only.
- Restarted local dashboard: `/companies`, failed-company filter, and `/schedule` returned HTTP 200. Export contains 115 companies, 112 enabled.
- Corrected the previous scheduler display from completed to partial using the matching stored collection outcome. Next daily search remains **2026-10-02 at 19:00 local time**, with the existing sources preserved.

## Operational limits

Source-check counts describe discovered postings; search-run counts describe analyzed and saved jobs. A blocked or unreadable external page can still fail and is reported explicitly. Elapsed-time limits are checked between operations and response chunks; an in-flight socket operation remains subject to its timeout.

When two distinct employers publish the same URL without an explicit ownership label, the collector retains one URL and marks the conflict partial. Complete many-company observation provenance would require a separate relation and ownership policy. Identical child board identities with contradictory employer overrides currently resolve by configured order; such entries should specify a filter or one authoritative employer label.

No commit or merge was performed.
