import unittest

from cvbankas_tracker.sources.career_html import collect_html


class EYCareerTests(unittest.TestCase):
    def test_lithuania_search_collects_job_details(self) -> None:
        listing = "https://careers.ey.com/ey/search/?q=&locationsearch=Lithuania"
        first = "https://careers.ey.com/ey/job/Vilnius-Engineer-LT/1/"
        second = "https://careers.ey.com/ey/job/Kaunas-Analyst-LT/2/"
        responses = {
            listing: (
                '<div aria-label="Search results for . Page 1 of 1, Results 1 to 2 of 2">'
                '<a href="/ey/job/Vilnius-Engineer-LT/1/">Engineer</a>'
                '<a href="/ey/job/Kaunas-Analyst-LT/2/">Analyst</a></div>'
            ),
            first: '<h1>Engineer</h1><span class="jobdescription"><p>Build reliable systems and maintain production services. Collaborate with teams, improve automation, and deliver secure software for clients.</p></span>',
            second: '<h1>Analyst</h1><span class="jobdescription"><p>Analyze business requirements, document processes, work with stakeholders, improve reporting, and deliver practical solutions for clients.</p></span>',
        }
        jobs, pages, incomplete = collect_html(
            {"name": "EY", "career_url": "https://careers.ey.com/", "ats_type": "html"},
            2,
            responses.__getitem__,
        )
        self.assertFalse(incomplete)
        self.assertEqual([job["title"] for job in jobs], ["Engineer", "Analyst"])
        self.assertEqual(pages, [listing, first, second])


if __name__ == "__main__":
    unittest.main()
