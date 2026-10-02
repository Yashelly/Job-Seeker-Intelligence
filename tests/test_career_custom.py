from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.sources.career_custom import collect_custom


class CareerCustomTests(unittest.TestCase):
    def test_elastic_collects_details_and_marks_listing_truncation(self) -> None:
        seen_json: list[tuple[str, dict, dict]] = []
        seen_html: list[str] = []

        def fetch_html(url: str) -> str:
            seen_html.append(url)
            if url == "https://jobs.elastic.co/":
                return "<html>board</html>"
            return '''<div id="app" data-page="{&quot;props&quot;:{&quot;job_object&quot;:{&quot;id&quot;:42,&quot;title&quot;:&quot;Platform Engineer&quot;,&quot;location&quot;:&quot;Vilnius&quot;,&quot;content&quot;:&quot;&lt;p&gt;Build reliable automation systems for daily vacancy collection, search quality, and candidate workflows across distributed teams.&lt;/p&gt;&quot;}}}"></div>'''

        def fetch_json(url: str, payload=None, headers=None) -> object:
            seen_json.append((url, payload, headers))
            return {"meta": {"page": {"total_results": 101}}, "results": [{
                "id": {"raw": "42"}, "title": {"raw": "Platform Engineer"},
                "location": {"raw": "Vilnius"}, "url": {"raw": "engineering/lithuania/platform-engineer/42"},
            }]}

        jobs, pages, incomplete = collect_custom(
            "elastic_custom", {"name": "Elastic", "ats_token": "1509"}, 1,
            fetch_json, fetch_html, lambda name, domain: "token" if (name, domain) == ("XSRF-TOKEN", "jobs.elastic.co") else None,
        )

        self.assertTrue(incomplete)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "elastic:42")
        self.assertIn("Build reliable automation", jobs[0]["description"])
        self.assertEqual(seen_json[0][0], "https://jobs.elastic.co/api/appSearch")
        self.assertEqual(seen_json[0][1]["page"], {"size": 100, "current": 1})
        self.assertEqual(seen_json[0][2]["X-XSRF-TOKEN"], "token")
        self.assertIn("https://jobs.elastic.co/jobs/engineering/lithuania/platform-engineer/42", pages)

    def test_elastic_rejects_missing_cookie_and_malformed_listing(self) -> None:
        with self.assertRaisesRegex(ValueError, "XSRF-TOKEN"):
            collect_custom("elastic_custom", {"ats_token": "1509"}, 1, lambda *_args, **_kwargs: {}, lambda _url: "", lambda *_args: None)
        with self.assertRaisesRegex(ValueError, "results and a total"):
            collect_custom("elastic_custom", {"ats_token": "1509"}, 1, lambda *_args, **_kwargs: {"results": []}, lambda _url: "", lambda *_args: "ok")

    def test_doist_discovers_signed_island_and_collects_job_details(self) -> None:
        career = "https://todoist.com/careers"
        island = "https://todoist.com/_server-islands/OpenRoles?e=encrypted&amp;p=props&amp;s=signature"
        detail = "https://todoist.com/careers/abc123-general-counsel"
        responses = {
            career: f'<script src="{island}"></script>',
            "https://todoist.com/_server-islands/OpenRoles?e=encrypted&p=props&s=signature": f'<a href="{detail}">General Counsel</a>',
            detail: "<main><h1>General Counsel</h1><p>Lead legal strategy for a distributed software company, advise teams on commercial matters, build practical governance, and support customers across international markets.</p></main>",
        }

        jobs, pages, incomplete = collect_custom("astro_server_island", {"name": "Doist", "career_url": career}, 1, lambda *_args, **_kwargs: {}, responses.__getitem__, lambda *_args: None)

        self.assertFalse(incomplete)
        self.assertEqual(jobs[0]["id"], "doist:abc123-general-counsel")
        self.assertEqual(jobs[0]["title"], "General Counsel")
        self.assertIn("distributed software", jobs[0]["description"])
        self.assertEqual(pages, [career, "https://todoist.com/_server-islands/OpenRoles?e=encrypted&p=props&s=signature", detail])

    def test_doist_reports_empty_fragment_and_rejects_missing_signed_url(self) -> None:
        career = "https://todoist.com/careers"
        island = "https://todoist.com/_server-islands/OpenRoles?e=x&amp;p=y&amp;s=z"
        jobs, pages, incomplete = collect_custom(
            "astro_server_island", {"career_url": career}, 1, lambda *_args, **_kwargs: {},
            lambda url: f'<script src="{island}"></script>' if url == career else "<p>No open roles right now.</p>", lambda *_args: None,
        )
        self.assertEqual(jobs, [])
        self.assertFalse(incomplete)
        self.assertEqual(len(pages), 2)
        with self.assertRaisesRegex(ValueError, "OpenRoles"):
            collect_custom("astro_server_island", {"career_url": career}, 1, lambda *_args, **_kwargs: {}, lambda _url: "<html />", lambda *_args: None)


if __name__ == "__main__":
    unittest.main()
