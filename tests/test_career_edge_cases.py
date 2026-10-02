from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cvbankas_tracker.companies import CompanyRegistry
from cvbankas_tracker.sources.base import CollectionCancelledError
from cvbankas_tracker.sources.career_feeds import collect_feed
from cvbankas_tracker.sources.career_html import collect_html
from cvbankas_tracker.sources.careers import CareerPagesSource
from cvbankas_tracker.storage import DatabaseManager
from tests.test_career_sources import _opener_from, _Registry


class CareerEdgeCasesTests(unittest.TestCase):
    def registry(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = DatabaseManager(Path(directory.name) / "edge.db")
        database.initialize(create_backup=False)
        return CompanyRegistry(database)

    def test_short_rss_description_uses_detail_even_with_ats_url(self):
        feed_url = "https://careers.example.com/jobs.rss"
        detail_url = "https://careers.example.com/jobs/1-engineer"
        rss = f"<rss><channel><item><title>Engineer</title><link>{detail_url}</link><description>Teaser.</description></item></channel></rss>"
        detail = '<script type="application/ld+json">{"@type":"JobPosting","title":"Engineer","description":"Build robust systems for our customers.","url":"' + detail_url + '"}</script>'
        jobs, _, partial = collect_feed("teamtailor", {"name": "Example", "career_url": "https://careers.example.com/", "ats_url": feed_url}, 10, lambda url: {}, {feed_url: rss, detail_url: detail}.__getitem__)
        self.assertEqual(len(jobs), 1)
        self.assertFalse(partial)
        self.assertIn("robust systems", jobs[0]["description"])

    def test_failed_detail_is_not_reported_as_successful_empty_page(self):
        page_url = "https://example.com/careers"
        def fetch(url):
            if url == page_url:
                return '<h1>Careers</h1><a href="/jobs/1-engineer">Engineer</a><div hidden>No current openings</div>'
            raise OSError("Unavailable")
        with self.assertRaises(ValueError):
            collect_html({"career_url": page_url}, 10, fetch)

    def test_replacing_board_clears_hidden_old_sources(self):
        registry = self.registry()
        registry.save({"name": "Example", "career_url": "https://example.com/careers", "ats_url": "https://jobs.ashbyhq.com/old", "collection_enabled": True,
                       "ats_sources": [{"ats_type": "ashby", "ats_token": "old", "ats_url": "https://jobs.ashbyhq.com/old"}],
                       "greenhouse_api_url": "https://example.com/old-api", "greenhouse_metadata_filter": {"id": "1", "value": "Old"}, "vacancy_company_name": "Old Employer"})
        updated = registry.save({"ats_url": "https://jobs.lever.co/new"}, company_id="example")
        self.assertFalse(updated.get("ats_sources"))
        self.assertFalse(updated.get("greenhouse_api_url"))
        self.assertFalse(updated.get("greenhouse_metadata_filter"))
        self.assertFalse(updated.get("vacancy_company_name"))

    def test_source_change_during_scan_cannot_restore_old_completed_status(self):
        registry = self.registry()
        registry.save({"name": "Example", "career_url": "https://jobs.ashbyhq.com/old", "collection_enabled": True})
        source = CareerPagesSource(company_registry=registry)
        def fetch(url, callback):
            registry.save({"career_url": "https://jobs.lever.co/new"}, company_id="example")
            return {"jobs": []}
        with patch.object(source, "_fetch_json", side_effect=fetch):
            source.collect_vacancy_urls(max_pages=1)
        self.assertIsNone(registry.get("example")["last_scan_status"])

    @staticmethod
    def company(name="Example", token="example", **fields):
        return {"name": name, "company_id": name.lower(), "ats_type": "ashby", "ats_token": token,
                "collection_enabled": True, **fields}

    @staticmethod
    def posting(number="1"):
        return {"id": number, "title": "Engineer", "jobUrl": f"https://example.com/jobs/{number}", "descriptionPlain": "Build systems."}

    def test_additional_board_keeps_primary_and_deduplicates_repeated_primary(self):
        company = self.company(ats_sources=[{"ats_type": "ashby", "ats_token": "example"}, {"ats_type": "ashby", "ats_token": "second"}])
        source = CareerPagesSource(company_registry=_Registry([company]))
        calls = []
        def fetch(url, callback):
            calls.append(url)
            return {"jobs": [self.posting("2" if "/second?" in url else "1")]}
        with patch.object(source, "_fetch_json", side_effect=fetch):
            urls, _ = source.collect_vacancy_urls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(urls), 2)
        self.assertFalse(source.collection_errors)
        self.assertFalse(source.collection_incomplete_reasons)

    def test_group_with_only_additional_boards_can_be_enabled(self):
        registry = self.registry()
        company = registry.save({"name": "Group", "collection_enabled": True,
                                 "ats_sources": [{"ats_type": "ashby", "ats_token": "example"}]})
        self.assertEqual(company["ats_type"], "ashby")
        self.assertEqual(company["ats_token"], "example")
        source = CareerPagesSource(company_registry=registry)
        with patch.object(source, "_fetch_json", return_value={"jobs": []}) as fetch:
            source.collect_vacancy_urls()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(registry.get("group")["last_scan_status"], "completed")

    def test_corporate_homepage_edit_preserves_separate_boards(self):
        registry = self.registry()
        registry.save({"name": "Example", "career_url": "https://example.com/careers", "ats_url": "https://jobs.ashbyhq.com/example",
                       "ats_sources": [{"ats_type": "ashby", "ats_token": "second"}], "collection_enabled": True})
        updated = registry.save({"career_url": "https://example.com/new-careers"}, company_id="example")
        self.assertEqual(updated["ats_token"], "example")
        self.assertEqual(updated["ats_sources"][0]["ats_token"], "second")

    def test_unchanged_form_save_does_not_clear_source_check(self):
        registry = self.registry()
        original = registry.save({"name": "Example", "career_url": "https://jobs.ashbyhq.com/example", "collection_enabled": True})
        registry.record_scan("example", "completed", jobs_count=3)
        saved = registry.save({"notes": "A note", "source_revision": 999}, company_id="example")
        self.assertEqual(saved["source_revision"], original["source_revision"])
        self.assertEqual(saved["last_scan_status"], "completed")

    def test_revision_rejects_scan_after_source_changes_away_and_back(self):
        registry = self.registry()
        original = registry.save({"name": "Example", "career_url": "https://jobs.ashbyhq.com/example", "collection_enabled": True})
        registry.save({"collection_enabled": False}, company_id="example")
        registry.save({"collection_enabled": True}, company_id="example")
        accepted = registry.record_scan("example", "completed", jobs_count=0, expected_revision=original["source_revision"])
        self.assertFalse(accepted)
        self.assertIsNone(registry.get("example")["last_scan_status"])

    def test_status_write_failure_does_not_abort_other_companies(self):
        registry = _Registry([self.company(), self.company("Other", "other")])
        source = CareerPagesSource(company_registry=registry)
        with patch.object(registry, "record_scan", side_effect=OSError("Database unavailable")) as write, patch.object(source, "_fetch_json", return_value={"jobs": []}) as fetch:
            source.collect_vacancy_urls()
        self.assertEqual(write.call_count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(source.collection_errors), 2)

    def test_primary_html_failure_with_working_fallback_is_partial(self):
        registry = _Registry([self.company(ats_type="html", ats_token=None, ats_url="https://example.com/broken", career_url="https://example.com/careers")])
        source = CareerPagesSource(company_registry=registry)
        def fetch(url, callback):
            if url.endswith("/broken"):
                raise OSError("Primary unavailable")
            return '<p>No current openings</p>'
        with patch.object(source, "_fetch_public_text", side_effect=fetch):
            urls, _ = source.collect_vacancy_urls()
        self.assertEqual(urls, [])
        self.assertEqual(registry.scans[-1][1], "partial")
        self.assertTrue(source.collection_incomplete_reasons)

    def test_hidden_or_malformed_empty_states_do_not_claim_complete_zero(self):
        for page in ('<template>No current openings</template>', '<p style="display:none">No current openings</p>',
                     '<p>No current openings</p><script type="application/ld+json">{"@type":"JobPosting", broken}</script>',
                     '<p>No current openings</p><button>Load more</button>'):
            with self.subTest(page=page), self.assertRaises(ValueError):
                collect_html({"career_url": "https://example.com/careers"}, 10, lambda url, page=page: page)

    def test_request_budget_stops_unchecked_companies_without_zero_counts(self):
        registry = _Registry([self.company(), self.company("Other", "other"), self.company("Third", "third")])
        source = CareerPagesSource(company_registry=registry, max_requests=1)
        response = {"https://api.ashbyhq.com/posting-api/job-board/example?includeCompensation=true": {"jobs": [self.posting()]}}
        with patch("cvbankas_tracker.sources.careers.build_opener", side_effect=_opener_from(response)):
            urls, _ = source.collect_vacancy_urls()
        self.assertEqual(len(urls), 1)
        self.assertEqual([scan[1] for scan in registry.scans], ["completed", "partial", "partial"])
        self.assertIsNone(registry.scans[1][3])
        self.assertIsNone(registry.scans[2][3])
        self.assertEqual(source._budget.request_count, 1)

    def test_job_budget_retains_already_discovered_jobs_as_partial(self):
        registry = _Registry([self.company()])
        source = CareerPagesSource(company_registry=registry, max_jobs=1)
        with patch.object(source, "_fetch_json", return_value={"jobs": [self.posting("1"), self.posting("2")]}):
            urls, _ = source.collect_vacancy_urls()
        self.assertEqual(urls, ["https://example.com/jobs/1"])
        self.assertEqual(registry.scans[0][1], "partial")
        self.assertEqual(registry.scans[0][3], 1)

    def test_byte_budget_prevents_later_requests_and_preserves_unknown_count(self):
        registry = _Registry([self.company()])
        source = CareerPagesSource(company_registry=registry, max_total_bytes=10)
        response = {"https://api.ashbyhq.com/posting-api/job-board/example?includeCompensation=true": {"jobs": [self.posting()]}}
        with patch("cvbankas_tracker.sources.careers.build_opener", side_effect=_opener_from(response)):
            urls, _ = source.collect_vacancy_urls()
        self.assertEqual(urls, [])
        self.assertEqual(registry.scans[0][1], "partial")
        self.assertIsNone(registry.scans[0][3])

    def test_cancellation_is_preserved_inside_additional_boards(self):
        registry = _Registry([self.company(ats_sources=[{"ats_type": "ashby", "ats_token": "second"}])])
        source = CareerPagesSource(company_registry=registry)
        with patch.object(source, "_fetch_json", side_effect=CollectionCancelledError("Cancelled")), self.assertRaises(CollectionCancelledError):
            source.collect_vacancy_urls()
        self.assertEqual(registry.scans, [])

    def test_shared_group_board_prefers_specific_company_in_either_order(self):
        specific = self.company("Employer")
        group = self.company("Group", ats_sources=[{"ats_type": "ashby", "ats_token": "example", "vacancy_company_name": "Employer"}])
        for companies in ([group, specific], [specific, group]):
            with self.subTest(order=companies):
                source = CareerPagesSource(company_registry=_Registry(companies))
                with patch.object(source, "_fetch_json", return_value={"jobs": [self.posting()]}):
                    urls, _ = source.collect_vacancy_urls()
                self.assertEqual(len(urls), 1)
                self.assertEqual(json.loads(source.fetch_vacancy_page(urls[0]))["company_name"], "Employer")

    def test_conflicting_specific_employers_mark_both_checks_partial(self):
        registry = _Registry([self.company("One"), self.company("Two")])
        source = CareerPagesSource(company_registry=registry)
        with patch.object(source, "_fetch_json", return_value={"jobs": [self.posting()]}):
            urls, _ = source.collect_vacancy_urls()
        self.assertEqual(len(urls), 1)
        self.assertEqual(registry.scans[-2][0:2], ("one", "partial"))
        self.assertEqual(registry.scans[-1][0:2], ("two", "partial"))

    def test_additional_boards_do_not_imply_company_is_a_group(self):
        multi = self.company("Multi", ats_sources=[{"ats_type": "ashby", "ats_token": "second"}])
        single = self.company("Single")
        for companies in ([multi, single], [single, multi]):
            registry = _Registry(companies)
            source = CareerPagesSource(company_registry=registry)
            with patch.object(source, "_fetch_json", return_value={"jobs": [self.posting()]}):
                source.collect_vacancy_urls()
            latest = {scan[0]: scan[1] for scan in registry.scans}
            self.assertEqual(latest, {"multi": "partial", "single": "partial"})

    def test_replacing_eu_lever_board_uses_new_region(self):
        registry = self.registry()
        registry.save({"name": "Example", "career_url": "https://jobs.eu.lever.co/old", "api_url": "https://api.eu.lever.co/v0/postings/old",
                       "region": "eu", "fallback_url": "https://jobs.eu.lever.co/old", "collection_enabled": True})
        updated = registry.save({"ats_url": "https://jobs.lever.co/new"}, company_id="example")
        self.assertFalse(updated.get("api_url"))
        self.assertFalse(updated.get("fallback_url"))
        self.assertFalse(updated.get("region"))
        source = CareerPagesSource(company_registry=registry)
        with patch.object(source, "_fetch_json", return_value=[]) as fetch:
            source.collect_vacancy_urls()
        self.assertIn("https://api.lever.co/v0/postings/new?", fetch.call_args[0][0])

    def test_personio_language_changes_invalidate_scan_and_survive_child_normalization(self):
        registry = self.registry()
        original = registry.save({"name": "Example", "career_url": "https://example.jobs.personio.de", "language": "en", "collection_enabled": True})
        registry.record_scan("example", "completed", jobs_count=1)
        updated = registry.save({"language": "de", "ats_sources": [{"career_url": "https://second.jobs.personio.de", "locale": "fr"}]}, company_id="example")
        self.assertGreater(updated["source_revision"], original["source_revision"])
        self.assertIsNone(updated["last_scan_status"])
        self.assertEqual(updated["ats_sources"][0]["locale"], "fr")

    def test_concurrent_rename_discards_old_attribution(self):
        registry = self.registry()
        registry.save({"name": "Example", "career_url": "https://jobs.ashbyhq.com/example", "collection_enabled": True})
        source = CareerPagesSource(company_registry=registry)
        def fetch(url, callback):
            registry.save({"name": "New Name"}, company_id="example")
            return {"jobs": [self.posting()]}
        with patch.object(source, "_fetch_json", side_effect=fetch):
            urls, _ = source.collect_vacancy_urls()
        self.assertEqual(urls, [])
        self.assertFalse(source._job_cache)
        self.assertTrue(source.collection_incomplete_reasons)
        self.assertIsNone(registry.get("example")["last_scan_status"])

    def test_source_change_after_discovery_prevents_processing_cached_jobs(self):
        registry = self.registry()
        registry.save({"name": "Example", "career_url": "https://jobs.ashbyhq.com/example", "collection_enabled": True})
        source = CareerPagesSource(company_registry=registry)
        with patch.object(source, "_fetch_json", return_value={"jobs": [self.posting()]}):
            urls, _ = source.collect_vacancy_urls()
        registry.save({"collection_enabled": False}, company_id="example")
        with self.assertRaisesRegex(ValueError, "changed after discovery"):
            source.fetch_vacancy_page(urls[0])
