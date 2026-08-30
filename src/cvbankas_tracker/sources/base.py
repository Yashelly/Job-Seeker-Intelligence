from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..models import Vacancy


class VacancySource(Protocol):
    """Common interface for vacancy providers."""

    name: str

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Return vacancy URLs and listing/search pages visited for this source.

        When ``stop_at_vacancy`` returns true, the matching URL is not included
        and no later result pages are fetched. Daily newest-first collection uses
        this hook to stop at the first vacancy already present in SQLite.
        """

    def fetch_vacancy_page(self, url: str) -> str:
        """Fetch the raw vacancy page content."""

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        """Parse one vacancy page into the shared vacancy model."""

    def can_handle_url(self, url: str) -> bool:
        """Return whether this source can fetch and parse a direct vacancy URL."""
