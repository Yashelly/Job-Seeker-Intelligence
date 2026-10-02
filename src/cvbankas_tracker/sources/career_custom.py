"""Public collectors for career sites with small, vendor-specific protocols.

These providers do not offer a reusable ATS endpoint, but their public career
applications expose stable, read-only data that their own pages consume.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Mapping
from urllib.parse import quote, unquote, urljoin, urlparse

from .base import CollectionCancelledError

_ELASTIC_ORIGIN = "https://jobs.elastic.co"
_ELASTIC_LIST_URL = f"{_ELASTIC_ORIGIN}/api/appSearch"
_ELASTIC_PAGE_SIZE = 100
_DESCRIPTION_MIN_CHARS = 80
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", re.IGNORECASE | re.DOTALL)
_DATA_PAGE_RE = re.compile(r'<div\s+id=["\']app["\'][^>]*\bdata-page=["\'](?P<data>.*?)["\']', re.IGNORECASE | re.DOTALL)
_OPEN_ROLES_RE = re.compile(
    r"(?P<url>(?:https?://(?:www\.)?todoist\.com)?/_server-islands/OpenRoles\?[^\"'<>\s]+)",
    re.IGNORECASE,
)
_HREF_RE = re.compile(r'''\bhref\s*=\s*["'](?P<url>[^"']+)["']''', re.IGNORECASE)
_H1_RE = re.compile(r"<h1\b[^>]*>(?P<value>.*?)</h1>", re.IGNORECASE | re.DOTALL)


def collect_custom(
    provider: str,
    company: Mapping[str, object],
    max_pages: int,
    fetch_json: Callable[..., object],
    fetch_html: Callable[[str], str],
    get_cookie: Callable[[str, str], str | None],
) -> tuple[list[dict], list[str], bool]:
    """Collect a known custom public career provider.

    ``max_pages`` limits listing pages only. Job-detail requests are necessary
    to obtain full descriptions and are deliberately not counted against it.
    """
    normalized = _text(provider).lower()
    max_pages = max(1, int(max_pages or 1))
    if normalized == "elastic_custom":
        return _collect_elastic(company, max_pages, fetch_json, fetch_html, get_cookie)
    if normalized == "astro_server_island":
        return _collect_doist(company, fetch_html)
    raise ValueError(f"Unsupported custom career provider: {provider}")


def _collect_elastic(
    company: Mapping[str, object],
    max_pages: int,
    fetch_json: Callable[..., object],
    fetch_html: Callable[[str], str],
    get_cookie: Callable[[str, str], str | None],
) -> tuple[list[dict], list[str], bool]:
    group_id = _positive_int(company.get("ats_token"), "Elastic requires a numeric group id.")
    pages = [_ELASTIC_ORIGIN + "/"]
    # The GET establishes the current Laravel XSRF cookie in the injected jar.
    fetch_html(pages[0])
    token = get_cookie("XSRF-TOKEN", "jobs.elastic.co")
    if not token:
        raise ValueError("Elastic careers site did not provide an XSRF-TOKEN cookie.")
    token = unquote(token)
    headers = {"X-XSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}

    jobs: list[dict] = []
    total: int | None = None
    incomplete = False
    for page_index in range(max_pages):
        payload = _elastic_payload(group_id, page_index + 1)
        response = fetch_json(_ELASTIC_LIST_URL, payload=payload, headers=headers)
        pages.append(_ELASTIC_LIST_URL)
        results, response_total = _elastic_results(response)
        if total is None:
            total = response_total
        elif total != response_total:
            raise ValueError("Elastic listing total changed while collecting pages.")
        for item in results:
            try:
                detail_url, title, location, source_id = _elastic_listing_fields(item)
                detail_html = fetch_html(detail_url)
                pages.append(detail_url)
                detail = _elastic_detail(detail_html, detail_url)
                description = _clean_html(_text(detail.get("content")))
                if len(description) < _DESCRIPTION_MIN_CHARS:
                    raise ValueError("Elastic job detail is missing a full description.")
                jobs.append(_job(
                    raw=detail,
                    source_id=f"elastic:{source_id}",
                    title=_text(detail.get("title")) or title,
                    url=detail_url,
                    location=_text(detail.get("location")) or location,
                    description=description,
                    company_name=_text(company.get("name")),
                ))
            except CollectionCancelledError:
                raise
            except (ValueError, OSError):
                incomplete = True
        if total is None:
            raise ValueError("Elastic response did not report a total result count.")
        if (page_index + 1) * _ELASTIC_PAGE_SIZE >= total:
            return jobs, pages, incomplete
        if not results:
            raise ValueError("Elastic listings ended before the reported total.")
    return jobs, pages, bool(total and len(jobs) < total) or incomplete


def _elastic_payload(group_id: int, current_page: int) -> dict:
    return {
        "query": "",
        "search_fields": {
            "title": {}, "location": {}, "category": {}, "city_filter": {},
            "country_filter": {}, "remote_locations": {}, "hybrid_locations": {},
        },
        "result_fields": {
            "title": {"raw": {}}, "location": {"raw": {}}, "remote_locations": {"raw": {}},
            "hybrid_locations": {"raw": {}}, "job_type": {"raw": {}},
            "content": {"snippet": {"fallback": True}}, "category": {"raw": {}},
            "country_filter": {"raw": {}}, "city_filter": {"raw": {}}, "url": {"raw": {}},
        },
        "precision": 2,
        "page": {"size": _ELASTIC_PAGE_SIZE, "current": current_page},
        "filters": {"all": [{"any": [{"group_id": group_id}]}, {"any": [{"live": 1}]}]},
        "facets": {
            "category": {"type": "value", "size": 30}, "location": {"type": "value", "size": 30},
            "job_type": {"type": "value", "size": 30},
        },
        "sort": [{"_score": "desc"}, {"created_at": "desc"}],
    }


def _elastic_results(response: object) -> tuple[list[Mapping[str, object]], int]:
    if not isinstance(response, Mapping):
        raise ValueError("Elastic response was not an object.")
    results = response.get("results")
    meta = response.get("meta")
    page = meta.get("page") if isinstance(meta, Mapping) else None
    total = page.get("total_results") if isinstance(page, Mapping) else None
    if not isinstance(results, list) or not isinstance(total, int) or total < 0:
        raise ValueError("Elastic response did not contain results and a total.")
    if not all(isinstance(item, Mapping) for item in results):
        raise ValueError("Elastic response contained malformed listing entries.")
    return list(results), total


def _elastic_listing_fields(item: Mapping[str, object]) -> tuple[str, str, str, str]:
    relative = _nested_text(item, "url", "raw")
    if not relative or relative.startswith(("/", "http:")) or ".." in relative.split("/"):
        raise ValueError("Elastic listing is missing a valid detail path.")
    detail_url = f"{_ELASTIC_ORIGIN}/jobs/{quote(relative, safe='/') }"
    title = _nested_text(item, "title", "raw")
    source_id = _nested_text(item, "id", "raw") or _nested_text(item, "_meta", "id") or relative
    if not title:
        raise ValueError("Elastic listing is missing a title.")
    return detail_url, title, _nested_text(item, "location", "raw"), source_id


def _elastic_detail(html_text: str, detail_url: str) -> Mapping[str, object]:
    match = _DATA_PAGE_RE.search(html_text)
    if not match:
        raise ValueError("Elastic job detail did not expose its page data.")
    try:
        payload = json.loads(html.unescape(match.group("data")))
    except json.JSONDecodeError as error:
        raise ValueError("Elastic job detail page data was malformed.") from error
    props = payload.get("props") if isinstance(payload, Mapping) else None
    job = props.get("job_object") if isinstance(props, Mapping) else None
    if not isinstance(job, Mapping):
        raise ValueError("Elastic job detail did not contain a job object.")
    return job


def _collect_doist(
    company: Mapping[str, object],
    fetch_html: Callable[[str], str],
) -> tuple[list[dict], list[str], bool]:
    career_url = _text(company.get("career_url") or company.get("ats_url")) or "https://todoist.com/careers"
    if not _is_todoist_url(career_url):
        raise ValueError("Doist requires an official todoist.com career URL.")
    page_html = fetch_html(career_url)
    match = _OPEN_ROLES_RE.search(html.unescape(page_html))
    if not match:
        raise ValueError("Doist careers page did not expose the current OpenRoles server-island URL.")
    fragment_url = urljoin(career_url, html.unescape(match.group("url")))
    if not _is_todoist_url(fragment_url) or urlparse(fragment_url).path != "/_server-islands/OpenRoles":
        raise ValueError("Doist OpenRoles server-island URL was invalid.")
    fragment = fetch_html(fragment_url)
    pages = [career_url, fragment_url]
    detail_urls = _doist_detail_urls(fragment, career_url)
    if not detail_urls:
        if _looks_empty(fragment):
            return [], pages, False
        raise ValueError("Doist OpenRoles fragment did not expose public job links.")

    jobs: list[dict] = []
    incomplete = False
    for detail_url in detail_urls:
        try:
            detail_html = fetch_html(detail_url)
            pages.append(detail_url)
            title = _html_text(_first_group(_H1_RE, detail_html))
            description = _description_from_doist_detail(detail_html)
            if not title or len(description) < _DESCRIPTION_MIN_CHARS:
                raise ValueError("Doist job detail is missing a title or full description.")
            slug = urlparse(detail_url).path.rsplit("/", 1)[-1]
            jobs.append(_job(
                raw={"provider": "astro_server_island", "slug": slug},
                source_id=f"doist:{slug}", title=title, url=detail_url, location="",
                description=description, company_name=_text(company.get("name")),
            ))
        except CollectionCancelledError:
            raise
        except (ValueError, OSError):
            incomplete = True
    return jobs, pages, incomplete


def _doist_detail_urls(fragment: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for match in _HREF_RE.finditer(html.unescape(fragment)):
        candidate = urljoin(base_url, match.group("url"))
        parsed = urlparse(candidate)
        if not _is_todoist_url(candidate) or not parsed.path.startswith("/careers/"):
            continue
        slug = parsed.path.removeprefix("/careers/")
        if not slug or "/" in slug or slug == "":
            continue
        if candidate not in urls:
            urls.append(candidate)
    return urls


def _description_from_doist_detail(html_text: str) -> str:
    main_match = re.search(r"<main\b[^>]*>(?P<value>.*?)</main>", html_text, re.IGNORECASE | re.DOTALL)
    value = main_match.group("value") if main_match else html_text
    return _html_text(value)


def _job(*, raw: Mapping[str, object], source_id: str, title: str, url: str, location: str, description: str, company_name: str) -> dict:
    return {
        "id": source_id, "title": title, "url": url, "location": location,
        "description": description, "salary_text": "", "requirements": [],
        "company": company_name, "raw": dict(raw),
    }


def _positive_int(value: object, message: str) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(message) from error
    if number <= 0:
        raise ValueError(message)
    return number


def _nested_text(value: Mapping[str, object], key: str, child: str) -> str:
    nested = value.get(key)
    return _text(nested.get(child)) if isinstance(nested, Mapping) else ""


def _is_todoist_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in {"todoist.com", "www.todoist.com"}


def _first_group(pattern: re.Pattern[str], value: str) -> str:
    match = pattern.search(value)
    return match.group("value") if match else ""


def _looks_empty(value: str) -> bool:
    return bool(re.search(r"\b(?:no\s+(?:open\s+)?roles|no\s+openings)\b", _html_text(value), re.IGNORECASE))


def _html_text(value: str) -> str:
    return _clean_html(value)


def _clean_html(value: str) -> str:
    value = html.unescape(value or "")
    value = _SCRIPT_STYLE_RE.sub(" ", value)
    value = _TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def _text(value: object) -> str:
    return " ".join(str(value or "").split())
