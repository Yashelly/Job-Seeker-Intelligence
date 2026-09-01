from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlparse

from ..collector import CvbankasCollector
from ..models import Vacancy
from ..parser import VacancyParser


class CvbankasSource:
    name = "cvbankas"
    exclude_russia_belarus = True
    excluded_location_markers = (
        "belarus",
        "russia",
        "беларус",
        "белорус",
        "минск",
        "moscow",
        "москва",
        "saint petersburg",
        "санкт-петербург",
    )

    def __init__(
        self,
        collector: CvbankasCollector | None = None,
        parser: VacancyParser | None = None,
    ) -> None:
        self._collector = collector or CvbankasCollector()
        self._parser = parser or VacancyParser()

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        return self._collector.collect_listing_urls_from_pages(
            keyword=keyword,
            listing_url=listing_url or None,
            max_pages=max_pages,
            before_listing_fetch=before_listing_fetch,
            stop_at_vacancy=stop_at_vacancy,
        )

    def fetch_vacancy_page(self, url: str) -> str:
        return self._collector.fetch_page(url)

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        vacancy = self._parser.parse(html_text, source_url, source_name=self.name)
        if self.exclude_russia_belarus and self._looks_excluded_location(vacancy.location):
            raise ValueError("CVbankas vacancy is located in Russia or Belarus.")
        return vacancy

    def can_handle_url(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host == "cvbankas.lt" or host.endswith(".cvbankas.lt")

    def _looks_excluded_location(self, location: str) -> bool:
        normalized_location = self._clean_text(location).lower().replace("ё", "е")
        return any(marker in normalized_location for marker in self.excluded_location_markers)

    def _clean_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()
