from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import urlopen


def _is_cvbankas_host(url: str) -> bool:
    """True only when ``url``'s host is cvbankas.lt or a real subdomain of it.

    A substring test (``"cvbankas.lt" in url``) is unsafe: a hostile listing page
    could smuggle an absolute link like ``https://cvbankas.lt.evil.example/1-9``
    that passes it and then gets fetched/analyzed (audit finding #8). Parsing the
    host and matching exact/subdomain closes that hole.
    """
    host = (urlparse(url).hostname or "").lower()
    return host == "cvbankas.lt" or host.endswith(".cvbankas.lt")


class CvbankasCollector:
    _REMOTE_LISTING_PATH = "/darbas-darbas-namuose"
    _LISTING_LINK_PATTERN = re.compile(
        r'<a[^>]*class="[^"]*\blist_a\b[^"]*"[^>]*href="(?P<url>[^"]+)"',
        re.IGNORECASE,
    )
    _GENERIC_LINK_PATTERN = re.compile(r'href="(?P<url>[^"]+)"', re.IGNORECASE)

    def build_listing_url(self, keyword: str | None = None, page: int = 1) -> str:
        base_url = f"https://www.cvbankas.lt{self._REMOTE_LISTING_PATH}"
        params: dict[str, str] = {}
        if keyword:
            params["keyw"] = keyword
        if page > 1:
            params["page"] = str(page)
        if not params:
            return base_url
        return f"{base_url}?{urlencode(params)}"

    def build_paged_url(self, listing_url: str, page: int) -> str:
        if page <= 1:
            return listing_url

        parsed = urlparse(listing_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = str(page)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def collect_listing_urls(self, listing_page_html: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        matches = list(self._LISTING_LINK_PATTERN.finditer(listing_page_html))
        if not matches:
            matches = list(self._GENERIC_LINK_PATTERN.finditer(listing_page_html))

        for match in matches:
            candidate = urljoin("https://www.cvbankas.lt/", match.group("url").strip())
            if not _is_cvbankas_host(candidate):
                continue
            if not re.search(r"/1-\d+", candidate):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)

        return urls

    def collect_listing_urls_from_pages(
        self,
        *,
        keyword: str | None = None,
        listing_url: str | None = None,
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        if max_pages < 1:
            max_pages = 1

        seed_url = self._ensure_remote_listing_url(listing_url) if listing_url else self.build_listing_url(keyword=keyword)
        page_urls: list[str] = []
        collected_urls: list[str] = []
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            page_url = self.build_paged_url(seed_url, page)
            if before_listing_fetch is not None:
                before_listing_fetch(page_url)
            listing_html = self.fetch_page(page_url)
            page_urls.append(page_url)
            page_vacancy_urls = self.collect_listing_urls(listing_html)
            if not page_vacancy_urls:
                break
            stop = False
            added_on_page = False
            for vacancy_url in page_vacancy_urls:
                if stop_at_vacancy is not None and stop_at_vacancy(vacancy_url):
                    stop = True
                    break
                if vacancy_url in seen:
                    continue
                seen.add(vacancy_url)
                collected_urls.append(vacancy_url)
                added_on_page = True
            if stop or not added_on_page:
                break

        return collected_urls, page_urls

    def _ensure_remote_listing_url(self, listing_url: str) -> str:
        parsed = urlparse(listing_url)
        if "cvbankas.lt" not in parsed.netloc.lower():
            return listing_url
        if parsed.path.rstrip("/") == self._REMOTE_LISTING_PATH:
            return listing_url
        return urlunparse(parsed._replace(path=self._REMOTE_LISTING_PATH))

    def fetch_page(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme not in {"http", "https", "file"}:
            raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")

        request_url = url
        request_headers = {}
        if parsed.scheme in {"http", "https"}:
            request_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            }
            from urllib.request import Request

            request_url = Request(url, headers=request_headers)

        with urlopen(request_url, timeout=20) as response:
            content_bytes = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return content_bytes.decode(charset, errors="ignore")
