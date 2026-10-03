from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.companies import CompanyRegistry
from cvbankas_tracker.storage import DatabaseManager

REPO_ROOT = Path(__file__).resolve().parents[1]

SOURCE_FIXES = {
    "coherent-solutions": {
        "ats_type": "greenhouse",
        "ats_token": "coherentsolutions",
        "ats_url": "https://job-boards.eu.greenhouse.io/coherentsolutions",
    },
    "danske-bank": {
        "ats_type": "html",
        "ats_token": None,
        "ats_url": "https://ejqi.fa.ocs.oraclecloud.eu/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
    },
    "elastic": {
        "ats_type": "greenhouse",
        "ats_token": "elastic",
        "ats_url": "https://boards-api.greenhouse.io/v1/boards/elastic/jobs?content=true",
    },
    "epam": {
        "ats_type": "html",
        "ats_token": None,
        "ats_url": "https://careers.epam.com/en/jobs",
        "api_url": "https://careers.epam.com/api/jobs/v2/search/careers-i18n",
    },
    "ignitis": {
        "ats_type": "smartrecruiters",
        "ats_token": "Ignitisgroup",
        "ats_url": "https://jobs.smartrecruiters.com/Ignitisgroup",
    },
    "paystrax": {
        "ats_type": "html",
        "ats_token": None,
        "ats_url": "https://paystrax.com/career",
    },
    "retool": {
        "ats_type": "html",
        "ats_token": None,
        "ats_url": "https://retool.com/careers",
        "career_url": "https://retool.com/careers",
    },
    "telia": {
        "ats_type": "workday",
        "ats_token": "teliacompany",
        "ats_url": "https://teliacompany.wd3.myworkdayjobs.com/en-US/Telia_careers",
    },
    "vilniaus-vandenys": {
        "ats_type": "html",
        "ats_token": None,
        "ats_url": "https://vilniausvandenys.teamdash.com/p/job/SHvDt8Ru/atraskite-karjeros-ir-praktikos-galimybes-vilniaus-vandenyse",
        "career_url": "https://vilniausvandenys.teamdash.com/p/job/SHvDt8Ru/atraskite-karjeros-ir-praktikos-galimybes-vilniaus-vandenyse",
    },
    "wargaming": {
        "ats_type": "html",
        "ats_token": None,
        "ats_url": "https://wargaming.com/en/careers/",
        "api_url": "https://wargaming.com/en/api/careers/vacancy/?limit=100",
    },
    "wix": {
        "ats_type": "smartrecruiters",
        "ats_token": "Wix2",
        "ats_url": "https://careers.smartrecruiters.com/Wix2",
    },
}


def apply_source_fixes(registry: CompanyRegistry) -> tuple[list[str], list[str]]:
    updated = []
    missing = []
    for company_id, updates in SOURCE_FIXES.items():
        company = registry.get(company_id)
        if company is None:
            missing.append(company_id)
            continue
        if all(company.get(key) == value for key, value in updates.items()):
            continue
        saved = registry.save(updates, company_id=company_id)
        ats_updates = {
            key: updates[key]
            for key in ("ats_type", "ats_token")
            if key in updates and saved.get(key) != updates[key]
        }
        if ats_updates:
            registry.save(ats_updates, company_id=company_id)
        updated.append(company_id)
    return updated, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply verified source URL fixes to an existing owner-managed registry.")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "config" / "job_seeker.db")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error(f"Database does not exist: {args.db}")
    database = DatabaseManager(args.db)
    database.initialize()
    registry = CompanyRegistry(database)
    updated, missing = apply_source_fixes(registry)
    print(f"Updated {len(updated)} existing company source(s).")
    if updated:
        print("Updated IDs: " + ", ".join(updated))
    if missing:
        print("Skipped missing IDs: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
