import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .career_html_structured import collect_structured_html

_SIMPLE_LISTS = {
    "mailerlite": "jobs",
    "kilo": "career",
    "macaw": "careers",
    "nordcurrent": "careers",
    "unmanned-defense-systems": "career",
}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.href = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.items.append((self.href, " ".join(" ".join(self.parts).split())))
            self.href = None
            self.parts = []


def _page_text(value):
    if isinstance(value, tuple):
        value = value[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _collect_deel(company, fetch_html):
    page = _page_text(fetch_html(company["ats_url"]))
    marker = r'\"jobs\":['
    start = page.find(marker)
    if start < 0:
        return None
    raw = page[start + len(marker) - 1:].replace(r'\"', '"').replace(chr(92) * 2, chr(92))
    try:
        postings, _ = json.JSONDecoder().raw_decode(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(postings, list) or not postings:
        return None
    jobs = []
    seen = set()
    for posting in postings:
        details = posting.get("attributes") if isinstance(posting, dict) else None
        if not isinstance(details, dict):
            return None
        if details.get("is_listed") is False:
            continue
        job_id = details.get("ashby_id")
        title = details.get("title")
        url = details.get("external_link")
        if not job_id or not title or not isinstance(url, str) or not url.startswith("https://jobs.deel.com/deel/job-details/"):
            return None
        if job_id in seen:
            return None
        seen.add(job_id)
        jobs.append({
            "id": str(job_id),
            "title": str(title),
            "url": url,
            "location": str(details.get("location_name") or ""),
            "description": str(details.get("full_job_description") or ""),
        })
    return (jobs, [company["ats_url"]], False) if jobs else None


def _collect_simple(company, fetch_html, segment):
    base = company.get("ats_url") or company.get("career_url")
    if not base:
        return None
    page = _page_text(fetch_html(base))
    parser = _Links()
    parser.feed(page)
    host = urlparse(base).hostname
    jobs = {}
    incomplete = False
    for href, label in parser.items:
        url = urljoin(base, href)
        parsed = urlparse(url)
        if parsed.hostname != host:
            continue
        if label.casefold() in {"next", "next page", "load more", "show more"}:
            incomplete = True
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != segment:
            continue
        job_url = parsed._replace(query="", fragment="").geturl()
        slug = parts[1]
        candidate = re.split(r"Full-time|Part-time", label, maxsplit=1)[0].strip()
        if not candidate or candidate.casefold() in {"apply", "apply now", "read more"} or len(candidate) > 85:
            candidate = slug.replace("-", " ").title()
        description = label if len(label) > len(candidate) else ""
        job = {
            "id": slug,
            "title": candidate,
            "url": job_url,
            "location": "",
            "description": description,
        }
        if job_url not in jobs or jobs[job_url]["title"] == slug.replace("-", " ").title():
            jobs[job_url] = job
    return (list(jobs.values()), [base], incomplete) if jobs else None


def _collect_retool(company, fetch_html):
    page = _page_text(fetch_html(company["ats_url"]))
    marker = chr(92) + '"jobs' + chr(92) + '":['
    start = page.find(marker)
    if start < 0:
        return None
    raw = page[start + len(marker) - 1:].replace(chr(92) + '"', '"').replace(chr(92) * 2, chr(92))
    try:
        postings, _ = json.JSONDecoder().raw_decode(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(postings, list) or not postings:
        return None
    jobs = []
    seen = set()
    for posting in postings:
        if not isinstance(posting, dict):
            return None
        job_id = posting.get("id")
        title = posting.get("title")
        url = posting.get("link")
        if not job_id or not title or not isinstance(url, str) or not url.startswith("https://jobs.gem.com/retool/"):
            return None
        if job_id in seen:
            return None
        seen.add(job_id)
        jobs.append({
            "id": str(job_id),
            "title": str(title),
            "url": url,
            "location": str(posting.get("location") or ""),
            "description": "",
        })
    return (jobs, [company["ats_url"]], False)


def collect_listing_html(company, max_pages, fetch_html):
    if max_pages < 1:
        return None
    structured = collect_structured_html(company, max_pages, fetch_html)
    if structured is not None:
        return structured
    source_id = company.get("company_id")
    if source_id == "deel":
        return _collect_deel(company, fetch_html)
    if source_id == "retool":
        return _collect_retool(company, fetch_html)
    segment = _SIMPLE_LISTS.get(source_id)
    if segment:
        return _collect_simple(company, fetch_html, segment)
    return None
