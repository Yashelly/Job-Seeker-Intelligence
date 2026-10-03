from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_EY_LISTING = "https://careers.ey.com/ey/search/?q=&locationsearch=Lithuania"
_VOID_TAGS = {"area", "br", "hr", "img", "input", "link", "meta", "source"}


class _EYDescription(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.depth:
            if tag == "br":
                self.parts.append(" ")
            elif tag not in _VOID_TAGS:
                self.depth += 1
            return
        if tag == "span" and "jobdescription" in (dict(attrs).get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth and tag not in _VOID_TAGS:
            self.depth -= 1
            if self.depth:
                self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)


def _text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _ey_job(markup: str, url: str) -> dict | None:
    title_match = re.search(r"<h1\b[^>]*>(.*?)</h1>", markup, re.I | re.S)
    if title_match is None:
        return None
    parser = _EYDescription()
    parser.feed(markup)
    description = " ".join(" ".join(parser.parts).split())
    if len(description) < 80:
        return None
    location_match = re.search(
        r"<[^>]*class=[\"'][^\"']*jobLocation[^\"']*[\"'][^>]*>(.*?)</[^>]+>",
        markup,
        re.I | re.S,
    )
    return {
        "id": f"html:{url}",
        "url": url,
        "title": _text(title_match.group(1)),
        "description": description,
        "location": _text(location_match.group(1)) if location_match else "Lithuania",
    }


def collect_targeted_html(company: dict, max_pages: int, fetch_html):
    if (company.get("name") or "").casefold() != "ey":
        return None

    pages: list[str] = []
    role_urls: list[str] = []
    seen: set[str] = set()
    incomplete = False
    total = None
    for page_index in range(max_pages):
        listing_url = _EY_LISTING
        if page_index:
            listing_url += f"&startrow={page_index * 25 + 1}"
        try:
            markup = fetch_html(listing_url)
        except (OSError, ValueError):
            if page_index == 0:
                raise
            incomplete = True
            break
        pages.append(listing_url)
        total_match = re.search(r"Results\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)", markup, re.I)
        if total_match:
            total = int(total_match.group(1).replace(",", ""))
        links = re.findall(r"href=[\"']([^\"']*/ey/job/[^\"']+)", markup, re.I)
        new_urls = []
        for link in links:
            url = urljoin(listing_url, html.unescape(link))
            if url not in seen:
                seen.add(url)
                new_urls.append(url)
        if not new_urls:
            if total and len(role_urls) < total:
                incomplete = True
            break
        role_urls.extend(new_urls)
        if total is not None and len(role_urls) >= total:
            break
    else:
        if total is None or len(role_urls) < total:
            incomplete = True

    if not role_urls:
        raise ValueError("EY career search did not expose public job postings.")

    jobs: list[dict] = []
    for url in role_urls:
        try:
            markup = fetch_html(url)
        except (OSError, ValueError):
            incomplete = True
            continue
        pages.append(url)
        job = _ey_job(markup, url)
        if job is None:
            incomplete = True
            continue
        jobs.append(job)
    if not jobs:
        raise ValueError("EY job details did not expose parseable public postings.")
    return jobs, pages, incomplete
