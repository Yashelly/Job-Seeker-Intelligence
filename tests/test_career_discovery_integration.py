from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from cvbankas_tracker.companies import detect_ats, normalize_company
from cvbankas_tracker.sources.careers import CareerPagesSource, _public_connection, _validate_public_url


class CareerDiscoveryIntegrationTests(unittest.TestCase):
    def test_new_board_detection(self):
        for url, expected in (
            ("https://apply.workable.com/acme/", ("workable", "acme")),
            ("https://acme.recruitee.com/", ("recruitee", "acme")),
            ("https://acme.jobs.personio.com/", ("personio", "acme")),
            ("https://jobs.smartrecruiters.com/Acme/123", ("smartrecruiters", "Acme")),
            ("https://apply.workable.com/j/123", ("unknown", None)),
        ):
            self.assertEqual(detect_ats(url), expected)

    def test_public_network_boundary(self):
        for url in ("http://localhost/", "http://169.254.169.254/", "https://example.com:8000/"):
            with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, 1, 6, "", ("127.0.0.1", 80))]):
                with self.assertRaises(ValueError):
                    _validate_public_url(url)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, 1, 6, "", ("8.8.8.8", 443))]):
            _validate_public_url("https://example.com/jobs")

    def test_html_page_reaches_common_vacancy_parser(self):
        company = normalize_company({"name": "Example", "career_url": "https://example.com/jobs", "collection_enabled": True})
        source = CareerPagesSource()
        page = '<script type="application/ld+json">{"@type":"JobPosting","title":"Engineer","description":"Build systems","url":"https://example.com/jobs/1"}</script>'
        with patch.object(source, "_fetch_public_text", return_value=page):
            jobs, _, partial = source._collect_company_jobs(company=company, provider="html", token="", max_pages=10, before_listing_fetch=None)
        self.assertFalse(partial)
        source._job_cache[jobs[0].source_url] = jobs[0]
        vacancy = source.parse_vacancy(source.fetch_vacancy_page(jobs[0].source_url), jobs[0].source_url)
        self.assertEqual(vacancy.title, "Engineer")
        self.assertEqual(vacancy.raw_text, "Build systems")

    def test_connection_uses_checked_numeric_address(self):
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, 1, 6, "", ("8.8.8.8", 443))]), patch("socket.create_connection") as connect:
            _public_connection(("example.com", 443), 10)
        connect.assert_called_once_with(("8.8.8.8", 443), 10, None)

    def test_failed_secondary_board_preserves_jobs_and_marks_partial(self):
        company = {"name": "Group", "company_id": "group", "ats_sources": [
            {"ats_type": "ashby", "ats_token": "working"},
            {"ats_type": "ashby", "ats_token": "broken"},
        ]}
        source = CareerPagesSource()
        def fetch(url, callback):
            if "/broken?" in url:
                raise ValueError("Unavailable")
            return {"jobs": [{"id": "1", "title": "Engineer", "jobUrl": "https://example.com/jobs/1", "descriptionPlain": "Build systems"}]}
        with patch.object(source, "_fetch_json", side_effect=fetch):
            jobs, _, partial = source._collect_company_jobs(company=company, provider="ashby", token="working", max_pages=10, before_listing_fetch=None)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(partial)

    def test_shared_greenhouse_board_filters_company_metadata(self):
        source = CareerPagesSource()
        company = {"company_id": "cognigy", "name": "Cognigy", "greenhouse_metadata_filter": {"id": "42", "value": "Cognigy"}}
        items = [{"id": number, "title": "Engineer", "absolute_url": f"https://example.com/jobs/{number}", "content": "Build systems", "metadata": [{"id": 42, "value": name}]} for number, name in ((1, "Cognigy"), (2, "Other"))]
        with patch.object(source, "_fetch_json", return_value={"jobs": items, "meta": {"total": 2}}):
            jobs, _, partial = source._collect_greenhouse(company, "nice", None)
        self.assertEqual([job.source_url for job in jobs], ["https://example.com/jobs/1"])
        self.assertFalse(partial)
