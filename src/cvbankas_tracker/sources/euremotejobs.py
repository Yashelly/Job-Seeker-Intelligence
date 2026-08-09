from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .generic_html import GenericHtmlJobSource


class EuRemoteJobsSource(GenericHtmlJobSource):
    name = "euremotejobs"
    listing_request_delay_seconds = 15
    vacancy_request_delay_seconds = 15
    base_url = "https://euremotejobs.com/"
    allowed_hosts = ("euremotejobs.com",)
    vacancy_path_patterns = (
        re.compile(r"^/job/[^/?#]+/$"),
    )
    keyword_param = "s"
    page_param = ""
    first_page = 1
    listing_path = "/job-region/remote-jobs-europe/"
    exclude_russia_belarus = True

    _EXCLUDED_PATH_PREFIXES = (
        "category/",
        "company/",
        "feed",
        "feed/",
        "job-category/",
        "page/",
        "privacy-policy/",
        "remote-jobs/",
        "tag/",
        "terms",
        "terms/",
        "wp-",
    )

    def build_listing_url(self, keyword: str | None = None, page: int | None = None) -> str:
        page_value = self.first_page if page is None else page
        base = (
            f"{self.base_url}job-region/remote-jobs-europe/"
            if page_value <= 1
            else f"{self.base_url}job-region/remote-jobs-europe/page/{page_value}/"
        )
        query = f"?{urlencode({'s': keyword})}" if keyword else ""
        return f"{base}{query}"

    def build_paged_url(self, listing_url: str, page: int) -> str:
        parsed = urlparse(listing_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        page_value = self.first_page + page
        region_path = "/job-region/remote-jobs-europe/"
        if parsed.path.startswith(region_path):
            path = region_path if page_value <= 1 else f"{region_path}page/{page_value}/"
        else:
            path = parsed.path if page_value <= 1 else f"/page/{page_value}/"
        return urlunparse(parsed._replace(path=path, query=urlencode(query)))

    def can_handle_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not self._host_allowed(host):
            return False
        path = parsed.path.strip("/")
        if not path or any(path.startswith(prefix) for prefix in self._EXCLUDED_PATH_PREFIXES):
            return False
        return any(pattern.search(parsed.path) for pattern in self.vacancy_path_patterns)

    def _extract_source_id(self, source_url: str, html_text: str) -> str:
        path = urlparse(source_url).path.strip("/")
        if path.startswith("job/"):
            return path.split("/", maxsplit=1)[1].strip("/") or path
        return path or source_url
