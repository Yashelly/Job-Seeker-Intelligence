from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


SPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.I | re.S)
YEAR_RE = re.compile(
    r"(\d+)\s*(?:-|to|–|—)?\s*(\d+)?\s*\+?\s*"
    r"(?:years?|yrs?|лет|года|met(?:ų|u)?|m\.|lat\b|lata\b)",
    re.I,
)
CANDIDATE_CONTEXT_RE = re.compile(
    r"experience|requirements|what we.?re looking for|looking for|you have|"
    r"you bring|must have|required|commercial|professional|hands-on|proven|"
    r"as a |work experience|опыт|опыта|требования|стаж|doświadczenie|"
    r"doswiadczenie|wymagania",
    re.I,
)
COMPANY_CONTEXT_RE = re.compile(
    r"global technology company|company with|company has|years of experience, "
    r"committed|on the market|we have been|history|founded|employer|businesses",
    re.I,
)

SENIOR_TITLE_TERMS = [
    "senior",
    "sr.",
    "sr ",
    "lead",
    "team lead",
    "leader",
    "principal",
    "staff",
    "head of",
    "manager",
    "director",
    "architect",
    "руководитель",
    "ведущий",
    "старший",
    "сеньор",
    "тимлид",
    "team leader",
    "tech lead",
    "technical lead",
]
JUNIOR_TERMS = [
    "junior",
    "entry",
    "entry-level",
    "entry level",
    "trainee",
    "intern",
    "assistant",
    "младший",
    "стажер",
    "стажёр",
    "praktikantas",
    "jaunesnysis",
]
REPORT_FIELDS = [
    "rank",
    "rank_score",
    "fit_score",
    "priority",
    "block_reason",
    "language_risk",
    "seniority_risk",
    "signals",
    "title",
    "company",
    "location",
    "salary",
    "source",
    "source_id",
    "why",
    "risks",
    "language_snippets",
    "url",
]


def configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def clean(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = SCRIPT_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def normalize(value: object) -> str:
    return clean(value).lower()


def latest_base_csv(exports_dir: Path) -> Path:
    candidates = sorted(
        exports_dir.glob("ai_automation_eligible_language_experience_*.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    candidates = [
        path
        for path in candidates
        if "final" not in path.name and "refined" not in path.name
    ]
    if not candidates:
        raise FileNotFoundError(
            "No base ai_automation_eligible_language_experience_*.csv found."
        )
    return candidates[0]


def vacancy_body(row: sqlite3.Row) -> str:
    requirements = json.loads(row["requirements_json"] or "[]")
    responsibilities = json.loads(row["responsibilities_json"] or "[]")
    return normalize(
        " ".join(
            [
                row["title"] or "",
                row["company"] or "",
                row["location"] or "",
                row["salary_text"] or "",
                *requirements,
                *responsibilities,
            ]
        )
    )


def refined_seniority(title: str, url: str, body: str) -> tuple[bool, str, str]:
    title_url = f"{normalize(title)} {url.lower()}"
    if any(term in title_url for term in JUNIOR_TERMS):
        return False, "OK", "junior/entry signal"

    senior_hits = [term for term in SENIOR_TITLE_TERMS if term in title_url]
    if senior_hits:
        return True, f"Senior-like title: {', '.join(senior_hits[:4])}", ""

    required_years: list[int] = []
    risk_years: list[int] = []
    for match in YEAR_RE.finditer(body):
        first = int(match.group(1))
        second = int(match.group(2)) if match.group(2) else first
        years = max(first, second)
        start, end = match.span()
        window = body[max(0, start - 120) : min(len(body), end + 160)]
        candidate_context = CANDIDATE_CONTEXT_RE.search(window)
        company_context = COMPANY_CONTEXT_RE.search(window)
        if company_context and not re.search(
            r"looking for|requirements|you have|must have|required|"
            r"опыт работы|what we",
            window,
            re.I,
        ):
            continue
        if not candidate_context:
            continue
        if years >= 5:
            required_years.append(years)
        elif years >= 3:
            risk_years.append(years)

    if required_years:
        return True, f"Requires {max(required_years)}+ years", ""
    if risk_years:
        return False, f"Experience requirement may be {max(risk_years)}+ years", ""
    return False, "OK", ""


def priority_for(rank_score: int, blocked: bool) -> str:
    if blocked:
        return "Blocked"
    if rank_score >= 82:
        return "Top"
    if rank_score >= 68:
        return "Strong"
    if rank_score >= 52:
        return "Maybe"
    return "Low"


def load_items(base_csv: Path, db_path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    db_rows = {
        row["source_url"]: row
        for row in connection.execute(
            """
            SELECT
                source_url, title, company, location, salary_text,
                requirements_json, responsibilities_json
            FROM vacancies
            """
        )
    }
    connection.close()

    items: list[dict[str, object]] = []
    with base_csv.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            item: dict[str, object] = dict(row)
            item["fit_score"] = int(item.get("fit_score") or 0)
            item["language_blocked"] = item.get("language_risk", "OK") != "OK"
            db_row = db_rows.get(str(item["url"]))
            body = (
                vacancy_body(db_row)
                if db_row is not None
                else normalize(
                    " ".join(
                        [
                            str(item.get("title", "")),
                            str(item.get("company", "")),
                            str(item.get("location", "")),
                        ]
                    )
                )
            )

            senior_blocked, senior_risk, senior_note = refined_seniority(
                str(item.get("title", "")),
                str(item.get("url", "")),
                body,
            )
            item["seniority_blocked"] = senior_blocked
            item["seniority_risk"] = senior_risk
            if senior_note and senior_note not in str(item.get("why", "")):
                item["why"] = f"{item.get('why', '')}; {senior_note}".strip("; ")

            block_reasons: list[str] = []
            if item["language_blocked"]:
                block_reasons.append(str(item["language_risk"]))
            if senior_blocked:
                block_reasons.append(senior_risk)

            blocked = bool(block_reasons)
            item["blocked"] = blocked
            item["block_reason"] = "; ".join(block_reasons) if blocked else "OK"
            item["rank_score"] = (
                min(29, int(item["fit_score"])) if blocked else int(item["fit_score"])
            )
            item["priority"] = priority_for(int(item["rank_score"]), blocked)

            base_risks = [
                risk.strip()
                for risk in str(item.get("risks", "")).split(";")
                if risk.strip()
            ]
            filtered_risks = [
                risk
                for risk in base_risks
                if not risk.startswith("Requires ")
                and not risk.startswith("Senior-like title")
            ]
            item["risks"] = (
                "; ".join(([str(item["block_reason"])] if blocked else []) + filtered_risks[:5])
                or "no major risk"
            )
            items.append(item)

    items.sort(
        key=lambda item: (
            bool(item["blocked"]),
            -int(item["rank_score"]),
            -int(item["fit_score"]),
            str(item["title"]).lower(),
            str(item["company"]).lower(),
        )
    )
    return items


def write_csv(path: Path, items: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for rank, item in enumerate(items, 1):
            writer.writerow(
                {
                    "rank": rank,
                    **{
                        field: item.get(field, "")
                        for field in REPORT_FIELDS
                        if field != "rank"
                    },
                }
            )


def html_escape(value: object) -> str:
    return html.escape(clean(value), quote=True)


def markdown_escape(value: object) -> str:
    return clean(value).replace("|", "\\|")


def render_table_rows(items: list[dict[str, object]]) -> str:
    rows: list[str] = []
    for index, item in enumerate(items, 1):
        css_class = "blocked" if item["blocked"] else str(item["priority"]).lower()
        rows.append(
            "<tr>"
            f"<td class='num'>{index}</td>"
            f"<td class='score'>{item['rank_score']}</td>"
            f"<td class='muted'>{item['fit_score']}</td>"
            f"<td><span class='pill {css_class}'>{html_escape(item['priority'])}</span></td>"
            f"<td>{html_escape(item['block_reason'])}</td>"
            f"<td>{html_escape(item['source'])}</td>"
            f"<td><a href='{html_escape(item['url'])}' target='_blank' "
            f"rel='noreferrer'>{html_escape(item['title'])}</a></td>"
            f"<td>{html_escape(item['company'])}</td>"
            f"<td>{html_escape(item['location'])}</td>"
            f"<td>{html_escape(item['why'])}</td>"
            f"<td>{html_escape(item['risks'])}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def write_html(
    path: Path,
    items: list[dict[str, object]],
    eligible: list[dict[str, object]],
    seniority_blocked: list[dict[str, object]],
    language_blocked: list[dict[str, object]],
) -> None:
    source_counts = Counter(str(item["source"]) for item in items)
    block_counts = Counter(str(item["block_reason"]) for item in items if item["blocked"])
    css = """
body{font-family:Inter,Segoe UI,Arial,sans-serif;background:#f6f7f8;color:#1d232b;margin:0;padding:32px}
header,.grid,.note,section{max-width:1440px;margin-left:auto;margin-right:auto}
h1{margin:0 0 8px}.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:18px auto 24px}
.metric{background:#fff;border:1px solid #d8dde3;border-radius:8px;padding:14px}.metric b{display:block;font-size:24px;margin-top:6px}
.note{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px;margin-bottom:18px}
section{background:#fff;border:1px solid #d8dde3;border-radius:8px;overflow:hidden;margin-bottom:32px}
h2{font-size:18px;margin:0;padding:16px 18px;background:#fbfbfc;border-bottom:1px solid #e4e7eb}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid #edf0f2;padding:9px 10px;vertical-align:top;text-align:left}
th{background:#f8fafb;color:#5b6572;position:sticky;top:0}.num,.score{text-align:right;font-variant-numeric:tabular-nums}
.score{font-weight:700}.muted{color:#6b7280}.pill{border:1px solid #cfd6dd;border-radius:999px;padding:3px 8px;white-space:nowrap}
.top{background:#e9f9ef;color:#166534}.strong{background:#edf6ff;color:#075985}.maybe{background:#fff8df;color:#854d0e}
.low{background:#f4f4f5}.blocked{background:#fff1f2;color:#be123c}a{color:#0b65c2;text-decoration:none}p{line-height:1.5}
"""
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_text = ", ".join(f"{key}={value}" for key, value in source_counts.most_common())
    block_text = (
        ", ".join(f"{key} ({value})" for key, value in block_counts.most_common(8))
        or "none"
    )
    document = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>AI Automation Eligible Ranking</title><style>{css}</style></head>
<body>
<header>
<h1>AI Automation Eligible Ranking</h1>
<p>Generated {generated}. Main list keeps only vacancies that match the AI automation intent and are not blocked by language or seniority. Senior, Lead, Principal, Staff, Head, Manager, Architect and real candidate-facing 5+ years requirements are excluded from the main ranking.</p>
</header>
<div class="grid">
<div class="metric">Total<b>{len(items)}</b></div>
<div class="metric">Eligible<b>{len(eligible)}</b></div>
<div class="metric">Blocked<b>{len(items) - len(eligible)}</b></div>
<div class="metric">Language blocked<b>{len(language_blocked)}</b></div>
<div class="metric">Seniority blocked<b>{len(seniority_blocked)}</b></div>
</div>
<div class="note">Sources: {html_escape(source_text)}. Most common blocks: {html_escape(block_text)}.</div>
<section><h2>Top eligible vacancies</h2><table><thead><tr><th>#</th><th>Rank</th><th>Fit</th><th>Priority</th><th>Block</th><th>Source</th><th>Vacancy</th><th>Company</th><th>Location</th><th>Why</th><th>Risks</th></tr></thead><tbody>{render_table_rows(eligible[:300])}</tbody></table></section>
<section><h2>Excluded by seniority despite fit</h2><table><thead><tr><th>#</th><th>Rank</th><th>Fit</th><th>Priority</th><th>Block</th><th>Source</th><th>Vacancy</th><th>Company</th><th>Location</th><th>Why</th><th>Risks</th></tr></thead><tbody>{render_table_rows(seniority_blocked[:160])}</tbody></table></section>
<section><h2>Excluded by language despite fit</h2><table><thead><tr><th>#</th><th>Rank</th><th>Fit</th><th>Priority</th><th>Block</th><th>Source</th><th>Vacancy</th><th>Company</th><th>Location</th><th>Why</th><th>Risks</th></tr></thead><tbody>{render_table_rows(language_blocked[:160])}</tbody></table></section>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def write_markdown(
    path: Path,
    html_path: Path,
    csv_path: Path,
    items: list[dict[str, object]],
    eligible: list[dict[str, object]],
    seniority_blocked: list[dict[str, object]],
    language_blocked: list[dict[str, object]],
) -> None:
    lines = [
        "# AI Automation Eligible Ranking",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total vacancies: **{len(items)}**",
        f"Eligible: **{len(eligible)}**",
        f"Blocked by language: **{len(language_blocked)}**",
        f"Blocked by seniority: **{len(seniority_blocked)}**",
        "",
        "Main list excludes languages outside English/Lithuanian/Russian and senior-like roles.",
        "",
        "## Top eligible vacancies",
        "",
        "| Rank | Rank score | Fit score | Priority | Source | Vacancy | Company | Location | Why | Risks | URL |",
        "|---:|---:|---:|---|---|---|---|---|---|---|---|",
    ]
    for rank, item in enumerate(eligible[:220], 1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(rank),
                    str(item["rank_score"]),
                    str(item["fit_score"]),
                    markdown_escape(item["priority"]),
                    markdown_escape(item["source"]),
                    f"[{markdown_escape(item['title'])}]({item['url']})",
                    markdown_escape(item["company"]),
                    markdown_escape(item["location"]),
                    markdown_escape(item["why"]),
                    markdown_escape(item["risks"]),
                    f"[open]({item['url']})",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Excluded by seniority despite fit",
        "",
        "| # | Fit score | Seniority block | Source | Vacancy | Company | Location | URL |",
        "|---:|---:|---|---|---|---|---|---|",
    ]
    for rank, item in enumerate(seniority_blocked[:140], 1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(rank),
                    str(item["fit_score"]),
                    markdown_escape(item["seniority_risk"]),
                    markdown_escape(item["source"]),
                    f"[{markdown_escape(item['title'])}]({item['url']})",
                    markdown_escape(item["company"]),
                    markdown_escape(item["location"]),
                    f"[open]({item['url']})",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Excluded by language despite fit",
        "",
        "| # | Fit score | Language block | Source | Vacancy | Company | Location | URL |",
        "|---:|---:|---|---|---|---|---|---|",
    ]
    for rank, item in enumerate(language_blocked[:140], 1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(rank),
                    str(item["fit_score"]),
                    markdown_escape(item["language_risk"]),
                    markdown_escape(item["source"]),
                    f"[{markdown_escape(item['title'])}]({item['url']})",
                    markdown_escape(item["company"]),
                    markdown_escape(item["location"]),
                    f"[open]({item['url']})",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Files",
        "",
        f"HTML artifact: `{html_path}`",
        f"Full CSV: `{csv_path}`",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    configure_stdout()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="job_seeker.db")
    parser.add_argument("--exports", default="exports")
    parser.add_argument("--base-csv", default="")
    args = parser.parse_args()

    root = Path.cwd()
    exports_dir = root / args.exports
    base_csv = Path(args.base_csv) if args.base_csv else latest_base_csv(exports_dir)
    if not base_csv.is_absolute():
        base_csv = root / base_csv
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = root / db_path

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = exports_dir / f"ai_automation_eligible_language_experience_final_{stamp}.html"
    csv_path = exports_dir / f"ai_automation_eligible_language_experience_final_{stamp}.csv"
    md_path = exports_dir / f"ai_automation_eligible_language_experience_final_{stamp}.md"

    items = load_items(base_csv, db_path)
    eligible = [item for item in items if not item["blocked"]]
    seniority_blocked = sorted(
        [item for item in items if item["seniority_blocked"]],
        key=lambda item: (-int(item["fit_score"]), str(item["title"]).lower()),
    )
    language_blocked = sorted(
        [item for item in items if item["language_blocked"]],
        key=lambda item: (-int(item["fit_score"]), str(item["title"]).lower()),
    )

    write_csv(csv_path, items)
    write_html(html_path, items, eligible, seniority_blocked, language_blocked)
    write_markdown(
        md_path,
        html_path,
        csv_path,
        items,
        eligible,
        seniority_blocked,
        language_blocked,
    )

    print(f"html={html_path}")
    print(f"csv={csv_path}")
    print(f"md={md_path}")
    print(
        f"eligible={len(eligible)} "
        f"language_blocked={len(language_blocked)} "
        f"seniority_blocked={len(seniority_blocked)} "
        f"total={len(items)}"
    )
    print("top eligible:")
    for rank, item in enumerate(eligible[:12], 1):
        print(
            f"{rank}. rank={item['rank_score']} fit={item['fit_score']} "
            f"{item['source']} | {item['title']} | {item['url']}"
        )
    print("top seniority blocked:")
    for rank, item in enumerate(seniority_blocked[:8], 1):
        print(
            f"{rank}. fit={item['fit_score']} block={item['seniority_risk']} "
            f"{item['source']} | {item['title']} | {item['url']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
