import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.sources.base import CollectionCancelledError
from cvbankas_tracker.sources.career_html import collect_html


def jobposting(title: str, url: str, description: str = "Build useful software for internal teams.") -> str:
    return json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": title,
            "url": url,
            "description": f"<p>{description}</p>",
            "hiringOrganization": {"name": "Acme"},
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Vilnius",
                    "addressCountry": "LT",
                },
            },
        }
    )


class CareerHtmlTests(unittest.TestCase):
    def test_collects_all_json_ld_jobpostings_from_arrays_and_graphs(self) -> None:
        page = f"""
        <script type="application/ld+json">
        [
          {jobposting("Backend Engineer", "https://example.test/jobs?id=be")},
          {{"@graph": [
            {{"@type": "Organization", "name": "Acme"}},
            {jobposting("Data Analyst", "https://example.test/jobs?id=da")}
          ]}}
        ]
        </script>
        """

        jobs, pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            1,
            lambda url: page,
        )

        self.assertEqual(pages, ["https://example.test/careers"])
        self.assertFalse(incomplete)
        self.assertEqual([job["title"] for job in jobs], ["Backend Engineer", "Data Analyst"])
        self.assertEqual(jobs[0]["url"], "https://example.test/jobs?id=be")
        self.assertEqual(jobs[0]["location"], "Vilnius, LT")

    def test_fetches_relevant_detail_links_and_preserves_query_ids(self) -> None:
        listing = """
        <a href="/jobs?jobId=123" class="job-card">Senior Python Engineer</a>
        <a href="/about">About us</a>
        """
        detail = """
        <html><head><title>Senior Python Engineer</title></head>
        <body>
          <h1>Senior Python Engineer</h1>
          <div class="job-location">Remote</div>
          <section class="job-description">
            Apply now. You will build automation for job discovery, maintain data
            ingestion, improve reliability, and collaborate with product users daily.
            The role owns production workflows and practical AI-assisted tooling.
          </section>
          <a>Apply for this job</a>
        </body></html>
        """
        responses = {
            "https://example.test/careers": listing,
            "https://example.test/jobs?jobId=123": detail,
        }

        jobs, pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            3,
            responses.__getitem__,
        )

        self.assertFalse(incomplete)
        self.assertEqual(pages, ["https://example.test/careers", "https://example.test/jobs?jobId=123"])
        self.assertEqual(jobs[0]["id"], "html:https://example.test/jobs?jobId=123")
        self.assertEqual(jobs[0]["url"], "https://example.test/jobs?jobId=123")
        self.assertEqual(jobs[0]["location"], "Remote")

    def test_follows_next_job_list_and_marks_budget_partial(self) -> None:
        responses = {
            "https://example.test/careers": """
                <script type="application/ld+json">
                {"@type":"JobPosting","title":"One","url":"https://example.test/jobs/1","description":"One long enough description"}
                </script>
                <a href="/careers?page=2" rel="next">Next</a>
            """,
            "https://example.test/careers?page=2": """
                <script type="application/ld+json">
                {"@type":"JobPosting","title":"Two","url":"https://example.test/jobs/2","description":"Two long enough description"}
                </script>
            """,
        }

        jobs, pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            1,
            responses.__getitem__,
        )

        self.assertTrue(incomplete)
        self.assertEqual(pages, ["https://example.test/careers"])
        self.assertEqual([job["title"] for job in jobs], ["One"])

    def test_no_openings_text_is_explicit_zero_jobs(self) -> None:
        jobs, pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            1,
            lambda _url: "<main>No current openings. Please check back later.</main>",
        )

        self.assertEqual(jobs, [])
        self.assertEqual(pages, ["https://example.test/careers"])
        self.assertFalse(incomplete)

    def test_unparsed_javascript_shell_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not expose parseable public job postings"):
            collect_html(
                {"name": "Acme", "career_url": "https://example.test/careers"},
                1,
                lambda _url: '<div id="app"></div><script src="/assets/jobs.js"></script>',
            )

    def test_detail_failure_after_a_valid_job_marks_partial(self) -> None:
        listing = f"""
        <script type="application/ld+json">
        {jobposting("Backend Engineer", "https://example.test/jobs?id=be")}
        </script>
        <a href="/jobs?jobId=broken" class="job-card">Data Engineer</a>
        """

        def fetch_html(url: str) -> str:
            if url == "https://example.test/careers":
                return listing
            raise ValueError("blocked")

        jobs, pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            3,
            fetch_html,
        )

        self.assertEqual([job["title"] for job in jobs], ["Backend Engineer"])
        self.assertEqual(pages, ["https://example.test/careers"])
        self.assertTrue(incomplete)

    def test_navigation_links_are_not_counted_as_jobs(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not expose parseable public job postings"):
            collect_html(
                {"name": "Acme", "career_url": "https://example.test/careers"},
                3,
                lambda _url: '<a href="/careers">Careers</a><a href="/team">Team</a>',
            )

    def test_start_url_can_be_a_strong_detail_page_without_json_ld(self) -> None:
        detail = """
        <html><head><title>Product Engineer</title></head>
        <body>
          <h1>Product Engineer</h1>
          <section class="job-description">
            Apply now. You will build product workflows, improve internal tools,
            own data ingestion reliability, collaborate with users, maintain
            practical automation, and ship robust backend services for hiring.
          </section>
          <a href="/apply">Apply for this job</a>
        </body></html>
        """

        jobs, pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/jobs/product-engineer"},
            1,
            lambda _url: detail,
        )

        self.assertFalse(incomplete)
        self.assertEqual(pages, ["https://example.test/jobs/product-engineer"])
        self.assertEqual(jobs[0]["title"], "Product Engineer")
        self.assertEqual(jobs[0]["url"], "https://example.test/jobs/product-engineer")

    def test_detail_page_incomplete_markers_do_not_force_partial(self) -> None:
        detail = """
        <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Backend Engineer",
          "description": "<p>Build useful software for internal teams.</p>"
        }
        </script>
        <button data-page="2">View more stories</button>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/jobs/backend"},
            1,
            lambda _url: detail,
        )

        self.assertEqual(jobs[0]["url"], "https://example.test/jobs/backend")
        self.assertFalse(incomplete)

    def test_single_json_ld_detail_url_does_not_force_partial_from_site_markers(self) -> None:
        detail = f"""
        <script type="application/ld+json">
        {jobposting("Backend Engineer", "https://example.test/jobs/backend")}
        </script>
        <button data-page="2">View more stories</button>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/jobs/backend"},
            1,
            lambda _url: detail,
        )

        self.assertEqual(jobs[0]["title"], "Backend Engineer")
        self.assertFalse(incomplete)

    def test_strong_detail_page_accepts_apply_on_cta(self) -> None:
        detail = """
        <html><body>
          <h1>Software Engineer</h1>
          <main>
            <p>Apply on Gem</p>
            <p>Nearly every team needs reliable internal software. You will build
            product infrastructure, own backend services, collaborate with design,
            improve developer workflows, and ship useful automation for customers.</p>
          </main>
        </body></html>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers/software-engineer"},
            1,
            lambda _url: detail,
        )

        self.assertEqual(jobs[0]["title"], "Software Engineer")
        self.assertFalse(incomplete)

    def test_bad_and_asset_links_are_skipped_without_failing_page(self) -> None:
        detail = """
        <a href="/_next/image?url=https%3A%2F%2Fcdn.test%2Fcareers.jpg">careers image</a>
        <a href="/'+$.translateLookup($.getLocale(), 'tc_url')+'">bad job link</a>
        <main>No current openings. Please check back later.</main>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            5,
            lambda _url: detail,
        )

        self.assertEqual(jobs, [])
        self.assertFalse(incomplete)

    def test_embedded_external_job_urls_are_collected_from_json_payloads(self) -> None:
        listing = """
        <script id="payload" type="application/json">
        {"jobs":["https:\\/\\/jobs.gem.com\\/retool\\/12345","https:\\/\\/jobs.deel.com\\/job-details\\/67890"]}
        </script>
        """
        gem_detail = """
        <h1>Software Engineer</h1>
        <main>Apply on Gem. Build internal software, automate workflows, maintain
        backend services, collaborate with product teams, and improve customer
        operations with reliable developer tools.</main>
        """
        deel_detail = """
        <h1>Support Specialist</h1>
        <main>Apply now. Help customers resolve technical issues, document support
        playbooks, improve internal processes, and work with engineering on
        product feedback loops.</main>
        """
        responses = {
            "https://example.test/careers": listing,
            "https://jobs.gem.com/retool/12345": gem_detail,
            "https://jobs.deel.com/job-details/67890": deel_detail,
        }

        jobs, pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            5,
            responses.__getitem__,
        )

        self.assertEqual([job["title"] for job in jobs], ["Software Engineer", "Support Specialist"])
        self.assertIn("https://jobs.gem.com/retool/12345", pages)
        self.assertFalse(incomplete)

    def test_section_detail_page_extracts_description(self) -> None:
        detail = """
        <h1>Customer Support Specialist</h1>
        <section>
          <p>Apply now. You will help customers succeed, answer technical product
          questions, document recurring issues, collaborate with engineering, and
          improve support workflows for distributed teams.</p>
        </section>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/jobs/customer-support-specialist"},
            1,
            lambda _url: detail,
        )

        self.assertEqual(jobs[0]["title"], "Customer Support Specialist")
        self.assertIn("help customers succeed", jobs[0]["description"])
        self.assertFalse(incomplete)

    def test_heading_window_detail_page_extracts_description(self) -> None:
        detail = """
        <h1>Senior Product Design Engineer</h1>
        <div><h2>About the Role</h2>
        <p>We are hiring a product design engineer to own core product surfaces,
        build production grade components, collaborate with engineering, test
        accessibility paths, and improve scheduling workflows end to end.</p>
        <h2>What You'll Do</h2>
        <ul><li>Own product surfaces from concept to production.</li></ul>
        <a>Apply now</a></div>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/jobs/senior-product-designer"},
            1,
            lambda _url: detail,
        )

        self.assertEqual(jobs[0]["title"], "Senior Product Design Engineer")
        self.assertIn("About the Role", jobs[0]["description"])
        self.assertFalse(incomplete)

    def test_head_role_title_is_accepted_as_job_detail(self) -> None:
        detail = """
        <h1>Head of Growth</h1>
        <main>
          <p>Apply now. Lead growth strategy, own acquisition channels, improve
          activation loops, partner with product teams, analyze funnel quality,
          and build repeatable programs for developer adoption.</p>
        </main>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/jobs/head-of-growth"},
            1,
            lambda _url: detail,
        )

        self.assertEqual(jobs[0]["title"], "Head of Growth")
        self.assertFalse(incomplete)

    def test_detail_page_with_multiple_json_ld_jobs_is_not_accepted_as_one_job(self) -> None:
        listing_detail = f"""
        <script type="application/ld+json">
        [
          {jobposting("Backend Engineer", "https://example.test/jobs/backend")},
          {jobposting("Data Engineer", "https://example.test/jobs/data")}
        ]
        </script>
        """
        responses = {
            "https://example.test/careers": '<a href="/jobs/listing" class="job-card">Backend Engineer</a>',
            "https://example.test/jobs/listing": listing_detail,
        }

        with self.assertRaisesRegex(ValueError, "did not expose parseable public job postings"):
            collect_html(
                {"name": "Acme", "career_url": "https://example.test/careers"},
                3,
                responses.__getitem__,
            )

    def test_detail_oserror_after_valid_job_marks_partial(self) -> None:
        listing = f"""
        <script type="application/ld+json">
        {jobposting("Backend Engineer", "https://example.test/jobs/backend")}
        </script>
        <a href="/jobs?jobId=broken" class="job-card">Data Engineer</a>
        """

        def fetch_html(url: str) -> str:
            if url == "https://example.test/careers":
                return listing
            raise OSError("network down")

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            3,
            fetch_html,
        )

        self.assertEqual([job["title"] for job in jobs], ["Backend Engineer"])
        self.assertTrue(incomplete)

    def test_detail_cancellation_is_rethrown(self) -> None:
        listing = '<a href="/jobs?jobId=cancel" class="job-card">Data Engineer</a>'

        def fetch_html(url: str) -> str:
            if url == "https://example.test/careers":
                return listing
            raise CollectionCancelledError("stop")

        with self.assertRaises(CollectionCancelledError):
            collect_html(
                {"name": "Acme", "career_url": "https://example.test/careers"},
                3,
                fetch_html,
            )

    def test_malformed_jobposting_and_visible_load_more_mark_result_partial(self) -> None:
        listing = f"""
        <script type="application/ld+json">
        [
          {jobposting("Backend Engineer", "https://example.test/jobs/backend")},
          {{"@type":"JobPosting","title":"Broken"}}
        ]
        </script>
        <button>Load more jobs</button>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            1,
            lambda _url: listing,
        )

        self.assertEqual([job["title"] for job in jobs], ["Backend Engineer"])
        self.assertTrue(incomplete)

    def test_json_ld_parser_tries_raw_json_before_html_unescape(self) -> None:
        listing = """
        <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Backend Engineer",
          "url": "https://example.test/jobs/backend",
          "description": "Build &quot;boring&quot; reliable automation for internal teams."
        }
        </script>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            1,
            lambda _url: listing,
        )

        self.assertFalse(incomplete)
        self.assertEqual(jobs[0]["description"], 'Build "boring" reliable automation for internal teams.')

    def test_jobposting_without_url_is_rejected_as_partial_instead_of_collapsed_to_listing_url(self) -> None:
        listing = f"""
        <script type="application/ld+json">
        [
          {jobposting("Backend Engineer", "https://example.test/jobs/backend")},
          {{
            "@type": "JobPosting",
            "title": "Data Engineer",
            "description": "Build useful software for internal teams."
          }}
        ]
        </script>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            1,
            lambda _url: listing,
        )

        self.assertEqual([job["url"] for job in jobs], ["https://example.test/jobs/backend"])
        self.assertTrue(incomplete)

    def test_visible_pagination_control_without_rel_next_marks_partial(self) -> None:
        listing = f"""
        <script type="application/ld+json">
        {jobposting("Backend Engineer", "https://example.test/jobs/backend")}
        </script>
        <button class="jobs-pagination" data-page="2">View more jobs</button>
        """

        jobs, _pages, incomplete = collect_html(
            {"name": "Acme", "career_url": "https://example.test/careers"},
            1,
            lambda _url: listing,
        )

        self.assertEqual([job["title"] for job in jobs], ["Backend Engineer"])
        self.assertTrue(incomplete)


if __name__ == "__main__":
    unittest.main()
