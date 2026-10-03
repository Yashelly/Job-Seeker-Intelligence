import json
import unittest

from cvbankas_tracker.sources.career_html_listing import collect_listing_html


class CareerHtmlListingTests(unittest.TestCase):
    def test_deel_embedded_jobs(self):
        postings = [
            {"attributes": {"ashby_id": "first", "title": "Finance Manager", "external_link": "https://jobs.deel.com/deel/job-details/first/application", "location_name": "Spain", "is_listed": True}},
            {"attributes": {"ashby_id": "second", "title": "Engineer", "external_link": "https://jobs.deel.com/deel/job-details/second/application", "location_name": "Remote", "is_listed": True}},
        ]
        payload = json.dumps({"jobs": postings}, separators=(",", ":")).replace('"', r'\"')
        company = {"company_id": "deel", "ats_url": "https://www.deel.com/careers/"}
        jobs, pages, incomplete = collect_listing_html(company, 20, lambda url: payload)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["location"], "Spain")
        self.assertEqual((pages, incomplete), ([company["ats_url"]], False))

    def test_retool_embedded_jobs(self):
        postings = [{"id": "123", "title": "Engineer", "location": "Remote", "link": "https://jobs.gem.com/retool/123", "content": "$42"}]
        payload = json.dumps({"jobs": postings}, separators=(",", ":")).replace('"', chr(92) + '"')
        company = {"company_id": "retool", "ats_url": "https://retool.com/careers"}
        jobs, pages, incomplete = collect_listing_html(company, 20, lambda url: payload)
        self.assertEqual([job["title"] for job in jobs], ["Engineer"])
        self.assertEqual(jobs[0]["description"], "")
        self.assertEqual((pages, incomplete), ([company["ats_url"]], False))

    def test_static_career_links(self):
        cases = [
            ("mailerlite", "jobs", "https://www.mailerlite.com/jobs"),
            ("kilo", "career", "https://kilo.co/career/"),
            ("macaw", "careers", "https://www.macaw.net/careers"),
            ("nordcurrent", "careers", "https://nordcurrent.com/jobs/"),
            ("unmanned-defense-systems", "career", "https://www.udefenses.com/career"),
        ]
        for source_id, segment, base in cases:
            with self.subTest(source_id=source_id):
                company = {"company_id": source_id, "ats_url": base}
                page = f'<a href="/{segment}/software-engineer/">Software Engineer</a><a href="/{segment}/software-engineer/">apply now</a>'
                jobs, pages, incomplete = collect_listing_html(company, 20, lambda url, page=page: page)
                self.assertEqual([job["title"] for job in jobs], ["Software Engineer"])
                self.assertEqual((pages, incomplete), ([company["ats_url"]], False))

    def test_next_page_remains_incomplete(self):
        company = {"company_id": "macaw", "ats_url": "https://www.macaw.net/careers"}
        page = '<a href="/careers/engineer">Engineer</a><a href="/careers?page=2">Next</a>'
        self.assertTrue(collect_listing_html(company, 20, lambda url, page=page: page)[2])


if __name__ == "__main__":
    unittest.main()
