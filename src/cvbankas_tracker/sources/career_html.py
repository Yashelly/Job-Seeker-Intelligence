from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable, Mapping
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urldefrag, urljoin, urlparse

from .base import CollectionCancelledError

_JSON_LD_RE = re.compile(
    r'''<script[^>]+type=["']application/ld\+json["'][^>]*>(?P<data>.*?)</script>''',
    re.IGNORECASE | re.DOTALL,
)
_ANCHOR_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r'''(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?P<quote>["'])(?P<value>.*?)(?P=quote)''')
_EMBEDDED_URL_RE = re.compile(r'''https?:\\?/\\?/[^"'<>\s]+''', re.IGNORECASE)
_NEXT_RE = re.compile(
    r'''<(?:a|link)\b(?=[^>]*\brel=["'][^"']*\bnext\b[^"']*["'])[^>]*\bhref=["'](?P<url>[^"']+)["']''',
    re.IGNORECASE | re.DOTALL,
)
_NO_OPENINGS_MARKERS = (
    "no open positions",
    "no current openings",
    "no vacancies",
    "no jobs available",
    "nothing available right now",
    "currently no open roles",
    "currently no vacancies",
    "we are not hiring",
)
_JOB_LINK_MARKERS = (
    "job",
    "jobs",
    "career",
    "careers",
    "opening",
    "openings",
    "position",
    "positions",
    "vacancy",
    "vacancies",
    "role",
    "roles",
    "apply",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "smartrecruiters.com",
    "personio.de",
    "recruitee.com",
)
_ASSET_EXTENSIONS = (
    ".avif", ".css", ".gif", ".ico", ".jpeg", ".jpg", ".js", ".json", ".png", ".svg", ".webp", ".woff", ".woff2",
)
_KNOWN_JOB_HOSTS = (
    "ashbyhq.com",
    "greenhouse.io",
    "jobs.deel.com",
    "jobs.gem.com",
    "lever.co",
    "personio.de",
    "recruitee.com",
    "smartrecruiters.com",
    "workable.com",
)
_DESCRIPTION_MIN_CHARS = 120
_INCOMPLETE_PAGE_MARKERS = (
    "load more",
    "loadmore",
    "show more",
    "show all",
    "view more",
    "see more",
    "more jobs",
    "next page",
    "pagination",
    "data-next",
    "data-page",
    "__next_data__",
)


def collect_html(
    company: dict,
    max_pages: int,
    fetch_html: Callable[[str], str],
    *, max_detail_pages: int | None = None,
) -> tuple[list[dict], list[str], bool]:
    start_url = _text(company.get("ats_url") or company.get("career_url"))
    if not start_url:
        raise ValueError("HTML career collection requires a career_url or ats_url.")
    _valid_public_url(start_url)
    max_pages = max(1, int(max_pages or 1))
    detail_budget = max(1, int(max_detail_pages)) if max_detail_pages is not None else max_pages
    detail_requests = 0

    jobs: list[dict] = []
    pages: list[str] = []
    seen_pages: set[str] = set()
    seen_jobs: set[str] = set()
    page_url = start_url
    incomplete = False
    detail_failures = 0

    for _ in range(max_pages):
        page_url = _preserve_url(urljoin(start_url, page_url))
        if page_url in seen_pages:
            break
        seen_pages.add(page_url)
        page_html = fetch_html(page_url)
        pages.append(page_url)

        page_is_detail = False
        page_jobs, malformed_jobs = _jobs_from_json_ld(page_html, page_url, company)
        for job in page_jobs:
            if job["url"] in seen_jobs:
                continue
            seen_jobs.add(job["url"])
            jobs.append(job)
        if not page_jobs:
            try:
                detail_job = _job_from_detail_html(page_html, page_url, company)
            except ValueError:
                pass
            else:
                if detail_job["url"] not in seen_jobs:
                    seen_jobs.add(detail_job["url"])
                    jobs.append(detail_job)
                    page_is_detail = True
        if malformed_jobs and not page_is_detail:
            incomplete = True
        if len(page_jobs) == 1 and _preserve_url(page_jobs[0]["url"]) == page_url:
            page_is_detail = True
        if not page_is_detail and _looks_incomplete_page(page_html):
            incomplete = True

        detail_urls = [] if page_is_detail else [
            url for url in _detail_links(page_html, page_url, start_url) if url not in seen_jobs and url not in seen_pages
        ]
        remaining_detail_budget = (detail_budget - detail_requests) if max_detail_pages is not None else (max_pages - len(pages))
        if remaining_detail_budget < len(detail_urls):
            incomplete = True
            detail_urls = detail_urls[: max(0, remaining_detail_budget)]
        for detail_url in detail_urls:
            detail_requests += 1
            try:
                detail_html = fetch_html(detail_url)
                pages.append(detail_url)
                detail_job = _job_from_detail_html(detail_html, detail_url, company)
            except CollectionCancelledError:
                raise
            except (HTTPError, OSError):
                detail_failures += 1
                incomplete = True
                continue
            except ValueError:
                detail_failures += 1
                incomplete = True
                continue
            seen_pages.add(detail_url)
            if detail_job["url"] in seen_jobs:
                continue
            seen_jobs.add(detail_job["url"])
            jobs.append(detail_job)

        next_url = _next_url(page_html, page_url, start_url)
        if not next_url:
            break
        if (len(seen_pages) if max_detail_pages is None else _ + 1) >= max_pages:
            incomplete = True
            break
        page_url = next_url
    else:
        next_url = _next_url(page_html, page_url, start_url) if "page_html" in locals() else ""
        if next_url:
            incomplete = True

    if detail_failures and jobs:
        incomplete = True
    if jobs:
        return jobs, pages, incomplete
    first_html = fetch_html(start_url) if not pages else ""
    if not incomplete and _looks_no_openings(page_html if "page_html" in locals() else first_html):
        return [], pages or [start_url], False
    raise ValueError("HTML career page did not expose parseable public job postings.")


def _jobs_from_json_ld(
    html_text: str,
    source_url: str,
    company: Mapping[str, object],
    *,
    allow_source_url: bool = False,
) -> tuple[list[dict], int]:
    jobs: list[dict] = []
    nodes, malformed = _read_json_ld(html_text)
    nodes_by_id: dict[str, dict] = {}
    for node in nodes:
        node_id = node.get("@id")
        if isinstance(node_id, str):
            nodes_by_id[node_id] = node
    for node in nodes:
        if not _has_type(node, "JobPosting"):
            continue
        try:
            jobs.append(_job_from_json_ld(node, source_url, company, nodes_by_id, allow_source_url=allow_source_url))
        except ValueError:
            malformed += 1
            continue
    return jobs, malformed


def _job_from_detail_html(html_text: str, source_url: str, company: Mapping[str, object]) -> dict:
    jobs, _malformed = _jobs_from_json_ld(html_text, source_url, company, allow_source_url=True)
    if len(jobs) == 1:
        return jobs[0]
    if len(jobs) > 1:
        matching = [job for job in jobs if _preserve_url(job["url"]) == _preserve_url(source_url)]
        if len(matching) == 1:
            return matching[0]
        raise ValueError("HTML detail page contains multiple job postings.")
    title = _extract_title(html_text)
    description = _extract_description(html_text)
    if not _strong_job_page(title, description, html_text):
        raise ValueError("HTML detail page is not a recognizable job posting.")
    return _job(
        source_id=f"html:{source_url}",
        title=title,
        url=source_url,
        location=_extract_location(html_text),
        description=description,
        salary_text=_extract_salary(html_text),
        company_name=_text(company.get("name")),
        requirements=_extract_requirements(html_text),
        raw={"source": "html"},
    )


def _job_from_json_ld(
    node: Mapping[str, object],
    source_url: str,
    company: Mapping[str, object],
    nodes_by_id: Mapping[str, dict],
    *,
    allow_source_url: bool = False,
) -> dict:
    title = _clean_text(_text(node.get("title") or node.get("name")))
    description = _clean_html(_text(node.get("description")))
    if not title or not description:
        raise ValueError("JobPosting is missing a title or description.")
    source_identifier = _text(node.get("identifier"))
    source_url_value = _text(node.get("url")) or _text(node.get("sameAs")) or _text(node.get("applicationContact"))
    if not source_identifier and isinstance(node.get("identifier"), Mapping):
        source_identifier = _text(node["identifier"].get("value"))
    if not source_url_value:
        if not allow_source_url:
            raise ValueError("JobPosting is missing a public URL.")
        source_url_value = source_url
    url = _valid_public_url(
        source_url_value
    )
    return _job(
        source_id=f"html:{source_identifier or url}",
        title=title,
        url=url,
        location=_json_ld_location(node, nodes_by_id),
        description=description,
        salary_text=_json_ld_salary(node),
        company_name=_json_ld_company(node, nodes_by_id) or _text(company.get("name")),
        requirements=[],
        raw=dict(node),
    )


def _job(
    *,
    source_id: str,
    title: str,
    url: str,
    location: str,
    description: str,
    salary_text: str,
    company_name: str,
    requirements: list[str],
    raw: dict,
) -> dict:
    if not title:
        raise ValueError("HTML career job is missing a title.")
    if not description:
        raise ValueError("HTML career job is missing a description.")
    return {
        "id": source_id,
        "title": title,
        "url": url,
        "location": location,
        "description": description,
        "salary_text": salary_text,
        "requirements": requirements,
        "company": company_name,
        "raw": raw,
    }


def _read_json_ld(html_text: str) -> tuple[list[dict], int]:
    nodes: list[dict] = []
    malformed = 0
    for match in _JSON_LD_RE.finditer(html_text):
        raw_data = match.group("data").strip()
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            try:
                data = json.loads(html.unescape(raw_data))
            except json.JSONDecodeError:
                malformed += 1
                continue
        _collect_nodes(data, nodes)
    return nodes, malformed


def _collect_nodes(value: object, nodes: list[dict]) -> None:
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            _collect_nodes(child, nodes)
    elif isinstance(value, list):
        for child in value:
            _collect_nodes(child, nodes)


def _has_type(node: Mapping[str, object], expected: str) -> bool:
    node_type = node.get("@type")
    return node_type == expected or (isinstance(node_type, list) and expected in node_type)


def _detail_links(html_text: str, page_url: str, start_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    def add_candidate(candidate: str, signal: str) -> None:
        if not candidate or candidate.startswith(("#", "mailto:", "tel:", "javascript:")):
            return
        try:
            url = _preserve_url(urljoin(page_url, html.unescape(candidate)))
        except ValueError:
            return
        if url in seen or _looks_asset_url(url) or not _valid_detail_url(url):
            return
        if not any(marker in signal.lower() for marker in _JOB_LINK_MARKERS):
            return
        if not _same_site_or_known_ats(url, start_url):
            return
        seen.add(url)
        urls.append(url)

    for match in _ANCHOR_RE.finditer(html_text):
        attrs = _attrs(match.group("attrs"))
        href = _text(attrs.get("href"))
        signal = " ".join([href, _clean_text(match.group("body")), " ".join(attrs.values())]).lower()
        add_candidate(href, signal)
    for match in _EMBEDDED_URL_RE.finditer(html_text):
        raw = match.group(0).replace("\\/", "/")
        add_candidate(raw, raw)
    return urls


def _same_site_or_known_ats(url: str, start_url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    start_host = (urlparse(start_url).hostname or "").lower()
    if host == start_host or host.endswith(f".{start_host}"):
        return True
    return any(host == ats_host or host.endswith(f".{ats_host}") for ats_host in _KNOWN_JOB_HOSTS)


def _next_url(html_text: str, page_url: str, start_url: str) -> str:
    match = _NEXT_RE.search(html_text)
    if not match:
        return ""
    url = _preserve_url(urljoin(page_url, html.unescape(match.group("url"))))
    if _same_site_or_known_ats(url, start_url):
        return url
    return ""


def _attrs(value: str) -> dict[str, str]:
    return {match.group("name").lower(): html.unescape(match.group("value")) for match in _ATTR_RE.finditer(value)}


def _strong_job_page(title: str, description: str, html_text: str) -> bool:
    if len(description) < _DESCRIPTION_MIN_CHARS:
        return False
    haystack = f"{title} {html_text}".lower()
    title_signal = any(
        word in title.lower()
        for word in (
            "engineer",
            "developer",
            "head",
            "analyst",
            "lead",
            "manager",
            "designer",
            "architect",
            "consultant",
            "marketer",
            "specialist",
            "support",
        )
    )
    apply_signal = any(
        marker in haystack
        for marker in (
            "apply now",
            "apply for this job",
            "apply for this role",
            "apply for this position",
            "apply on ",
            "application form",
        )
    )
    return title_signal and apply_signal


def _extract_title(html_text: str) -> str:
    for pattern in (
        r"<h1[^>]*>(.*?)</h1>",
        r'''<meta\s+property=["']og:title["']\s+content=["']([^"']+)["']''',
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_text(match.group(1))
    return ""


def _extract_description(html_text: str) -> str:
    for pattern in (
        r'''<(?:section|div|article|main)\b[^>]*(?:data-testid|class|id)=["'][^"']*(?:job-description|description|job-content|posting|content)[^"']*["'][^>]*>(.*?)</(?:section|div|article|main)>''',
        r"<section[^>]*>(.*)</section>",
        r"<article[^>]*>(.*)</article>",
        r"<main[^>]*>(.*)</main>",
    ):
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        if match:
            text = _clean_html(_drop_non_content_html(match.group(1)))
            if len(text) >= _DESCRIPTION_MIN_CHARS:
                return text
    marker_text = _description_from_heading_window(html_text)
    if marker_text:
        return marker_text
    return ""


def _extract_location(html_text: str) -> str:
    for pattern in (
        r'''<[^>]+(?:data-testid|class|id)=["'][^"']*(?:location|job-location)[^"']*["'][^>]*>(.*?)</[^>]+>''',
        r'''<meta\s+name=["']job-location["']\s+content=["']([^"']+)["']''',
    ):
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_text(match.group(1))
    return ""


def _extract_salary(html_text: str) -> str:
    for pattern in (
        r'''<[^>]+(?:data-testid|class|id)=["'][^"']*(?:salary|compensation)[^"']*["'][^>]*>(.*?)</[^>]+>''',
        r'''<meta\s+name=["']salary["']\s+content=["']([^"']+)["']''',
    ):
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_text(match.group(1))
    return ""


def _extract_requirements(html_text: str) -> list[str]:
    requirements: list[str] = []
    for match in re.finditer(r"<li[^>]*>(.*?)</li>", html_text, re.IGNORECASE | re.DOTALL):
        text = _clean_text(match.group(1))
        if text and 8 <= len(text) <= 200 and text not in requirements:
            requirements.append(text)
    return requirements[:20]


def _json_ld_company(node: Mapping[str, object], nodes_by_id: Mapping[str, dict]) -> str:
    organization = _resolve_reference(node.get("hiringOrganization"), nodes_by_id)
    return _clean_text(_text(organization.get("name"))) if isinstance(organization, Mapping) else ""


def _json_ld_location(node: Mapping[str, object], nodes_by_id: Mapping[str, dict]) -> str:
    if _text(node.get("jobLocationType")).upper() == "TELECOMMUTE":
        return "Remote"
    locations = node.get("jobLocation", [])
    if isinstance(locations, Mapping):
        locations = [locations]
    if not isinstance(locations, list):
        return ""
    parts: list[str] = []
    for item in locations:
        location = _resolve_reference(item, nodes_by_id)
        address = _resolve_reference(location.get("address") if isinstance(location, Mapping) else {}, nodes_by_id)
        if not isinstance(address, Mapping):
            continue
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            value = _clean_text(_text(address.get(key)))
            if value and value not in parts:
                parts.append(value)
    return ", ".join(parts)


def _json_ld_salary(node: Mapping[str, object]) -> str:
    salary = node.get("baseSalary")
    if not isinstance(salary, Mapping):
        return ""
    value = salary.get("value")
    if not isinstance(value, Mapping):
        return ""
    return _join_values([value.get("minValue"), value.get("maxValue"), salary.get("currency"), value.get("unitText")])


def _resolve_reference(value: object, nodes_by_id: Mapping[str, dict]) -> object:
    if isinstance(value, Mapping) and isinstance(value.get("@id"), str):
        return nodes_by_id.get(value["@id"], value)
    return value


def _valid_detail_url(value: str) -> bool:
    if any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password


def _valid_public_url(value: str) -> str:
    if not _valid_detail_url(value):
        raise ValueError("HTML career URL must be a public HTTP or HTTPS URL.")
    return value


def _preserve_url(value: str) -> str:
    return urldefrag(value)[0].rstrip("/")


def _looks_asset_url(value: str) -> bool:
    parsed = urlparse(value)
    path = parsed.path.lower()
    return path.endswith(_ASSET_EXTENSIONS) or "/_next/image" in path or "/cdn-cgi/image/" in path


def _looks_no_openings(html_text: str) -> bool:
    parser = _VisibleText()
    parser.feed(html_text)
    visible = " ".join(parser.text).lower()
    return any(marker in visible for marker in _NO_OPENINGS_MARKERS)


class _VisibleText(HTMLParser):
    """Ignore hidden empty-state templates when recognizing an empty board."""

    _void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        style = re.sub(r"\s+", "", values.get("style") or "").lower()
        hidden = (
            bool(self.stack and self.stack[-1][1]) or tag in {"script", "style", "template", "noscript"}
            or "hidden" in values or values.get("aria-hidden") == "true"
            or "display:none" in style or "visibility:hidden" in style
        )
        if tag not in self._void_tags:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.text.append(data)


def _looks_incomplete_page(html_text: str) -> bool:
    visible = _clean_text(html_text).lower()
    raw = html_text.lower()
    return any(marker in visible or marker in raw for marker in _INCOMPLETE_PAGE_MARKERS)


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _join_values(values: Iterable[object]) -> str:
    parts: list[str] = []
    for value in values:
        text = _clean_text(_text(value))
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts)


def _clean_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _drop_non_content_html(value: str) -> str:
    for tag in ("script", "style", "svg", "nav", "header", "footer", "form", "noscript"):
        value = re.sub(fr"<{tag}\b.*?</{tag}>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    return value


def _description_from_heading_window(html_text: str) -> str:
    visible = _clean_html(_drop_non_content_html(html_text))
    lower = visible.lower()
    starts = [
        lower.find(marker)
        for marker in ("about the role", "about this role", "the role", "what you'll do", "what you will do")
        if lower.find(marker) >= 0
    ]
    if not starts:
        return ""
    start = min(starts)
    end_candidates = [
        lower.find(marker, start + 40)
        for marker in ("apply now", "apply for this", "benefits", "about us", "similar jobs")
        if lower.find(marker, start + 40) > start
    ]
    end = min(end_candidates) if end_candidates else min(len(visible), start + 8000)
    text = visible[start:end].strip()
    return text if len(text) >= _DESCRIPTION_MIN_CHARS else ""


def _clean_text(value: str) -> str:
    return _clean_html(value)
