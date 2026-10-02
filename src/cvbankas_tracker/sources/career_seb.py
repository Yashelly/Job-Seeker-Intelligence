"""Collect public SEB career listings through its documented site widget API."""
from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from urllib.parse import urljoin, urlparse

from .base import CollectionCancelledError

_ORIGIN = "https://sebgroup.com"
_LIST_URL = f"{_ORIGIN}/api/lever-v2/list"
_DETAIL_PREFIX = "/career/find-your-new-job/our-vacant-positions/"
_PAGE_SIZE = 100
_DESCRIPTION_MIN_CHARS = 120
_DETAIL_START_RE = re.compile(
    r'<div\b[^>]*\bclass=["\'][^"\']*\bpw-lever-description__details\b[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", re.IGNORECASE | re.DOTALL)


def collect_seb(
    company: Mapping[str, object],
    max_pages: int,
    fetch_json: Callable[..., object],
    fetch_html: Callable[[str], str],
) -> tuple[list[dict], list[str], bool]:
    """Collect SEB's paginated listing and each official job-description page.

    ``max_pages`` limits only POST listing requests. Detail pages are required
    to obtain descriptions and are therefore fetched for every returned role.
    A failed detail remains visible through the incomplete result while other
    vacancies are retained.
    """
    del company  # The public SEB endpoint has a single shared board.
    max_pages = max(1, int(max_pages or 1))
    pages: list[str] = []
    jobs: list[dict] = []
    total: int | None = None
    incomplete = False

    for page_index in range(max_pages):
        payload = {
            "hits": _PAGE_SIZE,
            "offset": page_index * _PAGE_SIZE,
            "language": "en-GB",
            "locationFilter": [],
            "teamFilter": [],
            "commitmentFilter": [],
            "departmentFilter": [],
            "query": "",
        }
        response = fetch_json(_LIST_URL, payload=payload, headers={"Accept": "application/json"})
        pages.append(_LIST_URL)
        items, response_total = _listing_items(response)
        if total is None:
            total = response_total
        elif response_total != total:
            raise ValueError("SEB listing total changed while collecting pages.")
        if not items and len(jobs) < total:
            raise ValueError("SEB listings ended before the reported total.")

        for item in items:
            try:
                detail_url, source_id, title, location = _listing_fields(item)
                detail_html = fetch_html(detail_url)
                pages.append(detail_url)
                description = _description_from_detail(detail_html)
                jobs.append({
                    "id": f"seb:{source_id}",
                    "title": title,
                    "url": detail_url,
                    "location": location,
                    "description": description,
                    "salary_text": "",
                    "requirements": [],
                    "company": "SEB",
                    "raw": dict(item),
                })
            except CollectionCancelledError:
                raise
            except (OSError, ValueError, TypeError, KeyError):
                incomplete = True

        if page_index * _PAGE_SIZE + len(items) >= total:
            return jobs, pages, incomplete
        if len(items) < _PAGE_SIZE:
            raise ValueError("SEB listing returned fewer jobs than its reported total.")

    return jobs, pages, True


def _listing_items(response: object) -> tuple[list[Mapping[str, object]], int]:
    if not isinstance(response, Mapping):
        raise ValueError("SEB listing response was not an object.")
    items = response.get("leverListItems")
    total = response.get("totalHits")
    if not isinstance(items, list) or not isinstance(total, int) or total < 0:
        raise ValueError("SEB listing response did not include jobs and a total.")
    if not all(isinstance(item, Mapping) for item in items):
        raise ValueError("SEB listing response contained a malformed job.")
    return list(items), total


def _listing_fields(item: Mapping[str, object]) -> tuple[str, str, str, str]:
    relative_url = _text(item.get("url"))
    title = _text(item.get("title"))
    if not title:
        raise ValueError("SEB listing job is missing a title.")
    detail_url = _detail_url(relative_url)
    source_id = urlparse(detail_url).path.rsplit("/", 1)[-1]
    if not source_id:
        raise ValueError("SEB listing job is missing a stable detail identifier.")
    return detail_url, source_id, title, _text(item.get("location"))


def _detail_url(relative_url: str) -> str:
    if not relative_url.startswith(_DETAIL_PREFIX) or ".." in relative_url.split("/"):
        raise ValueError("SEB listing job has an invalid detail path.")
    url = urljoin(_ORIGIN, relative_url)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "sebgroup.com" or not parsed.path.startswith(_DETAIL_PREFIX):
        raise ValueError("SEB listing job resolved outside the official career site.")
    return url


def _description_from_detail(detail_html: str) -> str:
    match = _DETAIL_START_RE.search(detail_html)
    if not match:
        raise ValueError("SEB job detail did not contain its description component.")
    # The server-rendered component ends before the site footer. Using that
    # boundary avoids treating global navigation or footer text as a vacancy.
    footer_start = detail_html.lower().find("<footer", match.end())
    content = detail_html[match.end():footer_start if footer_start >= 0 else len(detail_html)]
    description = _clean_html(content)
    if len(description) < _DESCRIPTION_MIN_CHARS:
        raise ValueError("SEB job detail did not contain a full description.")
    return description


def _clean_html(value: str) -> str:
    value = html.unescape(value or "")
    value = _SCRIPT_STYLE_RE.sub(" ", value)
    value = _TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def _text(value: object) -> str:
    return " ".join(str(value or "").split())
