from __future__ import annotations

import html
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from ..models import Vacancy


class HhHtmlSource:
    name = "hh"
    listing_request_delay_seconds = 15
    vacancy_request_delay_seconds = 15
    DEFAULT_NON_RU_BY_AREA_IDS = (
        "5",  # Ukraine
        "9",  # Azerbaijan
        "28",  # Georgia
        "40",  # Kazakhstan
        "48",  # Kyrgyzstan
        "97",  # Uzbekistan
        "1001",  # Other regions
    )
    EXCLUDED_AREA_IDS = {"16", "113"}  # Belarus, Russia
    EXCLUDED_LOCATION_MARKERS = (
        "belarus",
        "russia",
        "беларус",
        "белорус",
        "минск",
        "москва",
        "санкт-петербург",
        "spb",
        "казань",
        "новосибирск",
        "екатеринбург",
        "нижний новгород",
        "самара",
        "ростов-на-дону",
        "краснодар",
        "воронеж",
        "пермь",
        "омск",
        "уфа",
        "челябинск",
        "красноярск",
        "томск",
        "сочи",
        "калининград",
    )
    REMOTE_MARKERS = (
        "можно удаленно",
        "можно удалённо",
        "удаленная работа",
        "удалённая работа",
        "удаленно",
        "удалённо",
        "remote",
        "work_format",
        "workformat",
    )

    _VACANCY_LINK_PATTERN = re.compile(
        r'<a[^>]+href="(?P<url>[^"]*/vacancy/\d+[^"]*)"',
        re.IGNORECASE,
    )
    _GENERIC_LINK_PATTERN = re.compile(r'href="(?P<url>[^"]+)"', re.IGNORECASE)
    _SEARCH_INPUT_SELECTORS = (
        'input[data-qa="search-input"]',
        'input[name="text"]',
        'input[type="search"]',
    )
    _DEFAULT_BROWSER_PROFILE_DIR = ".browser_profiles/hh"
    _BROWSER_EXECUTABLE_CANDIDATES = (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )

    def __init__(
        self,
        *,
        area_ids: tuple[str, ...] | None = None,
        remote_only: bool = True,
        exclude_russia_belarus: bool = True,
        fetch_mode: str | None = None,
        browser_headless: bool | str = True,
        browser_executable_path: str = "",
        browser_profile_dir: str | Path = _DEFAULT_BROWSER_PROFILE_DIR,
        browser_persistent_profile: bool | str = False,
        browser_wait_ms: int = 2500,
        browser_timeout_ms: int = 30000,
    ) -> None:
        configured_area_ids = self._parse_area_ids(os.getenv("HH_AREA_IDS", ""))
        self.area_ids = area_ids or configured_area_ids or self.DEFAULT_NON_RU_BY_AREA_IDS
        self.remote_only = remote_only
        self.exclude_russia_belarus = exclude_russia_belarus
        mode_value = fetch_mode or os.getenv("HH_FETCH_MODE") or os.getenv("HH_BROWSER_MODE")
        self.fetch_mode = self._normalize_fetch_mode(mode_value or "http")
        self.browser_headless = self._parse_bool(
            os.getenv("HH_BROWSER_HEADLESS", str(browser_headless)),
            default=True,
        )
        self.browser_executable_path = (
            os.getenv("HH_BROWSER_EXECUTABLE") or browser_executable_path
        )
        self.browser_profile_dir = Path(
            os.getenv("HH_BROWSER_PROFILE_DIR") or browser_profile_dir
        )
        self.browser_persistent_profile = self._parse_bool(
            os.getenv("HH_BROWSER_PERSISTENT_PROFILE", str(browser_persistent_profile)),
            default=False,
        )
        self.browser_wait_ms = max(0, int(browser_wait_ms))
        self.browser_timeout_ms = max(1000, int(browser_timeout_ms))
        self._playwright = None
        self._browser = None
        self._browser_context = None
        self._browser_page = None
        self._temporary_browser_profile_dir: Path | None = None

    @classmethod
    def from_options(cls, options: dict | None = None) -> HhHtmlSource:
        options = options or {}
        return cls(
            fetch_mode=str(options.get("fetch_mode", options.get("mode", "http"))),
            browser_headless=options.get("browser_headless", True),
            browser_executable_path=str(options.get("browser_executable_path", "")),
            browser_profile_dir=str(
                options.get("browser_profile_dir", cls._DEFAULT_BROWSER_PROFILE_DIR)
            ),
            browser_persistent_profile=options.get("browser_persistent_profile", False),
            browser_wait_ms=int(options.get("browser_wait_ms", 2500)),
            browser_timeout_ms=int(options.get("browser_timeout_ms", 30000)),
        )

    def build_listing_url(self, keyword: str | None = None, page: int = 0) -> str:
        params: list[tuple[str, str]] = [("page", str(max(0, page)))]
        if keyword:
            params.append(("text", keyword))
        params.extend(self._default_search_filter_params())
        return f"https://hh.ru/search/vacancy?{urlencode(params)}"

    def build_paged_url(self, listing_url: str, page: int) -> str:
        parsed = urlparse(listing_url)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        updated_pairs: list[tuple[str, str]] = []
        page_seen = False
        for key, value in query_pairs:
            if key == "page":
                if not page_seen:
                    updated_pairs.append(("page", str(max(0, page))))
                    page_seen = True
                continue
            updated_pairs.append((key, value))
        if not page_seen:
            updated_pairs.append(("page", str(max(0, page))))
        return urlunparse(parsed._replace(query=urlencode(updated_pairs)))

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        if max_pages < 1:
            max_pages = 1

        if self._uses_browser():
            return self._collect_vacancy_urls_with_browser(
                keyword=keyword,
                listing_url=listing_url,
                max_pages=max_pages,
                before_listing_fetch=before_listing_fetch,
                stop_at_vacancy=stop_at_vacancy,
            )

        seed_url = listing_url or self.build_listing_url(keyword=keyword)
        seed_url = self._ensure_search_filters(seed_url)
        page_urls: list[str] = []
        collected_urls: list[str] = []
        seen: set[str] = set()

        for page in range(max_pages):
            page_url = self.build_paged_url(seed_url, page)
            if before_listing_fetch is not None:
                before_listing_fetch(page_url)
            listing_html = self.fetch_vacancy_page(page_url)
            page_urls.append(page_url)
            page_vacancy_urls = self.collect_listing_urls(listing_html)
            if not page_vacancy_urls and self._looks_listing_blocked(listing_html):
                if collected_urls:
                    break
                raise ValueError("HH listing page is not publicly accessible.")
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

    def collect_listing_urls(self, listing_page_html: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        matches = list(self._VACANCY_LINK_PATTERN.finditer(listing_page_html))
        if not matches:
            matches = list(self._GENERIC_LINK_PATTERN.finditer(listing_page_html))

        for match in matches:
            candidate = urljoin("https://hh.ru/", html.unescape(match.group("url")).strip())
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
            return self._browser_fetch_html(url)

        request_url: str | Request = url
        if parsed.scheme in {"http", "https"}:
            request_url = Request(
                url,
                headers={
                    "User-Agent": os.getenv("HH_USER_AGENT")
                    or (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru,en;q=0.9",
                },
            )

        with urlopen(request_url, timeout=20) as response:
            content_bytes = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return content_bytes.decode(charset, errors="ignore")

    def close(self) -> None:
        if self._browser_context is not None:
            try:
                self._browser_context.close()
            except Exception:
                pass
            self._browser_context = None
            self._browser_page = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._temporary_browser_profile_dir is not None:
            shutil.rmtree(self._temporary_browser_profile_dir, ignore_errors=True)
            self._temporary_browser_profile_dir = None

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        if self._looks_blocked(html_text):
            raise ValueError("HH HTML page is not publicly accessible.")

        structured = self._extract_json_ld(html_text)
        description = self._extract_description(html_text) or self._clean_text(
            str(structured.get("description", ""))
        )
        title = self._extract_title(html_text) or self._clean_text(str(structured.get("title", "")))

        location = self._extract_location(html_text) or self._extract_json_ld_location(structured)
        if self.remote_only and not self._looks_remote(html_text, location):
            raise ValueError("HH vacancy is not marked as remote.")
        if self.exclude_russia_belarus and self._looks_excluded_location(location):
            raise ValueError("HH vacancy is located in Russia or Belarus.")

        return Vacancy(
            source_name=self.name,
            source_id=self._extract_source_id(source_url, html_text),
            source_url=self._canonical_vacancy_url(source_url),
            title=title,
            company=self._extract_company(html_text) or self._extract_json_ld_company(structured),
            location=location,
            salary_text=self._extract_salary(html_text),
            requirements=self._extract_skills(html_text),
            responsibilities=[description] if description else [],
            raw_text=html_text,
        )

    def can_handle_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        return (host == "hh.ru" or host.endswith(".hh.ru")) and bool(
            re.search(r"/vacancy/\d+", parsed.path)
        )

    def _canonical_vacancy_url(self, url: str) -> str:
        parsed = urlparse(url)
        vacancy_id = self._extract_source_id(url, "")
        if not vacancy_id:
            return urlunparse(parsed._replace(query="", fragment=""))
        host = parsed.netloc or "hh.ru"
        return urlunparse(
            parsed._replace(
                netloc=host,
                path=f"/vacancy/{vacancy_id}",
                query="",
                fragment="",
            )
        )

    def _extract_source_id(self, source_url: str, html_text: str) -> str:
        url_match = re.search(r"/vacancy/(\d+)", source_url)
        if url_match:
            return url_match.group(1)

        for pattern in (
            r'data-vacancy-id="(\d+)"',
            r'"vacancyId"\s*:\s*"?(\d+)"?',
        ):
            match = re.search(pattern, html_text)
            if match:
                return match.group(1)
        return source_url

    def _extract_title(self, html_text: str) -> str:
        for pattern in (
            r'<h1[^>]*data-qa="vacancy-title"[^>]*>(.*?)</h1>',
            r'<h1[^>]*>(.*?)</h1>',
            r'<meta\s+property="og:title"\s+content="([^"]+)"',
            r"<title[^>]*>(.*?)</title>",
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_company(self, html_text: str) -> str:
        for pattern in (
            r'<[^>]+data-qa="vacancy-company-name"[^>]*>(.*?)</[^>]+>',
            r'<[^>]+data-qa="vacancy-company-name"[^>]*>.*?<span[^>]*>(.*?)</span>',
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_location(self, html_text: str) -> str:
        for pattern in (
            r'<[^>]+data-qa="vacancy-view-location"[^>]*>(.*?)</[^>]+>',
            r'<[^>]+data-qa="vacancy-view-raw-address"[^>]*>(.*?)</[^>]+>',
            r'<[^>]+data-qa="vacancy-view-location-location"[^>]*>(.*?)</[^>]+>',
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_salary(self, html_text: str) -> str:
        for pattern in (
            r'<[^>]+data-qa="vacancy-salary"[^>]*>(.*?)</[^>]+>',
            r'<span[^>]+class="[^"]*vacancy-salary[^"]*"[^>]*>(.*?)</span>',
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_description(self, html_text: str) -> str:
        for pattern in (
            r'<div[^>]+data-qa="vacancy-description"[^>]*>(.*?)</div>\s*(?:<div|<span|</main|</article)',
            r'<div[^>]+class="[^"]*\bg-user-content\b[^"]*"[^>]*>(.*?)</div>',
        ):
            value = self._extract_first(pattern, html_text)
            if value:
                return value
        return ""

    def _extract_skills(self, html_text: str) -> list[str]:
        patterns = (
            r'<[^>]+data-qa="skills-element"[^>]*>(.*?)</[^>]+>',
            r'<[^>]+class="[^"]*\bbloko-tag__section_text\b[^"]*"[^>]*>(.*?)</[^>]+>',
        )
        skills: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for match in re.finditer(pattern, html_text, re.IGNORECASE | re.DOTALL):
                value = self._clean_text(match.group(1))
                if not value or value in seen:
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
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "JobPosting":
                        return item
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data
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

    def _looks_blocked(self, html_text: str) -> bool:
        if re.search(
            r'data-qa="vacancy-title"|@type"\s*:\s*"JobPosting"',
            html_text,
            re.IGNORECASE,
        ):
            return False
        visible_text = self._clean_text(html_text).lower()
        return any(
            marker in visible_text
            for marker in (
                "account-login",
                "captcha",
                "доступ ограничен",
                "подтвердите, что вы не робот",
                "checking your browser",
                "access denied",
            )
        )

    def _looks_listing_blocked(self, html_text: str) -> bool:
        visible_text = self._clean_text(html_text).lower()
        if any(
            marker in visible_text
            for marker in (
                "account-login",
                "captcha",
                "доступ ограничен",
                "подтвердите, что вы не робот",
                "checking your browser",
                "access denied",
            )
        ):
            return True
        lowered = html_text.lower()
        return any(marker in lowered for marker in ("sgcaptcha", "challenge-platform"))

    def _default_search_filter_params(self) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = []
        for area_id in self.area_ids:
            if self.exclude_russia_belarus and area_id in self.EXCLUDED_AREA_IDS:
                continue
            params.append(("area", area_id))
        if self.remote_only:
            params.extend(
                [
                    ("schedule", "remote"),
                    ("work_format", "REMOTE"),
                ]
            )
        return params

    def _ensure_search_filters(self, listing_url: str) -> str:
        parsed = urlparse(listing_url)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        cleaned_pairs: list[tuple[str, str]] = []
        has_area = False

        for key, value in query_pairs:
            if key == "area":
                if self.exclude_russia_belarus and value in self.EXCLUDED_AREA_IDS:
                    continue
                has_area = True
                cleaned_pairs.append((key, value))
                continue
            if self.remote_only and key in {"schedule", "work_format"}:
                continue
            cleaned_pairs.append((key, value))

        if not has_area:
            for area_id in self.area_ids:
                if self.exclude_russia_belarus and area_id in self.EXCLUDED_AREA_IDS:
                    continue
                cleaned_pairs.append(("area", area_id))
        if self.remote_only:
            cleaned_pairs.extend(
                [
                    ("schedule", "remote"),
                    ("work_format", "REMOTE"),
                ]
            )

        return urlunparse(parsed._replace(query=urlencode(cleaned_pairs)))

    def _looks_remote(self, html_text: str, location: str = "") -> bool:
        haystack = self._clean_text(f"{location} {html_text}").lower().replace("ё", "е")
        return any(marker.replace("ё", "е") in haystack for marker in self.REMOTE_MARKERS)

    def _looks_excluded_location(self, location: str) -> bool:
        normalized_location = self._clean_text(location).lower().replace("ё", "е")
        return any(marker in normalized_location for marker in self.EXCLUDED_LOCATION_MARKERS)

    def _parse_area_ids(self, value: str) -> tuple[str, ...]:
        area_ids = []
        seen: set[str] = set()
        for part in re.split(r"[\s,;]+", value):
            normalized = part.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            area_ids.append(normalized)
        return tuple(area_ids)

    def _collect_vacancy_urls_with_browser(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        seed_url = self._ensure_search_filters(listing_url or self.build_listing_url(keyword=keyword))
        collected_urls: list[str] = []
        page_urls: list[str] = []
        seen: set[str] = set()

        for page_number in range(max_pages):
            if not listing_url and page_number == 0:
                page_url = self.build_paged_url(seed_url, 0)
                if before_listing_fetch is not None:
                    before_listing_fetch(page_url)
                listing_html, actual_url = self._browser_search_first_page(keyword or "", page_url)
                seed_url = self._ensure_search_filters(actual_url)
            else:
                page_url = self.build_paged_url(seed_url, page_number)
                if before_listing_fetch is not None:
                    before_listing_fetch(page_url)
                listing_html, actual_url = self._browser_fetch_html_and_url(page_url)

            page_vacancy_urls = self.collect_listing_urls(listing_html)
            if not page_vacancy_urls and self._looks_listing_blocked(listing_html):
                if collected_urls:
                    break
                raise ValueError("HH listing page is not publicly accessible.")
            page_urls.append(actual_url)
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

    def _browser_search_first_page(self, keyword: str, page_url: str) -> tuple[str, str]:
        page = self._browser_page_or_new()
        seed_url = self._ensure_search_filters(self.build_listing_url(keyword=None, page=0))
        self._goto_browser_page(page, seed_url)
        self._dismiss_browser_popups(page)

        if keyword:
            search_input = self._find_search_input(page)
            search_input.fill(keyword)
            search_input.press("Enter")
            self._wait_after_browser_action(page)
        else:
            self._goto_browser_page(page, page_url)

        filtered_url = self._ensure_search_filters(page.url)
        if filtered_url != page.url:
            self._goto_browser_page(page, filtered_url)

        return page.content(), page.url

    def _browser_fetch_html(self, url: str) -> str:
        html_text, _ = self._browser_fetch_html_and_url(url)
        return html_text

    def _browser_fetch_html_and_url(self, url: str) -> tuple[str, str]:
        page = self._browser_page_or_new()
        self._goto_browser_page(page, url)
        return page.content(), page.url

    def _browser_page_or_new(self):
        if self._browser_page is not None:
            return self._browser_page
        context = self._browser_context_or_start()
        if context.pages:
            self._browser_page = context.pages[0]
        else:
            self._browser_page = context.new_page()
        return self._browser_page

    def _browser_context_or_start(self):
        if self._browser_context is not None:
            return self._browser_context

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "HH browser mode requires Playwright. Install it with: pip install playwright"
            ) from error

        self._playwright = sync_playwright().start()
        executable_path = self._resolve_browser_executable()
        browser_launch_kwargs = {
            "headless": self.browser_headless,
            "timeout": self.browser_timeout_ms,
        }
        browser_context_kwargs = {
            "locale": "ru-RU",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "viewport": {"width": 1366, "height": 900},
        }
        if executable_path:
            browser_launch_kwargs["executable_path"] = executable_path

        try:
            if self.browser_persistent_profile:
                self._browser_context = self._launch_persistent_browser_context(
                    browser_launch_kwargs,
                    browser_context_kwargs,
                )
            else:
                self._browser = self._playwright.chromium.launch(**browser_launch_kwargs)
                self._browser_context = self._browser.new_context(**browser_context_kwargs)
        except Exception as error:
            self.close()
            raise RuntimeError(
                "HH browser mode could not start Chrome/Edge. "
                "Set HH_BROWSER_EXECUTABLE or install Playwright browsers."
            ) from error
        return self._browser_context

    def _launch_persistent_browser_context(
        self,
        browser_launch_kwargs: dict,
        browser_context_kwargs: dict,
    ):
        profile_dir = self.browser_profile_dir
        if not profile_dir.is_absolute():
            profile_dir = Path.cwd() / profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)

        launch_kwargs = {**browser_launch_kwargs, **browser_context_kwargs}
        try:
            return self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **launch_kwargs,
            )
        except Exception as error:
            if "ProcessSingleton" not in str(error) and "profile" not in str(error).lower():
                raise
            fallback_profile_dir = Path(
                tempfile.mkdtemp(prefix="hh-session-", dir=str(profile_dir.parent))
            )
            self._temporary_browser_profile_dir = fallback_profile_dir
            return self._playwright.chromium.launch_persistent_context(
                str(fallback_profile_dir),
                **launch_kwargs,
            )

    def _goto_browser_page(self, page, url: str) -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=self.browser_timeout_ms)
        self._wait_after_browser_action(page)
        self._dismiss_browser_popups(page)

    def _wait_after_browser_action(self, page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=self.browser_timeout_ms)
        except Exception:
            pass
        if self.browser_wait_ms:
            page.wait_for_timeout(self.browser_wait_ms)

    def _dismiss_browser_popups(self, page) -> None:
        for text in ("Понятно", "Accept", "I agree"):
            try:
                page.get_by_text(text, exact=True).click(timeout=1000)
            except Exception:
                continue

    def _find_search_input(self, page):
        for selector in self._SEARCH_INPUT_SELECTORS:
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=5000)
                return locator
            except Exception:
                continue
        raise ValueError("HH search input was not found on the page.")

    def _resolve_browser_executable(self) -> str:
        configured_path = self.browser_executable_path.strip()
        if configured_path:
            if not Path(configured_path).exists():
                raise RuntimeError(f"HH browser executable not found: {configured_path}")
            return configured_path

        for candidate in self._BROWSER_EXECUTABLE_CANDIDATES:
            if Path(candidate).exists():
                return candidate

        playwright_path = ""
        if self._playwright is not None:
            try:
                playwright_path = self._playwright.chromium.executable_path
            except Exception:
                playwright_path = ""
        if playwright_path and Path(playwright_path).exists():
            return playwright_path

        return ""

    def _uses_browser(self) -> bool:
        return self.fetch_mode == "browser"

    def _normalize_fetch_mode(self, value: str) -> str:
        normalized = str(value or "http").strip().lower()
        if normalized in {"1", "true", "yes", "on", "browser", "playwright"}:
            return "browser"
        if normalized in {"0", "false", "no", "off", "http", "html", "urllib"}:
            return "http"
        raise ValueError(f"Unsupported HH fetch mode: {value}")

    def _parse_bool(self, value: object, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if not normalized:
            return default
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    def _clean_text(self, value: str) -> str:
        value = html.unescape(value)
        value = re.sub(r"<script.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<style.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()
