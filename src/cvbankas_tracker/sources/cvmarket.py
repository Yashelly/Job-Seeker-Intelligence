"""CVMarket Lithuania vacancy source."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from ..models import Vacancy
from .generic_html import GenericHtmlJobSource


class CvMarketSource(GenericHtmlJobSource):
    """Collect CVMarket keyword results in newest-first order."""

    name = "cvmarket"
    base_url = "https://www.cvmarket.lt/"
    allowed_hosts = ("cvmarket.lt",)
    listing_path = "/darbo-skelbimai"
    keyword_param = "search[keyword]"
    page_param = "start"
    first_page = 0
    page_size = 30
    vacancy_path_patterns = (re.compile(r"-\d{7,}/?$"),)

    _LISTING_LINK_RE = re.compile(
        r'''<a\b(?=[^>]*\bclass=["'][^"']*\bjobad-url\b[^"']*["'])'''
        r'''[^>]*\bhref=["'](?P<url>[^"']+)["']''',
        re.IGNORECASE,
    )
    _NEXT_LINK_RE = re.compile(
        r'''<link\b(?=[^>]*\brel=["']next["'])[^>]*\bhref=["'](?P<url>[^"']+)["']''',
        re.IGNORECASE,
    )
    _COMPANY_RE = re.compile(
        r'''<a\b[^>]*href=["'][^"']*-imones-darbo-skelbimai-\d+["'][^>]*>(?P<name>.*?)</a>''',
        re.IGNORECASE | re.DOTALL,
    )
    _JSON_LD_RE = re.compile(
        r'''<script[^>]+type=["']application/ld\+json["'][^>]*>(?P<data>.*?)</script>''',
        re.IGNORECASE | re.DOTALL,
    )

    def build_listing_url(self, keyword: str | None = None, page: int | None = None) -> str:
        params: list[tuple[str, str]] = [("op", "search")]
        if keyword:
            params.append((self.keyword_param, keyword))
        params.append(("sort", "activation_date"))
        if page is not None and page > 0:
            params.append((self.page_param, str(page)))
        return f"{self.base_url.rstrip('/')}{self.listing_path}?{urlencode(params)}"

    def build_paged_url(self, listing_url: str, page: int) -> str:
        normalized = self._normalize_listing_url(listing_url)
        parsed = urlparse(normalized)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if page > 0:
            query[self.page_param] = str(page * self.page_size)
        else:
            query.pop(self.page_param, None)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        page_url = self._normalize_listing_url(listing_url or self.build_listing_url(keyword))
        page_urls: list[str] = []
        collected_urls: list[str] = []
        seen_pages: set[str] = set()
        seen_vacancies: set[str] = set()

        for _ in range(max(1, max_pages)):
            if page_url in seen_pages:
                break
            seen_pages.add(page_url)
            if before_listing_fetch is not None:
                before_listing_fetch(page_url)
            listing_html = self.fetch_vacancy_page(page_url)
            page_urls.append(page_url)
            vacancy_urls = self.collect_listing_urls(listing_html)
            if not vacancy_urls:
                if len(page_urls) == 1 and self._looks_blocked(listing_html):
                    raise ValueError("cvmarket listing page is not publicly accessible.")
                break

            stop = False
            added_on_page = False
            for vacancy_url in vacancy_urls:
                if stop_at_vacancy is not None and stop_at_vacancy(vacancy_url):
                    stop = True
                    break
                if vacancy_url in seen_vacancies:
                    continue
                seen_vacancies.add(vacancy_url)
                collected_urls.append(vacancy_url)
                added_on_page = True
            if stop or not added_on_page:
                break

            next_match = self._NEXT_LINK_RE.search(listing_html)
            if next_match is None:
                break
            page_url = self._normalize_listing_url(urljoin(page_url, html.unescape(next_match.group("url"))))

        return collected_urls, page_urls

    def collect_listing_urls(self, listing_page_html: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for match in self._LISTING_LINK_RE.finditer(listing_page_html):
            candidate = self._canonical_vacancy_url(urljoin(self.base_url, html.unescape(match.group("url"))))
            if candidate in seen or not self.can_handle_url(candidate):
                continue
            seen.add(candidate)
            urls.append(candidate)
        return urls

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        if self._looks_blocked(html_text):
            raise ValueError("cvmarket HTML page is not publicly accessible.")
        job, nodes_by_id = self._job_posting_and_nodes(html_text)
        if not job:
            raise ValueError("CVMarket page does not contain a public JobPosting payload.")

        description_html = str(job.get("description", ""))
        description = self._clean_text(description_html)
        return Vacancy(
            source_name=self.name,
            source_id=self._extract_source_id(source_url, html_text),
            source_url=self._canonical_vacancy_url(source_url),
            title=self._clean_text(str(job.get("title", ""))) or self._extract_title(html_text),
            company=self._company(job, nodes_by_id, html_text),
            location=self._location(job, nodes_by_id),
            salary_text=self._extract_json_ld_salary(job),
            requirements=self._requirements(description_html),
            responsibilities=[description] if description else [],
            raw_text=description,
        )

    def can_handle_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return self._host_allowed(parsed.netloc.lower()) and any(
            pattern.search(parsed.path) for pattern in self.vacancy_path_patterns
        )

    def _normalize_listing_url(self, url: str) -> str:
        parsed = urlparse(urljoin(self.base_url, url))
        if parsed.scheme not in {"http", "https"} or not self._host_allowed(parsed.netloc.lower()):
            raise ValueError("CVMarket listing URL must use cvmarket.lt.")
        if not parsed.path.rstrip("/").endswith(self.listing_path):
            raise ValueError("CVMarket listing URL must point to /darbo-skelbimai.")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["sort"] = "activation_date"
        return urlunparse(("https", "www.cvmarket.lt", self.listing_path, "", urlencode(query), ""))

    def _canonical_vacancy_url(self, url: str) -> str:
        parsed = urlparse(urljoin(self.base_url, url))
        return urlunparse(("https", "www.cvmarket.lt", parsed.path.rstrip("/"), "", "", ""))

    def _extract_source_id(self, source_url: str, html_text: str) -> str:
        del html_text
        match = re.search(r"-(\d{7,})/?$", urlparse(source_url).path)
        return match.group(1) if match else urlparse(source_url).path.strip("/")

    def _job_posting_and_nodes(self, html_text: str) -> tuple[dict, dict[str, dict]]:
        for match in self._JSON_LD_RE.finditer(html_text):
            raw_data = match.group("data").strip()
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                try:
                    data = json.loads(html.unescape(raw_data))
                except json.JSONDecodeError:
                    continue
            nodes: list[dict] = []
            self._collect_json_nodes(data, nodes)
            job = next((node for node in nodes if self._has_type(node, "JobPosting")), None)
            if job is not None:
                nodes_by_id: dict[str, dict] = {}
                for node in nodes:
                    node_id = node.get("@id")
                    if not isinstance(node_id, str):
                        continue
                    if node_id not in nodes_by_id or len(node) > len(nodes_by_id[node_id]):
                        nodes_by_id[node_id] = node
                return job, nodes_by_id
        return {}, {}

    def _collect_json_nodes(self, value: object, result: list[dict]) -> None:
        if isinstance(value, dict):
            result.append(value)
            for child in value.values():
                self._collect_json_nodes(child, result)
        elif isinstance(value, list):
            for child in value:
                self._collect_json_nodes(child, result)

    def _has_type(self, node: dict, expected: str) -> bool:
        node_type = node.get("@type")
        return node_type == expected or (isinstance(node_type, list) and expected in node_type)

    def _resolve_reference(self, value: object, nodes_by_id: dict[str, dict]) -> dict:
        if not isinstance(value, dict):
            return {}
        reference = value.get("@id")
        if isinstance(reference, str) and reference in nodes_by_id:
            return nodes_by_id[reference]
        return value

    def _company(self, job: dict, nodes_by_id: dict[str, dict], html_text: str) -> str:
        organization = self._resolve_reference(job.get("hiringOrganization"), nodes_by_id)
        name = self._clean_text(str(organization.get("name", "")))
        if name:
            return name
        match = self._COMPANY_RE.search(html_text)
        return self._clean_text(match.group("name")) if match else ""

    def _location(self, job: dict, nodes_by_id: dict[str, dict]) -> str:
        if str(job.get("jobLocationType", "")).upper() == "TELECOMMUTE":
            return "Remote"
        locations = job.get("jobLocation", [])
        if isinstance(locations, dict):
            locations = [locations]
        if not isinstance(locations, list):
            return ""
        parts: list[str] = []
        for item in locations:
            location = self._resolve_reference(item, nodes_by_id)
            address = self._resolve_reference(location.get("address"), nodes_by_id)
            for key in ("addressLocality", "addressRegion", "addressCountry"):
                value = self._clean_text(str(address.get(key, "")))
                if value and value not in parts:
                    parts.append(value)
        return ", ".join(parts)

    def _requirements(self, description_html: str) -> list[str]:
        start = re.search(
            r"<p[^>]*>.*?(?:requirements|reikalavimai(?:\s+kandidatui)?).*?</p>",
            description_html,
            re.IGNORECASE | re.DOTALL,
        )
        if start is None:
            return []
        section = description_html[start.end() :]
        end = re.search(
            r"<p[^>]*>.*?(?:company offers|we offer|įmonė siūlo|mes siūlome).*?</p>",
            section,
            re.IGNORECASE | re.DOTALL,
        )
        if end is not None:
            section = section[: end.start()]
        return [
            value
            for item in re.findall(r"<li[^>]*>(.*?)</li>", section, re.IGNORECASE | re.DOTALL)
            if (value := self._clean_text(item))
        ]
