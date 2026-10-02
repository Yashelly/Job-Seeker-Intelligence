import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.main import resolve_source_options
from cvbankas_tracker.sources import resolve_sources
from cvbankas_tracker.sources.base import CollectionCancelledError
from cvbankas_tracker.sources.careers import CareerPagesSource


class _Headers:
    def get_content_charset(self) -> str:
        return "utf-8"


class _Response:
    headers = _Headers()

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        content = json.dumps(self.payload).encode("utf-8")
        return content if size is None or size < 0 else content[:size]


class _Registry:
    def __init__(self, companies: list[dict]) -> None:
        self.companies = companies
        self.scans: list[tuple[str, str, str, int | None]] = []

    def list_companies(self, query: str = "", pool: str = "", enabled_only: bool = False) -> list[dict]:
        return [
            company
            for company in self.companies
            if not enabled_only or company.get("collection_enabled")
        ]

    def record_scan(
        self,
        company_id: str,
        status: str,
        error: str = "",
        jobs_count: int | None = None,
    ) -> None:
        self.scans.append((company_id, status, error, jobs_count))


class _Opener:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    def open(self, request: object, timeout: int = 20) -> _Response:
        url = request.full_url
        if url not in self.responses:
            raise AssertionError(f"Unexpected URL: {url}")
        payload = self.responses[url]
        if isinstance(payload, Exception):
            raise payload
        return _Response(payload)


def _opener_from(responses: dict[str, object]):
    return lambda *_args, **_kwargs: _Opener(responses)


class CareerPagesSourceTests(unittest.TestCase):
    def test_cancellation_does_not_mark_companies_failed(self) -> None:
        registry = _Registry([{
            "company_id": "example", "name": "Example", "ats_type": "ashby",
            "ats_token": "example", "collection_enabled": True,
        }])
        source = CareerPagesSource(company_registry=registry)

        def cancel(url: str) -> None:
            raise CollectionCancelledError("Collection cancelled.")

        with self.assertRaises(CollectionCancelledError):
            source.collect_vacancy_urls(before_listing_fetch=cancel)
        self.assertEqual(registry.scans, [])
        self.assertEqual(source.collection_errors, [])

    def test_ashby_remote_flag_is_preserved_in_vacancy_location(self) -> None:
        source = CareerPagesSource()
        job = source._ashby_job({"name": "Example", "company_id": "example", "ats_token": "example"}, {
            "id": "job", "title": "Engineer", "jobUrl": "https://jobs.ashbyhq.com/example/job",
            "location": "Europe", "isRemote": True, "department": "Engineering",
        })
        self.assertIn("Remote", job.location)
        self.assertNotIn("Engineering", job.location)

    def test_registry_resolves_careers_with_database_path_option(self) -> None:
        root = Path(__file__).resolve().parents[1]
        options = resolve_source_options({}, root / "jobs.db")

        (source,) = resolve_sources(
            ["careers"],
            data_dir=root / "sample_data",
            source_options=options,
        )

        self.assertIsInstance(source, CareerPagesSource)
        self.assertEqual(source.db_path, str(root / "jobs.db"))

    def test_ashby_collects_jobs_once_and_parses_from_cache(self) -> None:
        registry = _Registry(
            [
                {
                    "company_id": "acme",
                    "name": "Acme",
                    "ats_type": "ashby",
                    "ats_token": "acme",
                    "collection_enabled": True,
                }
            ]
        )
        source = CareerPagesSource(company_registry=registry)
        api_url = "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"
        responses = {
            api_url: {
                "jobs": [
                    {
                        "id": "job-1",
                        "title": "AI Automation Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/job-1",
                        "locationName": "Remote",
                        "descriptionHtml": "<p>Build workflow automations.</p>",
                        "compensation": {"compensationTierSummary": "EUR 3000-5000"},
                        "isListed": True,
                    }
                ]
            }
        }

        with patch("cvbankas_tracker.sources.careers.build_opener", side_effect=_opener_from(responses)):
            urls, page_urls = source.collect_vacancy_urls(max_pages=1)
            vacancy = source.parse_vacancy(source.fetch_vacancy_page(urls[0]), urls[0])

        self.assertEqual(page_urls, [api_url])
        self.assertEqual(urls, ["https://jobs.ashbyhq.com/acme/job-1"])
        self.assertEqual(vacancy.source_name, "careers")
        self.assertEqual(vacancy.source_id, "acme:job-1")
        self.assertEqual(vacancy.company, "Acme")
        self.assertEqual(vacancy.salary_text, "EUR 3000-5000")
        self.assertEqual(registry.scans, [("acme", "completed", "", 1)])

    def test_company_failure_is_isolated_and_recorded(self) -> None:
        registry = _Registry(
            [
                {
                    "company_id": "broken",
                    "name": "Broken",
                    "ats_type": "greenhouse",
                    "ats_token": "broken",
                    "collection_enabled": True,
                },
                {
                    "company_id": "ok",
                    "name": "OK",
                    "ats_type": "greenhouse",
                    "ats_token": "ok",
                    "collection_enabled": True,
                },
            ]
        )
        source = CareerPagesSource(company_registry=registry)
        responses = {
            "https://boards-api.greenhouse.io/v1/boards/broken/jobs?content=true": OSError("boom"),
            "https://boards-api.greenhouse.io/v1/boards/ok/jobs?content=true": {
                "jobs": [
                    {
                        "id": 10,
                        "title": "Platform Engineer",
                        "absolute_url": "https://boards.greenhouse.io/ok/jobs/10",
                        "location": {"name": "Vilnius"},
                        "content": "<p>Own platform reliability.</p>",
                    }
                ],
                "meta": {"total": 1},
            },
        }

        with patch("cvbankas_tracker.sources.careers.build_opener", side_effect=_opener_from(responses)):
            urls, _page_urls = source.collect_vacancy_urls(max_pages=1)

        self.assertEqual(urls, ["https://boards.greenhouse.io/ok/jobs/10"])
        self.assertEqual(len(source.collection_errors), 1)
        self.assertEqual(registry.scans[0][0:2], ("broken", "failed"))
        self.assertEqual(registry.scans[1], ("ok", "completed", "", 1))

    def test_lever_uses_eu_host_from_career_url_and_paginates(self) -> None:
        registry = _Registry(
            [
                {
                    "company_id": "euco",
                    "name": "EU Co",
                    "career_url": "https://jobs.eu.lever.co/euco",
                    "ats_type": "lever",
                    "ats_token": "euco",
                    "collection_enabled": True,
                }
            ]
        )
        source = CareerPagesSource(company_registry=registry, lever_page_size=2)
        responses = {
            "https://api.eu.lever.co/v0/postings/euco?mode=json&skip=0&limit=2": [
                {
                    "id": "a",
                    "text": "Workflow Engineer",
                    "hostedUrl": "https://jobs.eu.lever.co/euco/a",
                    "categories": {"location": "Remote"},
                    "descriptionPlain": "Build automations.",
                },
                {
                    "id": "b",
                    "text": "Integration Engineer",
                    "hostedUrl": "https://jobs.eu.lever.co/euco/b",
                    "categories": {"location": "Vilnius"},
                    "descriptionPlain": "Build integrations.",
                },
            ],
            "https://api.eu.lever.co/v0/postings/euco?mode=json&skip=2&limit=2": [
                {
                    "id": "c",
                    "text": "AI Engineer",
                    "hostedUrl": "https://jobs.eu.lever.co/euco/c",
                    "categories": {"location": "Remote"},
                    "descriptionPlain": "Build AI tools.",
                }
            ],
        }

        with patch("cvbankas_tracker.sources.careers.build_opener", side_effect=_opener_from(responses)):
            urls, page_urls = source.collect_vacancy_urls(max_pages=3)

        self.assertEqual(len(urls), 3)
        self.assertEqual(
            page_urls,
            [
                "https://api.eu.lever.co/v0/postings/euco?mode=json&skip=0&limit=2",
                "https://api.eu.lever.co/v0/postings/euco?mode=json&skip=2&limit=2",
            ],
        )
        self.assertEqual(registry.scans, [("euco", "completed", "", 3)])

    def test_lever_marks_scan_partial_when_page_cap_can_truncate_results(self) -> None:
        registry = _Registry(
            [
                {
                    "company_id": "bigco",
                    "name": "Big Co",
                    "ats_type": "lever",
                    "ats_token": "bigco",
                    "collection_enabled": True,
                }
            ]
        )
        source = CareerPagesSource(company_registry=registry, lever_page_size=2)
        responses = {
            "https://api.lever.co/v0/postings/bigco?mode=json&skip=0&limit=2": [
                {
                    "id": "a",
                    "text": "Workflow Engineer",
                    "hostedUrl": "https://jobs.lever.co/bigco/a",
                    "descriptionPlain": "Build automations.",
                },
                {
                    "id": "b",
                    "text": "Integration Engineer",
                    "hostedUrl": "https://jobs.lever.co/bigco/b",
                    "descriptionPlain": "Build integrations.",
                },
            ]
        }

        with patch("cvbankas_tracker.sources.careers.build_opener", side_effect=_opener_from(responses)):
            urls, _page_urls = source.collect_vacancy_urls(max_pages=1)

        self.assertEqual(len(urls), 2)
        self.assertEqual(len(source.collection_incomplete_reasons), 1)
        self.assertEqual(registry.scans[0][0:2], ("bigco", "partial"))

    def test_no_enabled_supported_companies_is_explicit_error(self) -> None:
        source = CareerPagesSource(company_registry=_Registry([]))

        with self.assertRaisesRegex(ValueError, "No enabled career sources"):
            source.collect_vacancy_urls(max_pages=1)

    def test_response_byte_limit_rejects_oversized_payloads(self) -> None:
        registry = _Registry(
            [
                {
                    "company_id": "big",
                    "name": "Big",
                    "ats_type": "greenhouse",
                    "ats_token": "big",
                    "collection_enabled": True,
                }
            ]
        )
        source = CareerPagesSource(company_registry=registry, max_response_bytes=1024)
        responses = {
            "https://boards-api.greenhouse.io/v1/boards/big/jobs?content=true": {
                "jobs": [{"id": 1, "title": "Big", "content": "x" * 2000}],
            }
        }

        with patch("cvbankas_tracker.sources.careers.build_opener", side_effect=_opener_from(responses)):
            urls, _page_urls = source.collect_vacancy_urls(max_pages=1)

        self.assertEqual(urls, [])
        self.assertIn("exceeds the configured byte limit", source.collection_errors[0])
        self.assertEqual(registry.scans[0][0:2], ("big", "failed"))


if __name__ == "__main__":
    unittest.main()
