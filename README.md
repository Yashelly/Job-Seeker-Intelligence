# Job Application Agent

A local-first AI-assisted job application agent for collecting vacancies, extracting structured data, evaluating job relevance, and helping prioritize which roles are actually worth reviewing or applying to.

## Project Purpose

Job boards are noisy. A large share of listings are clearly off-target, while relevant roles can be buried under retail, warehouse, manual labor, or generic business vacancies.

This project aims to solve that by turning raw vacancy pages into a more structured decision flow:

- collect vacancies from a source
- extract structured job data
- evaluate whether a role is in the target domain
- estimate current fit
- store results locally for later review

The goal is not just to scrape vacancies, but to build a system that helps separate:

- **relevant roles worth reviewing**
- **stretch roles worth considering**
- **off-target roles that should be ignored**

---

## Main Goals

- Build a **local-first** job analysis workflow
- Reduce manual job-board browsing
- Use AI for **structured extraction** instead of brittle keyword-only parsing
- Use AI-assisted evaluation for **relevance and fit**
- Keep the system transparent and inspectable through logs and local storage
- Support configurable runs through YAML instead of long terminal commands

---

## What Is Implemented

### Vacancy collection
- CVBankas batch collection is implemented
- listing pages can be traversed across multiple pages
- vacancy detail pages are fetched and parsed individually

### Structured vacancy extraction
- vacancies can be processed through:
  - `heuristic`
  - `openai`
  - `auto`
- OpenAI-based extraction is used to convert vacancy text into structured fields

### Match evaluation
- the project supports AI-assisted job match evaluation
- current output includes:
  - `target_domain`
  - `relevance_score`
  - `fit_score`
  - `final score`
  - `decision`

### Off-target filtering
- obvious off-target roles can be filtered early before deeper evaluation
- this helps reduce wasted analysis on clearly irrelevant vacancies

### Local persistence
- results are saved into SQLite
- each processed vacancy run is stored locally for later inspection

### Runtime configuration
- runs can be configured through YAML
- OpenAI secrets are loaded from `.env`
- no need to repeat long CLI argument chains every time

### Console visibility
- batch execution includes:
  - timestamps
  - progress counters
  - colored status output
  - score breakdown in console logs

---

## Current Workflow

The current high-level flow is:

```text
CVBankas listing pages
-> vacancy links
-> vacancy detail pages
-> structured extraction
-> match evaluation
-> SQLite storage
-> console output
```

For OpenAI-based runs, the system currently works as a hybrid pipeline:

```text
crawl
-> parse vacancy page
-> structured extraction
-> off-target filtering / AI-assisted match evaluation
-> save results locally
```

---

## Current Scope

### Implemented
- CVBankas crawling
- batch mode
- OpenAI extraction
- AI-assisted match evaluation
- YAML-based run configuration
- SQLite storage
- console progress logging

### Not fully implemented yet
- multi-source crawling beyond CVBankas
- browser-based crawling for JS-heavy sites
- advanced retry/backoff strategy for all network failures
- email drafting / automated application submission
- UI/dashboard
- ranking review interface
- deduplicated long-term vacancy history management

---

## Why This Project Exists

This project is intended as a practical system design and automation project, not just a wrapper around an API.

It combines:
- crawling
- structured extraction
- evaluation logic
- local persistence
- configuration-driven execution

The idea is to model a realistic AI-assisted workflow where the model is part of a larger system, rather than the entire system.

---

## Project Structure

```text
src/
  main.py
  cvbankas.py
  extraction_service.py
  openai_extractor.py
  openai_models.py
  match_service.py
  openai_match_evaluator.py
  match_models.py
  scorer.py
  config_loader.py
  storage.py
  validator.py
  ingest.py
  generator.py
  utils.py

config/
  cvbankas_openai.example.yaml

profile/
  user_profile.example.yaml

data/
  app.db   # local runtime database, ignored in git

tests/
  ...
```

---

## Configuration Model

The project uses two separate configuration layers:

### 1. `.env`
Used for secrets and environment-specific values.

Example:

```env
OPENAI_API_KEY=your_openai_api_key_here
JOB_AGENT_OPENAI_MODEL=gpt-4o-mini
```

### 2. YAML config
Used for run settings such as source, limits, and mode.

Example:

```yaml
profile: profile/user_profile.local.yaml
db: data/app.db

extractor: openai
openai_model: gpt-4o-mini
match_evaluator: openai

source:
  type: cvbankas
  start_url: https://www.cvbankas.lt/
  max_pages: 10
  limit: 100
  delay_seconds: 0.5

input:
  text: null
  text_file: null
  url: null
```

---

## Output Meaning

Each processed vacancy may include fields such as:

- `target_domain`
  - `target`
  - `adjacent`
  - `off_target`

- `relevance_score`
  - how relevant the vacancy is to the selected target direction

- `fit_score`
  - how strong the current candidate fit appears to be

- `final score`
  - combined score used for quick prioritization

- `decision`
  - `apply`
  - `stretch`
  - `skip`

This is meant to separate:
- vacancies that are **in the right domain**
- vacancies that are **not ideal but still worth seeing**
- vacancies that are **clearly irrelevant**

---

## Current Limitations

- crawling is currently centered on CVBankas
- some vacancy pages may time out under larger batch runs
- AI evaluation is only as good as the extracted vacancy content
- sparse job descriptions can still reduce evaluation quality
- not all ranking behavior is final; the system is still being refined

---

## Planned Improvements

- stronger retry and backoff handling for crawling
- support for additional job sources
- better ranking review workflow
- improved deduplication and result comparison
- optional browser-based collection for more complex sites
- richer analytics on stored vacancy runs
- better human review workflows for shortlist inspection

---

## Security Notes

Do **not** commit:
- `.env`
- local databases
- private user profiles
- local/private YAML configs

Use:
- `.env.example`
- `profile/user_profile.example.yaml`
- `config/*.example.yaml`

for safe public repository templates.

---

# Quick Start

## 1. Clone the repository

```bash
git clone <your-repo-url>
cd job-application-agent
```

## 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

If needed, also install:

```bash
python -m pip install pyyaml python-dotenv
```

## 3. Create a local `.env`

Create a file named `.env` in the project root:

```env
OPENAI_API_KEY=your_openai_api_key_here
JOB_AGENT_OPENAI_MODEL=gpt-4o-mini
```

## 4. Create a local user profile

Copy the example profile and adjust it for your own use:

```bash
copy profile\user_profile.example.yaml profile\user_profile.local.yaml
```

## 5. Create a local run config

Copy the example config and customize it if needed:

```bash
copy config\cvbankas_openai.example.yaml config\cvbankas_openai.local.yaml
```

Then update the profile path inside that YAML if needed.

## 6. Run the agent

```bash
python -m src.main --config config/cvbankas_openai.local.yaml
```

## 7. Optional: run without YAML

```bash
python -m src.main --cvbankas --cvbankas-max-pages 3 --cvbankas-limit 20 --db data/app.db --extractor openai
```

---

## Example Use Case

A typical run may:
- collect vacancies from CVBankas
- evaluate 100+ listings
- skip clearly off-target roles
- keep relevant IT / automation / support roles visible
- save everything into SQLite for later inspection

---

## Status

This is an active work-in-progress project focused on building a practical AI-assisted vacancy analysis pipeline.

The current version already supports real batch runs, structured extraction, YAML-driven execution, and local persistence, while leaving room for stronger ranking and broader source support.
