from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.sources import resolve_source_for_url
from cvbankas_tracker.sources.hh import HhHtmlSource


HH_LISTING_HTML = """
<html>
  <body>
    <a data-qa="serp-item__title" href="/vacancy/111111?query=python">Python Automation Engineer</a>
    <a data-qa="serp-item__title" href="https://spb.hh.ru/vacancy/222222?from=search">RPA Developer</a>
    <a data-qa="serp-item__title" href="/vacancy/111111?duplicate=true">Duplicate</a>
  </body>
</html>
"""


HH_VACANCY_HTML = """
<html>
  <head>
    <script type="application/ld+json">
    {
      "@type": "JobPosting",
      "title": "Python Automation Engineer",
      "hiringOrganization": {"name": "Fallback Company"},
      "jobLocation": {
        "address": {
          "addressLocality": "Belgrade",
          "addressCountry": "Serbia"
        }
      },
      "description": "Fallback description"
    }
    </script>
  </head>
  <body>
    <main>
      <h1 data-qa="vacancy-title">Python Automation Engineer</h1>
      <div data-qa="vacancy-company-name"><span>Automation Lab</span></div>
      <p data-qa="vacancy-view-location">Remote Europe</p>
      <span data-qa="vacancy-salary">3 000-4 500 EUR</span>
      <div data-qa="vacancy-description">
        <p>Build internal automation services.</p>
        <ul><li>Integrate APIs</li><li>Maintain Python jobs</li></ul>
      </div>
      <span data-qa="skills-element">Python</span>
      <span data-qa="skills-element">SQL</span>
    </main>
  </body>
</html>
"""


class HhHtmlSourceTests(unittest.TestCase):
    def test_collect_listing_urls_deduplicates_hh_vacancies(self) -> None:
        source = HhHtmlSource()

        urls = source.collect_listing_urls(HH_LISTING_HTML)

        self.assertEqual(
            urls,
            [
                "https://hh.ru/vacancy/111111",
                "https://spb.hh.ru/vacancy/222222",
            ],
        )

    def test_collect_vacancy_urls_from_pages_uses_zero_based_hh_paging(self) -> None:
        source = HhHtmlSource()
        delayed_urls = []

        with patch.object(source, "fetch_vacancy_page", return_value=HH_LISTING_HTML) as fetch:
            urls, page_urls = source.collect_vacancy_urls(
                keyword="python automation",
                max_pages=2,
                before_listing_fetch=delayed_urls.append,
            )

        first_query = parse_qs(urlparse(page_urls[0]).query)
        self.assertEqual(len(urls), 2)
        self.assertEqual(delayed_urls, page_urls)
        self.assertIn("text=python+automation", page_urls[0])
        self.assertIn("page=0", page_urls[0])
        self.assertIn("page=1", page_urls[1])
        self.assertEqual(first_query["schedule"], ["remote"])
        self.assertEqual(first_query["work_format"], ["REMOTE"])
        self.assertNotIn("113", first_query["area"])
        self.assertNotIn("16", first_query["area"])
        self.assertEqual(fetch.call_count, 2)

    def test_collect_vacancy_urls_reports_blocked_hh_listing(self) -> None:
        source = HhHtmlSource()

        with patch.object(source, "fetch_vacancy_page", return_value="captcha робот"):
            with self.assertRaisesRegex(ValueError, "not publicly accessible"):
                source.collect_vacancy_urls(keyword="python", max_pages=1)

    def test_collect_vacancy_urls_keeps_earlier_pages_if_later_hh_page_blocks(self) -> None:
        source = HhHtmlSource()

        with patch.object(
            source,
            "fetch_vacancy_page",
            side_effect=[HH_LISTING_HTML, "captcha подтвердите, что вы не робот"],
        ):
            urls, _ = source.collect_vacancy_urls(keyword="python", max_pages=2)

        self.assertEqual(
            urls,
            [
                "https://hh.ru/vacancy/111111",
                "https://spb.hh.ru/vacancy/222222",
            ],
        )

    def test_browser_mode_collects_listing_after_typed_search(self) -> None:
        source = HhHtmlSource(fetch_mode="browser")

        with patch.object(
            source,
            "_browser_search_first_page",
            return_value=(
                HH_LISTING_HTML,
                "https://hh.ru/search/vacancy?text=python&area=5&schedule=remote&work_format=REMOTE",
            ),
        ) as browser_search:
            urls, page_urls = source.collect_vacancy_urls(
                keyword="python",
                max_pages=1,
            )

        self.assertEqual(len(urls), 2)
        self.assertEqual(page_urls[0], browser_search.return_value[1])
        browser_search.assert_called_once()

    def test_browser_mode_fetches_direct_vacancy_with_browser(self) -> None:
        source = HhHtmlSource(fetch_mode="browser")

        with patch.object(source, "_browser_fetch_html", return_value=HH_VACANCY_HTML) as fetch:
            html = source.fetch_vacancy_page("https://hh.ru/vacancy/111111")

        self.assertIn("Python Automation Engineer", html)
        fetch.assert_called_once_with("https://hh.ru/vacancy/111111")

    def test_build_listing_url_defaults_to_remote_non_russia_belarus_areas(self) -> None:
        self.assertEqual(HhHtmlSource.listing_request_delay_seconds, 15)
        self.assertEqual(HhHtmlSource.vacancy_request_delay_seconds, 15)
        query = parse_qs(urlparse(HhHtmlSource().build_listing_url("n8n")).query)

        self.assertEqual(query["schedule"], ["remote"])
        self.assertEqual(query["work_format"], ["REMOTE"])
        self.assertEqual(query["area"], ["5", "9", "28", "40", "48", "97", "1001"])

    def test_listing_url_filters_are_forced_for_manual_hh_listing_urls(self) -> None:
        source = HhHtmlSource()

        with patch.object(source, "fetch_vacancy_page", return_value=HH_LISTING_HTML):
            _, page_urls = source.collect_vacancy_urls(
                listing_url=(
                    "https://hh.ru/search/vacancy?text=n8n&area=113"
                    "&area=16&schedule=fullDay&page=8"
                ),
                max_pages=1,
            )

        query = parse_qs(urlparse(page_urls[0]).query)
        self.assertEqual(query["page"], ["0"])
        self.assertEqual(query["schedule"], ["remote"])
        self.assertEqual(query["work_format"], ["REMOTE"])
        self.assertEqual(query["area"], ["5", "9", "28", "40", "48", "97", "1001"])

    def test_parse_vacancy_extracts_hh_fields(self) -> None:
        vacancy = HhHtmlSource().parse_vacancy(
            HH_VACANCY_HTML,
            "https://hh.ru/vacancy/111111?from=search",
        )

        self.assertEqual(vacancy.source_name, "hh")
        self.assertEqual(vacancy.source_id, "111111")
        self.assertEqual(vacancy.source_url, "https://hh.ru/vacancy/111111")
        self.assertEqual(vacancy.title, "Python Automation Engineer")
        self.assertEqual(vacancy.company, "Automation Lab")
        self.assertEqual(vacancy.location, "Remote Europe")
        self.assertEqual(vacancy.salary_text, "3 000-4 500 EUR")
        self.assertEqual(vacancy.requirements, ["Python", "SQL"])
        self.assertIn("Build internal automation services", vacancy.responsibilities[0])

    def test_parse_vacancy_rejects_russia_belarus_locations_by_default(self) -> None:
        html = HH_VACANCY_HTML.replace("Remote Europe", "Москва").replace(
            "<main>",
            "<main><span>Можно удалённо</span>",
        )

        with self.assertRaisesRegex(ValueError, "Russia or Belarus"):
            HhHtmlSource().parse_vacancy(html, "https://hh.ru/vacancy/111111")

    def test_registry_routes_hh_url_to_hh_source(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = resolve_source_for_url(
            "https://hh.ru/vacancy/111111?from=search",
            ["hh"],
            data_dir=root / "sample_data",
        )

        self.assertEqual(source.name, "hh")

    def test_parse_vacancy_does_not_fail_on_incidental_captcha_script_text(self) -> None:
        html = HH_VACANCY_HTML.replace("</body>", "<script>window.captchaConfig = {};</script></body>")

        vacancy = HhHtmlSource().parse_vacancy(html, "https://hh.ru/vacancy/111111")

        self.assertEqual(vacancy.title, "Python Automation Engineer")


if __name__ == "__main__":
    unittest.main()
