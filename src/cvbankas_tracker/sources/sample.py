from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..collector import CvbankasCollector
from ..models import Vacancy
from ..parser import VacancyParser


class SampleVacancySource:
    name = "sample"

    def __init__(
        self,
        data_dir: str | Path,
        collector: CvbankasCollector | None = None,
        parser: VacancyParser | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._collector = collector or CvbankasCollector()
        self._parser = parser or VacancyParser()
        self._file_map = {
            "1-1234567": self._data_dir / "vacancy_python_backend.html",
            "1-7654321": self._data_dir / "vacancy_data_analyst.html",
        }

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        listing_path = str(self._data_dir / "listings.html")
        if before_listing_fetch is not None:
            before_listing_fetch(listing_path)
        listing_html = Path(listing_path).read_text(encoding="utf-8")
        urls = self._collector.collect_listing_urls(listing_html)
        if stop_at_vacancy is not None:
            new_urls: list[str] = []
            for url in urls:
                if stop_at_vacancy(url):
                    break
                new_urls.append(url)
            urls = new_urls
        return urls, [listing_path]

    def fetch_vacancy_page(self, url: str) -> str:
        key = url.rsplit("/", maxsplit=1)[-1]
        return self._file_map[key].read_text(encoding="utf-8")

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        return self._parser.parse(html_text, source_url, source_name=self.name)

    def can_handle_url(self, url: str) -> bool:
        key = url.rsplit("/", maxsplit=1)[-1]
        return key in self._file_map
