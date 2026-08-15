from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.sources import resolve_source_for_url, resolve_sources
from cvbankas_tracker.sources.euremotejobs import EuRemoteJobsSource
from cvbankas_tracker.sources.justjoin_it import JustJoinItSource
from cvbankas_tracker.sources.startup_jobs import StartupJobsSource


JOB_POSTING_HTML = """
<html>
  <head>
    <script type="application/ld+json">
    {
      "@type": "JobPosting",
      "title": "AI Workflow Automation Specialist",
      "hiringOrganization": {"name": "Automation Studio"},
      "jobLocation": {
        "address": {
          "addressLocality": "Remote",
          "addressCountry": "Europe"
        }
      },
      "baseSalary": {
        "currency": "EUR",
        "value": {"minValue": 3000, "maxValue": 5000, "unitText": "MONTH"}
      },
      "description": "Build no-code automations with n8n, Zapier, and AI agents."
    }
    </script>
  </head>
  <body>
    <main>
      <span class="skill">n8n</span>
      <span class="skill">Zapier</span>
    </main>
  </body>
</html>
"""


class AdditionalSourcesTests(unittest.TestCase):
    def test_startup_jobs_source_collects_and_parses(self) -> None:
        source = StartupJobsSource()
        listing = """
        <a href="/ai-workflow-automation-specialist-123456">Job</a>
        <a href="https://startup.jobs/ai-workflow-automation-specialist-123456?ref=x">Duplicate</a>
        """

        urls = source.collect_listing_urls(listing)
        vacancy = source.parse_vacancy(JOB_POSTING_HTML, urls[0])

        self.assertEqual(urls, ["https://startup.jobs/ai-workflow-automation-specialist-123456"])
        self.assertEqual(vacancy.source_name, "startup_jobs")
        self.assertEqual(vacancy.source_id, "ai-workflow-automation-specialist-123456")
        self.assertEqual(vacancy.company, "Automation Studio")
        self.assertIn("n8n", vacancy.requirements)

    def test_startup_jobs_search_url_uses_remote_filters(self) -> None:
        url = StartupJobsSource().build_listing_url("AI automation", page=2)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/remote-jobs")
        self.assertEqual(query["q"], ["AI automation"])
        self.assertEqual(query["remote"], ["true"])
        self.assertEqual(query["w"], ["remote"])
        self.assertEqual(query["page"], ["2"])

    def test_justjoin_source_collects_and_parses(self) -> None:
        source = JustJoinItSource()
        listing = """
        <a href="/job-offer/automation-studio-ai-workflow-specialist-remote">Job</a>
        <a href="/companies/automation-studio">Company</a>
        """

        urls = source.collect_listing_urls(listing)
        vacancy = source.parse_vacancy(JOB_POSTING_HTML, urls[0])

        self.assertEqual(
            urls,
            ["https://justjoin.it/job-offer/automation-studio-ai-workflow-specialist-remote"],
        )
        self.assertEqual(vacancy.source_name, "justjoin")
        self.assertEqual(vacancy.source_id, "automation-studio-ai-workflow-specialist-remote")
        self.assertEqual(vacancy.location, "Remote, Europe")

    def test_justjoin_search_url_uses_remote_workplace_filter(self) -> None:
        url = JustJoinItSource().build_listing_url("AI automation", page=3)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/job-offers/all-locations")
        self.assertEqual(query["keyword"], ["AI automation"])
        self.assertEqual(query["workplace"], ["remote"])
        self.assertEqual(query["page"], ["3"])

    def test_euremotejobs_source_collects_and_parses(self) -> None:
        source = EuRemoteJobsSource()
        listing = """
        <a href="/job/ai-workflow-automation-specialist/">Job</a>
        <a href="/category/software-development/">Category</a>
        """

        urls = source.collect_listing_urls(listing)
        vacancy = source.parse_vacancy(JOB_POSTING_HTML, urls[0])

        self.assertEqual(urls, ["https://euremotejobs.com/job/ai-workflow-automation-specialist/"])
        self.assertEqual(vacancy.source_name, "euremotejobs")
        self.assertEqual(vacancy.source_id, "ai-workflow-automation-specialist")
        self.assertEqual(vacancy.salary_text, "3000-5000 EUR MONTH")

    def test_euremotejobs_search_url_uses_europe_region_path(self) -> None:
        source = EuRemoteJobsSource()
        first_page = source.build_listing_url("AI automation")
        second_page = source.build_paged_url(first_page, 1)

        self.assertEqual(source.listing_request_delay_seconds, 15)
        self.assertEqual(source.vacancy_request_delay_seconds, 15)
        self.assertEqual(
            first_page,
            "https://euremotejobs.com/job-region/remote-jobs-europe/?s=AI+automation",
        )
        self.assertEqual(
            second_page,
            "https://euremotejobs.com/job-region/remote-jobs-europe/page/2/?s=AI+automation",
        )

    def test_generic_remote_sources_reject_russia_belarus_locations(self) -> None:
        html = JOB_POSTING_HTML.replace("Remote", "Moscow").replace("Europe", "Russia")

        with self.assertRaisesRegex(ValueError, "Russia or Belarus"):
            JustJoinItSource().parse_vacancy(
                html,
                "https://justjoin.it/job-offer/company-role-remote",
            )

    def test_generic_source_reports_blocked_listing_pages(self) -> None:
        source = JustJoinItSource()
        delayed_urls = []

        with patch.object(source, "fetch_vacancy_page", return_value="checking your browser sgcaptcha"):
            with self.assertRaisesRegex(ValueError, "not publicly accessible"):
                source.collect_vacancy_urls(
                    keyword="AI automation",
                    max_pages=1,
                    before_listing_fetch=delayed_urls.append,
                )
        self.assertEqual(len(delayed_urls), 1)

    def test_registry_resolves_new_sources_and_routes_urls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sources = resolve_sources(
            ["startup_jobs", "justjoin", "euremotejobs"],
            data_dir=root / "sample_data",
        )

        self.assertEqual([source.name for source in sources], ["startup_jobs", "justjoin", "euremotejobs"])
        self.assertEqual(
            resolve_source_for_url(
                "https://justjoin.it/job-offer/company-role",
                ["justjoin"],
                data_dir=root / "sample_data",
            ).name,
            "justjoin",
        )


class BrowserModeTests(unittest.TestCase):
    _CF_SCRIPT = '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/x.js"></script>'

    def test_from_options_enables_browser_mode(self) -> None:
        source = StartupJobsSource.from_options({"fetch_mode": "browser"})
        self.assertTrue(source._uses_browser())
        self.assertEqual(EuRemoteJobsSource.from_options({}).fetch_mode, "http")
        self.assertEqual(StartupJobsSource().fetch_mode, "http")

    def test_looks_blocked_ignores_benign_cloudflare_script(self) -> None:
        source = StartupJobsSource()
        page = f"<html><head>{self._CF_SCRIPT}</head><body><a href='/role-123'>Job</a></body></html>"
        self.assertFalse(source._looks_blocked(page))

    def test_looks_blocked_detects_real_interstitial(self) -> None:
        source = StartupJobsSource()
        self.assertTrue(
            source._looks_blocked("<html><body>Just a moment... Checking your browser</body></html>")
        )

    def test_collect_does_not_flag_blocked_when_links_present(self) -> None:
        source = StartupJobsSource()
        page = (
            f"<html><head>{self._CF_SCRIPT}</head>"
            '<body><a href="/ai-workflow-automation-specialist-123456">Job</a></body></html>'
        )
        with patch.object(source, "fetch_vacancy_page", return_value=page):
            urls, _ = source.collect_vacancy_urls(keyword="ai automation", max_pages=1)
        self.assertEqual(urls, ["https://startup.jobs/ai-workflow-automation-specialist-123456"])

    def test_browser_mode_routes_fetch_through_browser(self) -> None:
        source = EuRemoteJobsSource.from_options({"fetch_mode": "browser"})
        with patch.object(source, "_browser_fetcher") as fetcher:
            fetcher.return_value.fetch_html.return_value = "<html>ok</html>"
            result = source.fetch_vacancy_page("https://euremotejobs.com/job/x/")
        self.assertEqual(result, "<html>ok</html>")
        fetcher.return_value.fetch_html.assert_called_once_with("https://euremotejobs.com/job/x/")

    def test_close_releases_browser(self) -> None:
        source = StartupJobsSource.from_options({"fetch_mode": "browser"})

        class _FakeBrowser:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        fake = _FakeBrowser()
        source._browser = fake
        source.close()
        self.assertTrue(fake.closed)
        self.assertIsNone(source._browser)

    def test_registry_passes_browser_options_from_config(self) -> None:
        root = Path(__file__).resolve().parents[1]
        (source,) = resolve_sources(
            ["startup_jobs"],
            data_dir=root / "sample_data",
            source_options={"startup_jobs": {"fetch_mode": "browser"}},
        )
        self.assertTrue(source._uses_browser())


if __name__ == "__main__":
    unittest.main()
