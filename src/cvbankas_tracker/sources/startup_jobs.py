from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin, urlparse

from .generic_html import GenericHtmlJobSource


class StartupJobsSource(GenericHtmlJobSource):
    name = "startup_jobs"
    base_url = "https://startup.jobs/"
    allowed_hosts = ("startup.jobs",)
    vacancy_path_patterns = (
        re.compile(r"^/[^/?#]+-\d+$"),
    )
    keyword_param = "q"
    page_param = "page"
    first_page = 1
    remote_only = True
    exclude_russia_belarus = True

    def build_listing_url(self, keyword: str | None = None, page: int | None = None) -> str:
        params: dict[str, str] = {
            "remote": "true",
            "w": "remote",
        }
        if keyword:
            params[self.keyword_param] = keyword
        page_value = self.first_page if page is None else page
        if page_value > 1:
            params["page"] = str(page_value)
        return f"{self.base_url}remote-jobs?{urlencode(params)}"

    def can_handle_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not self._host_allowed(host):
            return False
        return any(pattern.search(parsed.path) for pattern in self.vacancy_path_patterns)

    def _canonical_vacancy_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urljoin(self.base_url, parsed.path.strip("/"))
