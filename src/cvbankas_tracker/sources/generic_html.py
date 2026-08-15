from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from ..models import Vacancy
from .browser_fetch import BrowserFetcher


class GenericHtmlJobSource:
    name = "generic"
    base_url = ""
    allowed_hosts: tuple[str, ...] = ()
    vacancy_path_patterns: tuple[re.Pattern[str], ...] = ()
    listing_path = "/"
    keyword_param = "q"
    page_param = "page"
    first_page = 1
    remote_only = False
    exclude_russia_belarus = False
    remote_markers = (
        "remote",
        "work from home",
        "work from anywhere",
        "worldwide remote",
        "fully remote",
        "100% remote",
    )
    excluded_location_markers = (
        "belarus",
        "russia",
        "беларус",
        "белорус",
        "минск",
        "moscow",
        "москва",
        "saint petersburg",
        "st. petersburg",
        "санкт-петербург",
        "spb",
    )

    _GENERIC_LINK_PATTERN = re.compile(r'href="(?P<url>[^"]+)"', re.IGNORECASE)

    def __init__(
        self,
        *,
        fetch_mode: str | None = None,
        browser_headless: bool = True,
        browser_executable_path: str = "",
        browser_locale: str = "en-US",
        browser_wait_ms: int = 2500,
        browser_timeout_ms: int = 30000,
        browser_fresh_context: bool = False,
    ) -> None:
        self.fetch_mode = self._normalize_fetch_mode(fetch_mode or "http")
        self.browser_headless = bool(browser_headless)
        self.browser_executable_path = browser_executable_path
        self.browser_locale = browser_locale
        self.browser_wait_ms = int(browser_wait_ms)
        self.browser_timeout_ms = int(browser_timeout_ms)
        self.browser_fresh_context = bool(browser_fresh_context)
        self._browser: BrowserFetcher | None = None

    @classmethod
    def from_options(cls, options: dict | None = None) -> GenericHtmlJobSource:
        options = options or {}
        return cls(
            fetch_mode=str(options.get("fetch_mode", options.get("mode", "http"))),
            browser_headless=bool(options.get("browser_headless", True)),
            browser_executable_path=str(options.get("browser_executable_path", "")),
            browser_locale=str(options.get("browser_locale", "en-US")),
            browser_wait_ms=int(options.get("browser_wait_ms", 2500)),
            browser_timeout_ms=int(options.get("browser_timeout_ms", 30000)),
            browser_fresh_context=bool(options.get("browser_fresh_context", False)),
        )

    @staticmethod
    def _normalize_fetch_mode(value: str) -> str:
        normalized = str(value or "http").strip().lower()
        if normalized in {"1", "true", "yes", "on", "browser", "playwright"}:
            return "browser"
        if normalized in {"0", "false", "no", "off", "http", "html", "urllib"}:
            return "http"
        raise ValueError(f"Unsupported fetch mode: {value}")

    def _uses_browser(self) -> bool:
        return getattr(self, "fetch_mode", "http") == "browser"

    def _browser_fetcher(self) -> BrowserFetcher:
        if self._browser is None:
            self._browser = BrowserFetcher(
                headless=self.browser_headless,
                executable_path=self.browser_executable_path,
                locale=self.browser_locale,
                wait_ms=self.browser_wait_ms,
                timeout_ms=self.browser_timeout_ms,
                fresh_context=self.browser_fresh_context,
            )
        return self._browser

    def close(self) -> None:
        if getattr(self, "_browser", None) is not None:
            self._browser.close()
            self._browser = None

    def build_listing_url(self, keyword: str | None = None, page: int | None = None) -> str:
        page_value = self.first_page if page is None else page
        parsed = urlparse(urljoin(self.base_url, self.listing_path))
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if keyword:
            query[self.keyword_param] = keyword
        if self.page_param:
            query[self.page_param] = str(page_value)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def build_paged_url(self, listing_url: str, page: int) -> str:
        parsed = urlparse(listing_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if self.page_param:
            query[self.page_param] = str(self.first_page + page)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
    ) -> tuple[list[str], list[str]]:
        if max_pages < 1:
            max_pages = 1

        seed_url = listing_url or self.build_listing_url(keyword=keyword)
        page_urls = [self.build_paged_url(seed_url, page) for page in range(max_pages)]
        collected_urls: list[str] = []
        seen: set[str] = set()

        for page_url in page_urls:
            if before_listing_fetch is not None:
                before_listing_fetch(page_url)
            listing_html = self.fetch_vacancy_page(page_url)
            page_vacancy_urls = self.collect_listing_urls(listing_html)
            if not page_vacancy_urls and self._looks_blocked(listing_html):
                if collected_urls:
                    break
                raise ValueError(f"{self.name} listing page is not publicly accessible.")
            for vacancy_url in page_vacancy_urls:
                if vacancy_url in seen:
                    continue
                seen.add(vacancy_url)
                collected_urls.append(vacancy_url)

        return collected_urls, page_urls

    def collect_listing_urls(self, listing_page_html: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for match in self._GENERIC_LINK_PATTERN.finditer(listing_page_html):
            candidate = urljoin(self.base_url, html.unescape(match.group("url")).strip())
            if not self.can_handle_url(candidate):
                continue
            candidate = self._canonical_vacancy_url(candidate)
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
        return urls

    def fetch_vacancy_page(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme not in {"http", "https", "file"}:
            raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")

        if self._uses_browser() and parsed.scheme in {"http", "https"}:
            return self._browser_fetcher().fetch_html(url)

        request_url: str | Request = url
        if parsed.scheme in {"http", "https"}:
            request_url = Request(
                url,
                headers={
                    "User-Agent": os.getenv("JOB_SEEKER_USER_AGENT")
                    or (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en,ru;q=0.9,lt;q=0.8",
                },
            )

        with urlopen(request_url, timeout=20) as response:  # noqa: S310 - configured job source
            content_bytes = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return content_bytes.decode(charset, errors="ignore")

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        if self._looks_blocked(html_text):
            raise ValueError(f"{self.name} HTML page is not publicly accessible.")

        structured = self._extract_json_ld(html_text)
        title = self._extract_title(html_text) or self._clean_text(str(structured.get("title", "")))
        description = self._extract_description(html_text) or self._clean_text(
            str(structured.get("description", ""))
        )

        vacancy = Vacancy(
            source_name=self.name,
            source_id=self._extract_source_id(source_url, html_text),
            source_url=self._canonical_vacancy_url(source_url),
            title=title,
            company=self._extract_company(html_text) or self._extract_json_ld_company(structured),
            location=self._extract_location(html_text) or self._extract_json_ld_location(structured),
            salary_text=self._extract_salary(html_text) or self._extract_json_ld_salary(structured),
            requirements=self._extract_skills(html_text),
            responsibilities=[description] if description else [],
            raw_text=html_text,
        )
        if self.remote_only and not self._looks_remote(vacancy, html_text):
            raise ValueError(f"{self.name} vacancy is not marked as remote.")
        if self.exclude_russia_belarus and self._looks_excluded_location(vacancy.location):
            raise ValueError(f"{self.name} vacancy is located in Russia or Belarus.")
        return vacancy

    def can_handle_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        return self._host_allowed(host) and any(
            pattern.search(parsed.path) for pattern in self.vacancy_path_patterns
        )

    def _host_allowed(self, host: str) -> bool:
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in self.allowed_hosts)

    def _canonical_vacancy_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse(parsed._replace(query="", fragment=""))

    def _extract_source_id(self, source_url: str, html_text: str) -> str:
        parsed = urlparse(source_url)
        path = parsed.path.strip("/")
        return path or source_url

    def _extract_title(self, html_text: str) -> str:
        for pattern in (
            r'<h1[^>]*>(.*?)</h1>',
            r'<meta\s+property="og:title"\s+content="([^"]+)"',
            r'<meta\s+name="twitter:title"\s+content="([^"]+)"',
            r"<title[^>]*>(.*?)</title>",
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_company(self, html_text: str) -> str:
        for pattern in (
            r'<[^>]+data-testid="company-name"[^>]*>(.*?)</[^>]+>',
            r'<[^>]+class="[^"]*(?:company|employer)[^"]*"[^>]*>(.*?)</[^>]+>',
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_location(self, html_text: str) -> str:
        for pattern in (
            r'<[^>]+data-testid="location"[^>]*>(.*?)</[^>]+>',
            r'<[^>]+class="[^"]*(?:location|job-location)[^"]*"[^>]*>(.*?)</[^>]+>',
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_salary(self, html_text: str) -> str:
        for pattern in (
            r'<[^>]+data-testid="salary"[^>]*>(.*?)</[^>]+>',
            r'<[^>]+class="[^"]*(?:salary|compensation)[^"]*"[^>]*>(.*?)</[^>]+>',
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_description(self, html_text: str) -> str:
        for pattern in (
            r'<[^>]+data-testid="job-description"[^>]*>(.*?)</(?:section|div|article)>',
            r'<(?:section|div|article)[^>]+class="[^"]*(?:description|content|job-description)[^"]*"[^>]*>(.*?)</(?:section|div|article)>',
            r'<main[^>]*>(.*?)</main>',
            r'<article[^>]*>(.*?)</article>',
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_skills(self, html_text: str) -> list[str]:
        patterns = (
            r'<[^>]+data-testid="skill"[^>]*>(.*?)</[^>]+>',
            r'<[^>]+class="[^"]*(?:skill|tag|badge)[^"]*"[^>]*>(.*?)</[^>]+>',
        )
        skills: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for match in re.finditer(pattern, html_text, re.IGNORECASE | re.DOTALL):
                value = self._clean_text(match.group(1))
                if not value or value in seen or len(value) > 60:
                    continue
                seen.add(value)
                skills.append(value)
        return skills

    def _extract_first(self, pattern: str, html_text: str) -> str:
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        return self._clean_text(match.group(1)) if match else ""

    def _extract_json_ld(self, html_text: str) -> dict:
        for match in re.finditer(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html_text,
            re.IGNORECASE | re.DOTALL,
        ):
            try:
                data = json.loads(html.unescape(match.group(1)).strip())
            except json.JSONDecodeError:
                continue
            found = self._find_job_posting(data)
            if found:
                return found
        return {}

    def _find_job_posting(self, data: object) -> dict:
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                return data
            graph = data.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    found = self._find_job_posting(item)
                    if found:
                        return found
        if isinstance(data, list):
            for item in data:
                found = self._find_job_posting(item)
                if found:
                    return found
        return {}

    def _extract_json_ld_company(self, data: dict) -> str:
        company = data.get("hiringOrganization", {})
        if isinstance(company, dict):
            return self._clean_text(str(company.get("name", "")))
        return ""

    def _extract_json_ld_location(self, data: dict) -> str:
        locations = data.get("jobLocation", [])
        if isinstance(locations, dict):
            locations = [locations]
        if not isinstance(locations, list):
            return ""

        parts: list[str] = []
        for location in locations:
            if not isinstance(location, dict):
                continue
            address = location.get("address", {})
            if not isinstance(address, dict):
                continue
            for key in ("addressLocality", "addressRegion", "addressCountry"):
                value = self._clean_text(str(address.get(key, "")))
                if value and value not in parts:
                    parts.append(value)
        return ", ".join(parts)

    def _extract_json_ld_salary(self, data: dict) -> str:
        salary = data.get("baseSalary", {})
        if not isinstance(salary, dict):
            return ""
        value = salary.get("value", {})
        if not isinstance(value, dict):
            return ""
        min_value = value.get("minValue", "")
        max_value = value.get("maxValue", "")
        unit = value.get("unitText", "")
        currency = salary.get("currency", "")
        if min_value and max_value:
            return self._clean_text(f"{min_value}-{max_value} {currency} {unit}")
        if min_value:
            return self._clean_text(f"from {min_value} {currency} {unit}")
        if max_value:
            return self._clean_text(f"up to {max_value} {currency} {unit}")
        return ""

    def _looks_blocked(self, html_text: str) -> bool:
        # Inspect visible text only: Cloudflare injects a benign
        # /cdn-cgi/challenge-platform/ script tag on pages it lets through, so
        # matching raw HTML would false-positive on real content served via the
        # browser fetcher. A genuine challenge shows one of these phrases.
        visible = self._clean_text(html_text).lower()
        return any(
            marker in visible
            for marker in (
                "access denied",
                "just a moment",
                "checking your browser",
                "verify you are human",
                "sgcaptcha",
                "temporarily unavailable",
            )
        )

    def _looks_remote(self, vacancy: Vacancy, html_text: str) -> bool:
        haystack = self._clean_text(
            " ".join([vacancy.title, vacancy.location, vacancy.source_url, html_text])
        ).lower()
        return any(marker in haystack for marker in self.remote_markers)

    def _looks_excluded_location(self, location: str) -> bool:
        normalized_location = self._clean_text(location).lower().replace("ё", "е")
        return any(marker in normalized_location for marker in self.excluded_location_markers)

    def _clean_text(self, value: str) -> str:
        value = html.unescape(value)
        value = re.sub(r"<script.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<style.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()
