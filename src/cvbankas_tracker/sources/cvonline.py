"""CV-Online Lithuania source.

CV-Online renders the public result set into ``__NEXT_DATA__``.  Reading that
payload lets us crawl the complete newest-first listing without relying on its
keyword search and, importantly, avoids one HTTP request per vacancy.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from ..models import Vacancy


class CvOnlineSource:
    """Collect the public full feed, then rely on SQLite for incrementality.

    This is deliberately not a search adapter: CV-Online's public live-search
    filters have proven unreliable.  The only supported collection rule is
    unfiltered newest-first feed -> local database URL de-duplication.
    """

    name = "cvonline"
    base_url = "https://www.cvonline.lt"
    page_size = 100
    uses_search_keywords = False
    requires_database = True
    collection_rule = "unfiltered_full_feed_then_database_incremental"
    _NEXT_DATA_RE = re.compile(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(?P<data>.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    _VACANCY_HREF_RE = re.compile(r'href=["\'](?P<href>/vacancy/(?P<id>\d+)[^"\']*)', re.IGNORECASE)
    _TAG_RE = re.compile(r"<[^>]+>")
    _SPACE_RE = re.compile(r"\s+")
    _CACHE_KEY = "cvonline-listing-vacancy"

    def __init__(self) -> None:
        self._listing_payloads: dict[str, dict[str, object]] = {}

    def build_listing_url(self, keyword: str | None = None, page: int = 0) -> str:
        """Return the unfiltered public listing; CV-Online keywords are ignored."""
        del keyword
        offset = max(0, page) * self.page_size
        return f"{self.base_url}/lt/search?{urlencode({'limit': self.page_size, 'offset': offset, 'sort': 'created'})}"

    def build_paged_url(self, listing_url: str, page: int) -> str:
        # Do not preserve arbitrary query parameters from a pasted CV-Online
        # URL.  In particular, filtered live-search URLs are not a supported
        # source of truth for this adapter.
        del listing_url
        return self.build_listing_url(page=page)

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        del keyword
        if listing_url:
            raise ValueError(
                "CV-Online does not support filtered --listing-url collection; "
                "use the unfiltered full feed and the local database for incremental runs."
            )
        seed_url = self.build_listing_url()
        urls: list[str] = []
        page_urls: list[str] = []
        seen: set[str] = set()

        for page in range(max(1, max_pages)):
            page_url = self.build_paged_url(seed_url, page)
            if before_listing_fetch is not None:
                before_listing_fetch(page_url)
            page_urls.append(page_url)
            payload = self._search_results(self.fetch_vacancy_page(page_url))
            vacancies = payload.get("vacancies", [])
            if not isinstance(vacancies, list) or not vacancies:
                break

            hrefs = self._vacancy_hrefs(payload, page_url)
            stop = False
            for item in vacancies:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                source_id = str(item["id"])
                source_url = hrefs.get(source_id, f"{self.base_url}/lt/vacancy/{source_id}")
                source_url = self._canonical_url(source_url)
                if stop_at_vacancy is not None and stop_at_vacancy(source_url):
                    stop = True
                    break
                self._listing_payloads[source_url] = item
                if source_url not in seen:
                    seen.add(source_url)
                    urls.append(source_url)

            if stop:
                break
            # The last page can be shorter than the requested page size.
            if len(vacancies) < self.page_size:
                break

        return urls, page_urls

    def fetch_vacancy_page(self, url: str) -> str:
        canonical_url = self._canonical_url(url)
        cached = self._listing_payloads.get(canonical_url)
        if cached is not None:
            return json.dumps({"kind": self._CACHE_KEY, "vacancy": cached}, ensure_ascii=False)
        return self._fetch_page(url)

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        cached = self._cached_vacancy(html_text)
        if cached is not None:
            return self._vacancy_from_listing(cached, source_url)

        page_props = self._page_props(html_text)
        detail = self._detail_vacancy(page_props)
        if detail is None:
            raise ValueError("CV-Online page does not contain a public vacancy payload.")
        return self._vacancy_from_detail(detail, page_props, source_url)

    def can_handle_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        return (host == "cvonline.lt" or host.endswith(".cvonline.lt")) and bool(
            re.search(r"/vacancy/\d+", parsed.path)
        )

    def _fetch_page(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                "Accept-Language": "lt,en;q=0.8",
            },
        )
        with urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="ignore")

    def _search_results(self, page_html: str) -> dict[str, object]:
        props = self._page_props(page_html)
        value = props.get("searchResults")
        if not isinstance(value, dict):
            raise ValueError("CV-Online listing page does not contain search results.")
        # Keep the original HTML long enough to recover canonical vacancy links.
        value = dict(value)
        value["_listing_html"] = page_html
        return value

    def _vacancy_hrefs(self, payload: dict[str, object], page_url: str) -> dict[str, str]:
        listing_html = str(payload.get("_listing_html", ""))
        return {
            match.group("id"): urljoin(page_url, match.group("href"))
            for match in self._VACANCY_HREF_RE.finditer(listing_html)
        }

    def _page_props(self, page_html: str) -> dict[str, object]:
        match = self._NEXT_DATA_RE.search(page_html)
        if match is None:
            raise ValueError("CV-Online page is missing __NEXT_DATA__; it may no longer be public.")
        try:
            data = json.loads(html.unescape(match.group("data")))
            props = data["props"]["pageProps"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("CV-Online __NEXT_DATA__ payload is invalid.") from error
        if not isinstance(props, dict):
            raise ValueError("CV-Online page properties are invalid.")
        return props

    def _cached_vacancy(self, value: str) -> dict[str, object] | None:
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return None
        vacancy = data.get("vacancy") if data.get("kind") == self._CACHE_KEY else None
        return vacancy if isinstance(vacancy, dict) else None

    def _detail_vacancy(self, page_props: dict[str, object]) -> dict[str, object] | None:
        candidates = page_props.get("vacancy")
        if not isinstance(candidates, dict):
            return None
        for key, value in candidates.items():
            if key.isdigit() and isinstance(value, dict):
                return value
        return None

    def _vacancy_from_listing(self, item: dict[str, object], source_url: str) -> Vacancy:
        description = self._clean(item.get("positionContent"))
        skills = self._string_list(item.get("skills")) + self._string_list(item.get("keywords"))
        return Vacancy(
            source_name=self.name,
            source_id=str(item["id"]),
            source_url=self._canonical_url(source_url),
            title=self._clean(item.get("positionTitle")),
            company=self._clean(item.get("employerName")),
            location=self._listing_location(item),
            salary_text=self._salary(item.get("salaryFrom"), item.get("salaryTo"), item.get("hourlySalary")),
            requirements=skills,
            responsibilities=[description] if description else [],
            raw_text=description,
        )

    def _vacancy_from_detail(
        self, item: dict[str, object], page_props: dict[str, object], source_url: str
    ) -> Vacancy:
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        highlights = item.get("highlights") if isinstance(item.get("highlights"), dict) else {}
        description = self._clean(details.get("standardDetails")) or self._clean(page_props.get("alma"))
        return Vacancy(
            source_name=self.name,
            source_id=str(item["id"]),
            source_url=self._canonical_url(source_url),
            title=self._clean(item.get("position")),
            company=self._clean(item.get("employerName")),
            location=self._detail_location(highlights, page_props.get("locations")),
            salary_text=self._salary(highlights.get("salaryFrom"), highlights.get("salaryTo"), False),
            requirements=self._string_list(item.get("skills")) + self._string_list(item.get("languages")),
            responsibilities=[description] if description else [],
            raw_text=description,
        )

    def _listing_location(self, item: dict[str, object]) -> str:
        if item.get("remoteWork"):
            return "Remote"
        return ""

    def _detail_location(self, highlights: dict[str, object], locations: object) -> str:
        if highlights.get("remoteWork"):
            return "Remote"
        location = highlights.get("location")
        if not isinstance(location, dict) or not isinstance(locations, dict):
            return ""
        names: list[str] = []
        for key, group in (("townId", "towns"), ("countyId", "counties"), ("countryId", "countries")):
            entries = locations.get(group)
            entry = entries.get(str(location.get(key))) if isinstance(entries, dict) else None
            if isinstance(entry, dict) and self._clean(entry.get("name")):
                names.append(self._clean(entry.get("name")))
        return ", ".join(dict.fromkeys(names))

    def _salary(self, minimum: object, maximum: object, hourly: object) -> str:
        values = [self._clean(value) for value in (minimum, maximum) if value not in (None, "")]
        if not values:
            return ""
        suffix = " EUR/hour" if hourly else " EUR"
        return f"{'–'.join(values)}{suffix}"

    def _string_list(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [text for item in value if (text := self._clean(item))]

    def _clean(self, value: object) -> str:
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        return self._SPACE_RE.sub(" ", html.unescape(self._TAG_RE.sub(" ", str(value or "")))).strip()

    def _canonical_url(self, url: str) -> str:
        parsed = urlparse(urljoin(self.base_url, url))
        path = parsed.path
        if path.startswith("/vacancy/"):
            path = f"/lt{path}"
        return urlunparse(parsed._replace(path=path, query="", fragment=""))
