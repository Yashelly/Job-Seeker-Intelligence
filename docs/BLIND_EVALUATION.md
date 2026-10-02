# Blind evaluation protocol

This protocol separates a public case file from privately held labels. It makes a result a genuine holdout result only when the implementer cannot inspect the private labels or per-case errors before the first run.

## Prepare

1. Freeze the code revision that will be evaluated.
2. Copy `benchmarks/blind_inputs.template.json` to a new input file and populate it with real or manually anonymised listings. Do not add expected outcomes there.
3. A person who will not tune the code creates a private labels file using `benchmarks/blind_labels.private.example.json` as a schema guide. Keep it in `benchmarks/private/`, which is ignored by Git.
4. Hash the input file and insert its exact SHA-256 into `labels.cases_sha256`:

```powershell
Get-FileHash benchmarks/blind_inputs.json -Algorithm SHA256
```

The evaluator rejects labels attached to a different input file and rejects missing, duplicate, or mismatched case IDs.

To produce a local candidate file from the active SQLite database without generating labels:

```powershell
python scripts/build_blind_candidate_set.py `
  --database config/job_seeker.db `
  --profile sample_data/active_profile.json `
  --per-category 120 `
  --dedup-pairs 100
```

This writes to the ignored `benchmarks/private/blind_inputs.json`. The script samples only local data and never calls an external job board.

## Label schema

Every input case has a unique `id`. Its corresponding private label uses the same `id` and one outcome field:

| Category | Input fields | Private label field |
|---|---|---|
| `deduplication` | `id`, `url` | `expected_group` |
| `relevance` | `id`, `title`, `company`, `location`, `text` | `expected_relevant` |
| `work_mode` | `id`, `text` | `expected`: `remote`, `hybrid`, or `office` |
| `eligibility` | `id`, `title`, `company`, `location`, `text` | `expected`: boolean |
| `seniority` | `id`, `text` | `expected`: `intern`, `junior`, `mid`, `senior`, `staff_principal`, or `unknown` |

## Run once

```powershell
python scripts/evaluate_blind_set.py `
  --cases benchmarks/blind_inputs.json `
  --labels benchmarks/private/blind_labels.json
```

The default result path is also ignored: `benchmarks/private/blind_results.json`. It contains only aggregate metrics. Do not run with `--include-errors` until deciding that the holdout may become a development regression set.

## Sample-size guidance

- Relevance and eligibility: at least 100 cases each, including a balanced set of positives and negatives.
- Work mode: at least 40 examples per class.
- Seniority: at least 25 examples per class.
- Deduplication: at least 100 labelled true-duplicate and 100 hard-negative pairs, with cross-source examples.

Two independent annotators and an adjudication record are preferable. Record the code revision, input hash, label hash, label policy, and whether per-case errors were viewed. After any code change informed by the result or error list, the set is no longer blind and must be reclassified as a regression set.
