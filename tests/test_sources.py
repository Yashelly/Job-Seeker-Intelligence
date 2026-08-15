import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from argparse import Namespace

from cvbankas_tracker.main import (
    parse_import_urls,
    parse_search_keywords,
    resolve_source_search_keywords,
)
from cvbankas_tracker.sources import resolve_sources
from cvbankas_tracker.sources.cvbankas import CvbankasSource


class SourceRegistryTests(unittest.TestCase):
    def test_resolve_sample_source_collects_and_parses_vacancies(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = resolve_sources(["sample"], data_dir=root / "sample_data")[0]

        urls, page_urls = source.collect_vacancy_urls(keyword="python", max_pages=1)
        html = source.fetch_vacancy_page(urls[0])
        vacancy = source.parse_vacancy(html, urls[0])

        self.assertEqual(source.name, "sample")
        self.assertEqual(len(page_urls), 1)
        self.assertEqual(len(urls), 2)
        self.assertEqual(vacancy.source_name, "sample")
        self.assertEqual(vacancy.title, "Python Backend Developer")

    def test_unknown_source_name_raises_clear_error(self) -> None:
        root = Path(__file__).resolve().parents[1]

        with self.assertRaisesRegex(ValueError, "Unknown vacancy source"):
            resolve_sources(["missing"], data_dir=root / "sample_data")

    def test_source_registry_applies_hh_options(self) -> None:
        root = Path(__file__).resolve().parents[1]

        source = resolve_sources(
            ["hh"],
            data_dir=root / "sample_data",
            source_options={"hh": {"fetch_mode": "browser", "browser_headless": "false"}},
        )[0]

        self.assertEqual(source.fetch_mode, "browser")
        self.assertFalse(source.browser_headless)

    def test_parse_import_urls_accepts_mixed_separators_and_deduplicates(self) -> None:
        urls = parse_import_urls(
            "https://example.com/one, https://example.com/two\n"
            "https://example.com/three https://example.com/one <https://example.com/four>"
        )

        self.assertEqual(
            urls,
            [
                "https://example.com/one",
                "https://example.com/two",
                "https://example.com/three",
                "https://example.com/four",
            ],
        )

    def test_parse_search_keywords_accepts_lists_and_deduplicates_case_insensitively(self) -> None:
        keywords = parse_search_keywords(
            ["AI automation", " ai automation ", "low-code; no-code"]
        )

        self.assertEqual(keywords, ["AI automation", "low-code; no-code"])

    def test_parse_search_keywords_accepts_cli_string(self) -> None:
        keywords = parse_search_keywords("AI automation, low-code; no-code\nRPA")

        self.assertEqual(keywords, ["AI automation", "low-code", "no-code", "RPA"])

    def test_source_search_keywords_use_source_override_before_global_fallback(self) -> None:
        args = Namespace(search_keywords=["global one", "global two"])
        cfg = {
            "sources": {
                "keywords": {
                    "hh": ["специалист по автоматизации", "инженер автоматизации"]
                }
            }
        }

        self.assertEqual(
            resolve_source_search_keywords("hh", args, cfg),
            ["специалист по автоматизации", "инженер автоматизации"],
        )
        self.assertEqual(
            resolve_source_search_keywords("cvbankas", args, cfg),
            ["global one", "global two"],
        )

    def test_cvbankas_source_requires_remote_marker(self) -> None:
        html = """
        <html>
          <body>
            <h1 id="jobad_heading1">AI Automation Specialist</h1>
            <div id="jobad_location">Vilnius - Automation Studio</div>
            <section><div class="jobad_txt">Build automations.</div></section>
          </body>
        </html>
        """

        with self.assertRaisesRegex(ValueError, "not marked as remote"):
            CvbankasSource().parse_vacancy(
                html,
                "https://www.cvbankas.lt/ai-automation-specialist-vilniuje/1-11111111",
            )

    def test_cvbankas_source_accepts_darbas_namuose_marker(self) -> None:
        html = """
        <html>
          <body>
            <h1 id="jobad_heading1">AI Automation Specialist</h1>
            <div id="jobad_location">Darbas namuose - Automation Studio</div>
            <section><div class="jobad_txt">Build automations nuotoliniu būdu.</div></section>
          </body>
        </html>
        """

        vacancy = CvbankasSource().parse_vacancy(
            html,
            "https://www.cvbankas.lt/ai-automation-specialist-darbas-namuose/1-11111111",
        )

        self.assertEqual(vacancy.location, "Darbas namuose")


if __name__ == "__main__":
    unittest.main()
