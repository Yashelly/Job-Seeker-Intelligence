import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.sources.cvonline import CvOnlineSource


def listing_page(*vacancies: dict) -> str:
    links = "".join(
        f'<a href="/vacancy/{vacancy["id"]}/company/role-{vacancy["id"]}">role</a>'
        for vacancy in vacancies
    )
    payload = {"props": {"pageProps": {"searchResults": {"vacancies": list(vacancies)}}}}
    return f'<html><body>{links}<script id="__NEXT_DATA__">{json.dumps(payload)}</script></body></html>'


class CvOnlineSourceTests(unittest.TestCase):
    def test_build_listing_url_uses_unfiltered_newest_first_pages(self) -> None:
        source = CvOnlineSource()

        url = source.build_listing_url(keyword="ignored", page=2)

        query = parse_qs(urlparse(url).query)
        self.assertEqual(urlparse(url).path, "/lt/search")
        self.assertEqual(query["limit"], ["100"])
        self.assertEqual(query["offset"], ["200"])
        self.assertEqual(query["sort"], ["created"])
        self.assertFalse(source.uses_search_keywords)
        self.assertTrue(source.requires_database)
        self.assertEqual(
            source.collection_rule,
            "unfiltered_full_feed_then_database_incremental",
        )

    def test_filtered_listing_url_is_rejected(self) -> None:
        source = CvOnlineSource()

        with self.assertRaisesRegex(ValueError, "does not support filtered --listing-url"):
            source.collect_vacancy_urls(
                listing_url="https://www.cvonline.lt/lt/search?keywords%5B0%5D=python"
            )

    def test_listing_payload_is_cached_and_parsed_without_a_vacancy_request(self) -> None:
        source = CvOnlineSource()
        vacancy = {
            "id": 123,
            "positionTitle": "Automation Engineer",
            "positionContent": "Build n8n workflows and API integrations.",
            "employerName": "Automation UAB",
            "salaryFrom": 2500,
            "salaryTo": 3500,
            "hourlySalary": False,
            "remoteWork": True,
            "skills": ["n8n"],
            "keywords": ["API"],
        }

        with patch.object(source, "_fetch_page", return_value=listing_page(vacancy)) as fetch:
            urls, pages = source.collect_vacancy_urls(max_pages=1)
            raw = source.fetch_vacancy_page(urls[0])

        parsed = source.parse_vacancy(raw, urls[0])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(pages), 1)
        self.assertEqual(parsed.source_id, "123")
        self.assertEqual(parsed.title, "Automation Engineer")
        self.assertEqual(parsed.company, "Automation UAB")
        self.assertEqual(parsed.location, "Remote")
        self.assertEqual(parsed.salary_text, "2500–3500 EUR")
        self.assertEqual(parsed.requirements, ["n8n", "API"])
        self.assertIn("n8n workflows", parsed.responsibilities[0])

    def test_collection_stops_after_short_page(self) -> None:
        source = CvOnlineSource()
        first = {
            "id": 1,
            "positionTitle": "Role",
            "positionContent": "Description",
            "employerName": "Company",
        }
        with patch.object(source, "_fetch_page", return_value=listing_page(first)) as fetch:
            urls, pages = source.collect_vacancy_urls(max_pages=10)

        self.assertEqual(len(urls), 1)
        self.assertEqual(len(pages), 1)
        self.assertEqual(fetch.call_count, 1)

    def test_collection_stops_at_first_known_vacancy(self) -> None:
        source = CvOnlineSource()
        vacancies = [
            {
                "id": index,
                "positionTitle": f"Role {index}",
                "positionContent": "Description",
                "employerName": "Company",
            }
            for index in range(1, 101)
        ]

        with patch.object(source, "_fetch_page", return_value=listing_page(*vacancies)) as fetch:
            urls, pages = source.collect_vacancy_urls(
                max_pages=100,
                stop_at_vacancy=lambda url: "/vacancy/2/" in url,
            )

        self.assertEqual(len(urls), 1)
        self.assertIn("/vacancy/1/", urls[0])
        self.assertEqual(len(pages), 1)
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
