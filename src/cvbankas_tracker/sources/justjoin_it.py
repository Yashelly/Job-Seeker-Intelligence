from __future__ import annotations

import re
from urllib.parse import urlencode, urlparse, urlunparse

from .generic_html import GenericHtmlJobSource


class JustJoinItSource(GenericHtmlJobSource):
    name = "justjoin"
    base_url = "https://justjoin.it/"
    allowed_hosts = ("justjoin.it",)
    vacancy_path_patterns = (
        re.compile(r"^/job-offer/[^/?#]+"),
    )
    keyword_param = "keyword"
    page_param = "page"
    first_page = 1
    remote_only = True
    exclude_russia_belarus = True

    def build_listing_url(self, keyword: str | None = None, page: int | None = None) -> str:
        params: dict[str, str] = {"workplace": "remote"}
        if keyword:
            params["keyword"] = keyword
        page_value = self.first_page if page is None else page
        if page_value > 1:
            params["page"] = str(page_value)
        return f"{self.base_url}job-offers/all-locations?{urlencode(params)}"

    def can_handle_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not self._host_allowed(host):
            return False
        return any(pattern.search(parsed.path) for pattern in self.vacancy_path_patterns)

    def _extract_source_id(self, source_url: str, html_text: str) -> str:
        parsed = urlparse(source_url)
        path = parsed.path.strip("/")
        return path.rsplit("/", maxsplit=1)[-1] or path or source_url

    def _canonical_vacancy_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse(parsed._replace(query="", fragment=""))
