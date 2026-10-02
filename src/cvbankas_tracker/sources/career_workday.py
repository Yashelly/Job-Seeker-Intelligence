"""Read public Workday career-site listings and their posting details."""
from __future__ import annotations

import html
import re
from urllib.parse import urlparse


def collect_workday(company, max_pages, fetch_json):
    board = company.get("ats_url") or company.get("career_url") or ""
    parsed = urlparse(board)
    host = parsed.hostname or ""
    if not host.endswith(".myworkdayjobs.com"):
        raise ValueError("Workday requires a public myworkdayjobs.com career board.")
    tenant = host.split(".")[0]
    path = [part for part in parsed.path.split("/") if part]
    if path and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", path[0]):
        path.pop(0)
    if not path or not re.fullmatch(r"[A-Za-z0-9_-]+", path[0]):
        raise ValueError("Workday board URL is missing its career site identifier.")
    site = path[0]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    pages, jobs = [], []
    limit = 20
    total = None
    for page in range(max(1, int(max_pages))):
        list_url = base + "/jobs"
        payload = fetch_json(list_url, {"appliedFacets": {}, "limit": limit, "offset": page * limit, "searchText": ""})
        pages.append(list_url)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobPostings"), list) or not isinstance(payload.get("total"), int):
            raise ValueError("Workday response did not contain postings and a total.")
        # Workday returns total=0 on offset pages; the first page carries the total.
        if total is None:
            total = payload["total"]
        for item in payload["jobPostings"]:
            if not isinstance(item, dict):
                raise ValueError("Workday response contained a malformed posting.")
            external_path = item.get("externalPath")
            if not isinstance(external_path, str) or not external_path.startswith("/job/") or ".." in external_path:
                raise ValueError("Workday posting is missing a valid detail path.")
            detail_url = base + external_path
            detail = fetch_json(detail_url)
            pages.append(detail_url)
            info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
            if not isinstance(info, dict) or not info.get("title") or not info.get("jobDescription"):
                raise ValueError("Workday posting is missing a title or description.")
            url = f"https://{host}/en-US/{site}{external_path}"
            jobs.append({
                "id": str(info.get("jobReqId") or external_path), "url": url,
                "title": str(info["title"]), "location": str(info.get("location") or item.get("locationsText") or ""),
                "description": re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", info["jobDescription"]))).strip(),
                "raw": info,
            })
        if page * limit + len(payload["jobPostings"]) >= total:
            return jobs, pages, False
        if not payload["jobPostings"]:
            raise ValueError("Workday listings ended before the reported total.")
    return jobs, pages, bool(total is not None and len(jobs) < total)
