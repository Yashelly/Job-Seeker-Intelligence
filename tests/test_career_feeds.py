import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.sources.career_feeds import SUPPORTED_EXTRA_ATS, collect_feed


class CareerFeedsTests(unittest.TestCase):
    def test_workable_collects_public_widget_jobs_with_descriptions(self) -> None:
        seen: list[str] = []

        def fetch_json(url: str) -> object:
            seen.append(url)
            return {
                "name": "Acme",
                "jobs": [
                    {
                        "shortcode": "ABC123",
                        "title": "Automation Engineer",
                        "url": "https://apply.workable.com/acme/j/ABC123/",
                        "city": "Vilnius",
                        "country": "Lithuania",
                        "telecommuting": True,
                        "description": "<p>Build reliable job search automation.</p>",
                    }
                ],
            }

        jobs, urls, incomplete = collect_feed(
            "workable",
            {"name": "Acme", "career_url": "https://apply.workable.com/acme/"},
            1,
            fetch_json,
            lambda _url: "",
        )

        self.assertEqual(seen, ["https://apply.workable.com/api/v1/widget/accounts/acme?details=true"])
        self.assertEqual(urls, seen)
        self.assertFalse(incomplete)
        self.assertEqual(jobs[0]["id"], "workable:acme:ABC123")
        self.assertEqual(jobs[0]["title"], "Automation Engineer")
        self.assertIn("Remote", jobs[0]["location"])
        self.assertEqual(jobs[0]["description"], "Build reliable job search automation.")

    def test_smartrecruiters_fetches_each_detail_and_marks_truncated_pages(self) -> None:
        urls_seen: list[str] = []

        def fetch_json(url: str) -> object:
            urls_seen.append(url)
            if url.endswith("offset=0"):
                return {"content": [{"id": "job-1"}], "totalFound": 2}
            if url.endswith("/postings/job-1"):
                return {
                    "id": "job-1",
                    "name": "Backend Engineer",
                    "applyUrl": "https://jobs.smartrecruiters.com/acme/job-1",
                    "location": {"city": "Kaunas", "country": "Lithuania"},
                    "jobAd": {
                        "sections": {
                            "jobDescription": {"text": "<p>Own backend services.</p>"},
                            "qualifications": {"text": "<p>Python required.</p>"},
                        }
                    },
                }
            raise AssertionError(f"Unexpected URL: {url}")

        jobs, urls, incomplete = collect_feed(
            "smartrecruiters",
            {"name": "Acme", "ats_token": "acme"},
            1,
            fetch_json,
            lambda _url: "",
        )

        self.assertTrue(incomplete)
        self.assertEqual(urls, urls_seen)
        self.assertEqual(jobs[0]["id"], "smartrecruiters:acme:job-1")
        self.assertEqual(jobs[0]["description"], "Own backend services., Python required.")
        self.assertEqual(jobs[0]["requirements"], ["Python required."])

    def test_personio_parses_public_xml_feed(self) -> None:
        xml = """
        <workzag-jobs>
          <position>
            <id>42</id>
            <name>Data Engineer</name>
            <jobUrl>https://demo.jobs.personio.de/job/42</jobUrl>
            <office>Vilnius</office>
            <jobDescriptions>
              <jobDescription>
                <name>Your mission</name>
                <value><![CDATA[<p>Build analytics pipelines.</p>]]></value>
              </jobDescription>
            </jobDescriptions>
          </position>
        </workzag-jobs>
        """
        fetched: list[str] = []

        def fetch_html(url: str) -> str:
            fetched.append(url)
            return xml

        jobs, urls, incomplete = collect_feed(
            "personio",
            {"name": "Demo", "ats_url": "https://demo.jobs.personio.de/"},
            1,
            lambda _url: {},
            fetch_html,
        )

        self.assertEqual(fetched, ["https://demo.jobs.personio.de/xml?language=en"])
        self.assertEqual(urls, fetched)
        self.assertFalse(incomplete)
        self.assertEqual(jobs[0]["title"], "Data Engineer")
        self.assertEqual(jobs[0]["description"], "Your mission Build analytics pipelines.")

    def test_recruitee_collects_public_offers(self) -> None:
        def fetch_json(url: str) -> object:
            self.assertEqual(url, "https://acme.recruitee.com/api/offers")
            return {
                "offers": [
                    {
                        "id": 7,
                        "slug": "platform-engineer",
                        "title": "Platform Engineer",
                        "careers_url": "https://acme.recruitee.com/o/platform-engineer",
                        "city": "Remote",
                        "description": "<p>Keep the platform healthy.</p>",
                        "requirements": "<p>Observability experience.</p>",
                    }
                ]
            }

        jobs, _urls, incomplete = collect_feed(
            "recruitee",
            {"name": "Acme", "ats_token": "acme"},
            1,
            fetch_json,
            lambda _url: "",
        )

        self.assertFalse(incomplete)
        self.assertEqual(jobs[0]["id"], "recruitee:acme.recruitee.com:7")
        self.assertEqual(jobs[0]["requirements"], ["Observability experience."])

    def test_malformed_payloads_are_explicit_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Workable response did not contain a jobs list"):
            collect_feed("workable", {"ats_token": "acme"}, 1, lambda _url: {"jobs": {}}, lambda _url: "")
        with self.assertRaisesRegex(ValueError, "missing a full description"):
            collect_feed(
                "workable",
                {"ats_token": "acme"},
                1,
                lambda _url: {
                    "jobs": [
                        {
                            "shortcode": "ABC",
                            "title": "No Description",
                            "url": "https://apply.workable.com/acme/j/ABC/",
                        }
                    ]
                },
                lambda _url: "",
            )

    def test_teamtailor_collects_public_rss_jobs_from_career_origin(self) -> None:
        rss = """
        <rss version="2.0">
          <channel>
            <item>
              <title>Product Engineer</title>
              <link>https://careers.acme.test/jobs/123-product-engineer</link>
              <description><![CDATA[
                Build product automation, improve reliable workflows, own backend
                services, collaborate with users, and maintain practical AI tooling
                across the job-search pipeline every week.
              ]]></description>
              <category>Vilnius</category>
            </item>
          </channel>
        </rss>
        """
        fetched: list[str] = []

        def fetch_html(url: str) -> str:
            fetched.append(url)
            return rss

        jobs, urls, incomplete = collect_feed(
            "teamtailor",
            {"name": "Acme", "career_url": "https://careers.acme.test/departments/engineering"},
            1,
            lambda _url: {},
            fetch_html,
        )

        self.assertEqual(fetched, ["https://careers.acme.test/jobs.rss"])
        self.assertEqual(urls, fetched)
        self.assertFalse(incomplete)
        self.assertEqual(jobs[0]["id"], "teamtailor:https://careers.acme.test/jobs/123-product-engineer")
        self.assertEqual(jobs[0]["location"], "Vilnius")

    def test_teamtailor_fetches_detail_when_rss_description_is_short(self) -> None:
        rss = """
        <rss version="2.0">
          <channel>
            <item>
              <title>Data Engineer</title>
              <link>https://careers.acme.test/jobs/456-data-engineer?source=rss</link>
              <description>Short teaser.</description>
            </item>
          </channel>
        </rss>
        """
        detail = """
        <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Data Engineer",
          "url": "https://careers.acme.test/jobs/456-data-engineer?source=rss",
          "description": "<p>Build robust analytics pipelines and automation for daily job discovery.</p>"
        }
        </script>
        """
        responses = {
            "https://careers.acme.test/jobs.rss": rss,
            "https://careers.acme.test/jobs/456-data-engineer?source=rss": detail,
        }

        jobs, pages, incomplete = collect_feed(
            "teamtailor",
            {"name": "Acme", "career_url": "https://careers.acme.test"},
            2,
            lambda _url: {},
            responses.__getitem__,
        )

        self.assertFalse(incomplete)
        self.assertEqual(
            pages,
            [
                "https://careers.acme.test/jobs.rss",
                "https://careers.acme.test/jobs/456-data-engineer?source=rss",
            ],
        )
        self.assertEqual(jobs[0]["description"], "Build robust analytics pipelines and automation for daily job discovery.")
        self.assertEqual(jobs[0]["id"], "teamtailor:https://careers.acme.test/jobs/456-data-engineer?source=rss")

    def test_empty_xml_feeds_are_valid_zero_jobs(self) -> None:
        teamtailor_jobs, teamtailor_pages, teamtailor_incomplete = collect_feed(
            "teamtailor",
            {"name": "Acme", "career_url": "https://careers.acme.test"},
            1,
            lambda _url: {},
            lambda _url: "<rss><channel><title>Jobs</title></channel></rss>",
        )
        self.assertEqual(teamtailor_jobs, [])
        self.assertEqual(teamtailor_pages, ["https://careers.acme.test/jobs.rss"])
        self.assertFalse(teamtailor_incomplete)

        personio_jobs, personio_pages, personio_incomplete = collect_feed(
            "personio",
            {"name": "Demo", "ats_url": "https://demo.jobs.personio.com/"},
            1,
            lambda _url: {},
            lambda _url: "<workzag-jobs/>",
        )
        self.assertEqual(personio_jobs, [])
        self.assertEqual(personio_pages, ["https://demo.jobs.personio.com/xml?language=en"])
        self.assertFalse(personio_incomplete)

    def test_xml_feeds_reject_non_feed_html_and_entities(self) -> None:
        with self.assertRaisesRegex(ValueError, "was not an RSS feed"):
            collect_feed(
                "teamtailor",
                {"career_url": "https://careers.acme.test"},
                1,
                lambda _url: {},
                lambda _url: "<html>No jobs</html>",
            )
        with self.assertRaisesRegex(ValueError, "cannot contain DTD or entity"):
            collect_feed(
                "personio",
                {"ats_url": "https://demo.jobs.personio.de/"},
                1,
                lambda _url: {},
                lambda _url: '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><workzag-jobs/>',
            )

    def test_supported_extra_ats_contract_includes_public_teamtailor_rss(self) -> None:
        self.assertEqual(
            SUPPORTED_EXTRA_ATS,
            {"personio", "recruitee", "smartrecruiters", "teamtailor", "workable", "paylocity"},
        )


if __name__ == "__main__":
    unittest.main()
