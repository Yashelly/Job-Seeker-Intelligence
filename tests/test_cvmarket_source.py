import html
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.sources import resolve_sources
from cvbankas_tracker.sources.cvmarket import CvMarketSource


def listing_page(*urls: str, next_url: str = "") -> str:
    next_link = f'<link href="{html.escape(next_url)}" rel="next">' if next_url else ""
    cards = "".join(
        f'<article data-component="jobad"><a href="{url}" class="card jobad-url">Role</a></article>'
        for url in urls
    )
    return f"<html><head>{next_link}</head><body>{cards}</body></html>"


class CvMarketSourceTests(unittest.TestCase):
    def test_build_listing_url_uses_keyword_and_newest_first_sort(self) -> None:
        source = CvMarketSource()

        url = source.build_listing_url("AI automation")

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/darbo-skelbimai")
        self.assertEqual(query["op"], ["search"])
        self.assertEqual(query["search[keyword]"], ["AI automation"])
        self.assertEqual(query["sort"], ["activation_date"])
        self.assertNotIn("start", query)
        self.assertEqual(
            parse_qs(urlparse(source.build_paged_url(url, 2)).query)["start"],
            ["60"],
        )

    def test_collection_follows_next_link_and_stops_at_known_vacancy(self) -> None:
        source = CvMarketSource()
        first = "/ai-engineer-vilnius-company-2292802"
        known = "/automation-engineer-kaunas-company-2292801"
        later = "/data-engineer-vilnius-company-2292800"
        first_page = listing_page(
            first,
            known,
            next_url=(
                "/darbo-skelbimai/darbo-skelbimai?op=search&"
                "search%5Bkeyword%5D=automation&sort=activation_date&start=30"
            ),
        )
        second_page = listing_page(later)

        with patch.object(source, "fetch_vacancy_page", side_effect=[first_page, second_page]) as fetch:
            urls, pages = source.collect_vacancy_urls(
                keyword="automation",
                max_pages=10,
                stop_at_vacancy=lambda url: url.endswith("-2292801"),
            )

        self.assertEqual(urls, ["https://www.cvmarket.lt/ai-engineer-vilnius-company-2292802"])
        self.assertEqual(len(pages), 1)
        fetch.assert_called_once()

    def test_collection_normalizes_cvmarket_next_link(self) -> None:
        source = CvMarketSource()
        first_page = listing_page(
            "/ai-engineer-vilnius-company-2292802",
            next_url="/darbo-skelbimai/darbo-skelbimai?sort=activation_date&start=30",
        )
        second_page = listing_page("/data-engineer-vilnius-company-2292800")

        with patch.object(source, "fetch_vacancy_page", side_effect=[first_page, second_page]) as fetch:
            urls, pages = source.collect_vacancy_urls(max_pages=10)

        self.assertEqual(len(urls), 2)
        self.assertEqual(len(pages), 2)
        self.assertEqual(urlparse(pages[1]).path, "/darbo-skelbimai")
        self.assertEqual(parse_qs(urlparse(pages[1]).query)["start"], ["30"])
        self.assertEqual(fetch.call_count, 2)

    def test_parse_jobposting_resolves_location_salary_company_and_requirements(self) -> None:
        source = CvMarketSource()
        organization_id = "https://www.cvmarket.lt/#/schema/Organization/1911"
        place_id = "https://www.cvmarket.lt/#/schema/Place/listing-2292802"
        address_id = "https://www.cvmarket.lt/#/schema/PostalAddress/listing-2292802"
        payload = {
            "@context": "https://schema.org",
            "@graph": [
                [
                    {
                        "@type": "JobPosting",
                        "title": "AI Automation Engineer",
                        "description": (
                            "<p>Job Description</p><p>Build &quot;AI&quot; API automations.</p>"
                            "<p>Requirements</p><ul><li>Python</li><li>n8n</li></ul>"
                            "<p>Company offers</p><p>Learning budget.</p>"
                        ),
                        "hiringOrganization": {"@id": organization_id},
                        "jobLocation": {"@id": place_id},
                        "baseSalary": {
                            "@type": "MonetaryAmount",
                            "currency": "EUR",
                            "value": {
                                "@type": "QuantitativeValue",
                                "unitText": "MONTH",
                                "minValue": 2500,
                                "maxValue": 3500,
                            },
                        },
                    },
                    {"@type": "Place", "@id": place_id, "address": {"@id": address_id}},
                    {
                        "@type": "PostalAddress",
                        "@id": address_id,
                        "addressRegion": "Vilnius",
                        "addressCountry": "LT",
                    },
                ]
            ],
        }
        page = (
            f'<script type="application/ld+json">{json.dumps(payload)}</script>'
            '<a href="/automation-uab-imones-darbo-skelbimai-1911">Automation UAB</a>'
        )

        vacancy = source.parse_vacancy(
            page,
            "https://www.cvmarket.lt/ai-automation-engineer-vilnius-automation-uab-2292802",
        )

        self.assertEqual(vacancy.source_name, "cvmarket")
        self.assertEqual(vacancy.source_id, "2292802")
        self.assertEqual(vacancy.title, "AI Automation Engineer")
        self.assertEqual(vacancy.company, "Automation UAB")
        self.assertEqual(vacancy.location, "Vilnius, LT")
        self.assertEqual(vacancy.salary_text, "2500-3500 EUR MONTH")
        self.assertEqual(vacancy.requirements, ["Python", "n8n"])
        self.assertIn('Build "AI" API automations', vacancy.responsibilities[0])

    def test_url_handling_rejects_non_vacancy_and_foreign_urls(self) -> None:
        source = CvMarketSource()

        self.assertTrue(source.can_handle_url("https://www.cvmarket.lt/ai-engineer-company-2292802"))
        self.assertFalse(
            source.can_handle_url("https://www.cvmarket.lt/company-imones-darbo-skelbimai-1911")
        )
        self.assertFalse(source.can_handle_url("https://example.com/ai-engineer-company-2292802"))
        with self.assertRaisesRegex(ValueError, "cvmarket.lt"):
            source.collect_vacancy_urls(listing_url="https://example.com/jobs")

    def test_registry_exposes_cvmarket(self) -> None:
        root = Path(__file__).resolve().parents[1]

        source = resolve_sources(["cvmarket"], data_dir=root / "sample_data")[0]

        self.assertIsInstance(source, CvMarketSource)


if __name__ == "__main__":
    unittest.main()
