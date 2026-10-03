import json
import unittest
from urllib.parse import parse_qs, urlparse

from cvbankas_tracker.sources.career_html_structured import collect_structured_html


class CareerHtmlStructuredTests(unittest.TestCase):
    def test_moodys_pagination_and_limit(self):
        base = "https://careers.moodys.com/en/search-jobs"
        pages = {
            base: '<ul id="search-results-jobs" data-results-count="2"><li><a href="/en/job/city/engineer/49841/1">Engineer</a></li></ul>',
            base + "?p=2": '<ul id="search-results-jobs" data-results-count="2"><li><a href="/en/job/city/designer/49841/2">Designer</a></li></ul>',
        }
        company = {"company_id": "moody-s", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 1, pages.__getitem__)
        self.assertEqual((len(jobs), len(fetched), incomplete), (1, 1, True))
        jobs, fetched, incomplete = collect_structured_html(company, 2, pages.__getitem__)
        self.assertEqual((len(jobs), len(fetched), incomplete), (2, 2, False))

    def test_valantic_category_counts(self):
        base = "https://www.valantic.com/en/careers/"
        category = base + "artificial-intelligence/"
        pages = {
            base: '<a href="' + category + '"><span data-default="1">1 job</span></a>',
            category: '<a href="' + base + 'vacancies/engineer-1/">Engineer</a>',
        }
        company = {"company_id": "valantic", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 20, pages.__getitem__)
        self.assertEqual((len(jobs), len(fetched), incomplete), (1, 2, False))

    def test_epam_full_api_pagination(self):
        pages = {
            "0": {"data": {"total": 2, "jobs": [{"unique_id": "one", "name": "Engineer", "seo": {"url": "/en/vacancy/engineer-1"}, "description": "Build software"}]}},
            "50": {"data": {"total": 2, "jobs": [{"unique_id": "two", "name": "Designer", "seo": {"url": "/en/vacancy/designer-2"}, "description": "Design software"}]}},
        }
        def fetch(url):
            offset = parse_qs(urlparse(url).query)["from"][0]
            return json.dumps(pages[offset])
        company = {"company_id": "epam", "ats_url": "https://careers.epam.com/en/jobs"}
        jobs, fetched, incomplete = collect_structured_html(company, 1, fetch)
        self.assertEqual((len(jobs), len(fetched), incomplete), (1, 1, True))
        jobs, fetched, incomplete = collect_structured_html(company, 2, fetch)
        self.assertEqual((len(jobs), len(fetched), incomplete), (2, 2, False))
        self.assertEqual(jobs[0]["url"], "https://careers.epam.com/en/vacancy/engineer-1")

    def test_wargaming_api_pagination(self):
        first = "https://wargaming.com/en/api/careers/vacancy/?limit=100"
        second = "https://wargaming.com/en/api/careers/vacancy/?limit=100&offset=1"
        pages = {
            first: json.dumps({"count": 2, "next": second, "results": [{"id": 1, "slug": "vacancy_1_vilnius", "title": "Engineer"}]}),
            second: json.dumps({"count": 2, "next": None, "results": [{"id": 2, "slug": "vacancy_2_berlin", "title": "Designer"}]}),
        }
        company = {"company_id": "wargaming", "ats_url": "https://wargaming.com/en/careers/"}
        jobs, fetched, incomplete = collect_structured_html(company, 1, pages.__getitem__)
        self.assertEqual((len(jobs), len(fetched), incomplete), (1, 1, True))
        jobs, fetched, incomplete = collect_structured_html(company, 2, pages.__getitem__)
        self.assertEqual((len(jobs), len(fetched), incomplete), (2, 2, False))
        self.assertEqual(jobs[0]["url"], "https://wargaming.com/en/careers/vacancy_1_vilnius/")

    def test_danske_oracle_pagination_and_count(self):
        pages = {
            "0": {"items": [{"TotalJobsCount": 2, "requisitionList": [
                {"Id": "101", "Title": "Engineer", "PrimaryLocation": "Vilnius", "ShortDescriptionStr": "Build software"}
            ]}]},
            "200": {"items": [{"TotalJobsCount": 2, "requisitionList": [
                {"Id": "102", "Title": "Designer", "PrimaryLocation": "Copenhagen"}
            ]}]},
        }
        def fetch(url):
            offset = "200" if "offset=200" in url else "0"
            return json.dumps(pages[offset])
        company = {
            "company_id": "danske-bank",
            "ats_url": "https://ejqi.fa.ocs.oraclecloud.eu/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
        }
        jobs, fetched, incomplete = collect_structured_html(company, 1, fetch)
        self.assertEqual((len(jobs), len(fetched), incomplete), (1, 1, True))
        jobs, fetched, incomplete = collect_structured_html(company, 2, fetch)
        self.assertEqual((len(jobs), len(fetched), incomplete), (2, 2, False))
        self.assertEqual(
            jobs[0]["url"],
            "https://ejqi.fa.ocs.oraclecloud.eu/hcmUI/CandidateExperience/en/sites/CX_1001/job/101",
        )

    def test_bartus_explicit_no_listed_roles(self):
        base = "https://bartusit.com/careers"
        page = (
            "<h1>Careers</h1><p>We do not run a careers page full of fictional roles.</p>"
            '<a href="/contact">Get in touch</a>'
        )
        company = {"company_id": "bartus-it-solutions", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 10, lambda url, page=page: page)
        self.assertEqual((jobs, fetched, incomplete), ([], [base], False))
        with_role = page + '<a href="/careers/senior-engineer">Senior Engineer</a>'
        self.assertIsNone(collect_structured_html(company, 10, lambda url: with_role))

    def test_revolut_embedded_positions_and_count(self):
        base = "https://www.revolut.com/careers/"
        positions = [
            {"id": "abc-123", "text": "Support Specialist", "locations": [{"name": "Poland - Remote"}]},
            {"id": "def-456", "text": "Software Engineer", "locations": [{"name": "London"}]},
        ]
        page = "We have 2 open positions" + '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"positions": positions}}}) + "</script>"
        company = {"company_id": "revolut", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 10, lambda url, page=page: page)
        self.assertEqual((len(jobs), fetched, incomplete), (2, [base], False))
        self.assertEqual(jobs[0]["url"], "https://www.revolut.com/careers/position/support-specialist-abc-123/")
        self.assertTrue(collect_structured_html(company, 10, lambda url: page.replace("We have 2", "We have 3"))[2])

    def test_connectpay_linkedin_positions(self):
        base = "https://connectpay.com/career/"
        page = '<h2>Open positions</h2><a href="https://www.linkedin.com/jobs/view/123"><h5>Sales Executive</h5></a>'
        company = {"company_id": "connectpay", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 10, lambda url, page=page: page)
        self.assertEqual((len(jobs), fetched, incomplete), (1, [base], False))
        self.assertEqual(jobs[0]["title"], "Sales Executive")

    def test_paystrax_linkedin_cards(self):
        base = "https://paystrax.com/career"
        card = '<h4 class="heading-6">AI Engineer</h4><span>Technology &#8226; Vilnius, Lithuania</span></div><a href="https://www.linkedin.com/jobs/view/123/">View role</a>'
        company = {"company_id": "paystrax", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 10, lambda url: card + card)
        self.assertEqual((len(jobs), fetched, incomplete), (1, [base], False))
        self.assertEqual((jobs[0]["title"], jobs[0]["location"]), ("AI Engineer", "Vilnius, Lithuania"))

    def test_teamdash_embedded_career_feed(self):
        base = "https://vilniausvandenys.teamdash.com/p/job/SHvDt8Ru/example"
        feed = {"is_landing": True, "landing": {"page_type": "career"}, "career_page_feed_contents": {"main": [
            {"url": "https://vilniausvandenys.teamdash.com/p/job/abc123/", "title": "Engineer"},
            {"url": "https://vilniausvandenys.teamdash.com/p/job/abc123/", "title": "Engineer"},
        ]}}
        page = "<script>window.context = " + json.dumps(feed) + ";</script>"
        company = {"company_id": "vilniaus-vandenys", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 10, lambda url, page=page: page)
        self.assertEqual((len(jobs), fetched, incomplete), (1, [base], False))
        self.assertEqual(jobs[0]["id"], "abc123")

    def test_cognizant_complete_xml_feed(self):
        base = "https://careers.cognizant.com/us-en/"
        feed = '<source><publisher>Cognizant</publisher><job><title>Engineer</title><requisitionid>123</requisitionid><url>https://careers.cognizant.com/us-en/jobs/123/engineer/</url><city>Vilnius</city></job></source>'
        company = {"company_id": "cognizant", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 1, lambda url: feed)
        self.assertEqual((len(jobs), fetched, incomplete), (1, [base + "jobs/xml/?rss=true"], False))
        self.assertEqual(jobs[0]["location"], "Vilnius")

    def test_application_only_career_pages(self):
        cases = [
            ("idenfy", "https://idenfy.com/careers/", "We are waiting for your application!"),
            ("strapi", "https://strapi.io/careers", "Come into the open"),
        ]
        for company_id, base, marker in cases:
            company = {"company_id": company_id, "ats_url": base}
            page = "<h1>" + marker + '</h1><a href="/about">About</a>'
            self.assertEqual(collect_structured_html(company, 10, lambda url, page=page: page), ([], [base], False))
            self.assertIsNone(collect_structured_html(company, 10, lambda url, page=page: page + '<a href="/jobs/123">Engineer</a>'))

    def test_visma_external_job_links(self):
        base = "https://www.visma.com/careers/open-positions"
        page = '<a href="https://jobs.example.com/jobs/123?promotion=x">Engineer</a><a href="https://jobs.example.com/jobs/123?promotion=x">Apply</a>'
        company = {"company_id": "visma", "ats_url": base}
        jobs, fetched, incomplete = collect_structured_html(company, 20, lambda url, page=page: page)
        self.assertEqual((len(jobs), fetched, incomplete), (1, [base], False))
        self.assertEqual(jobs[0]["url"], "https://jobs.example.com/jobs/123")


if __name__ == "__main__":
    unittest.main()
