import unittest

from cvbankas_tracker.sources.career_workday import collect_workday


class WorkdayTests(unittest.TestCase):
    def test_pagination_and_full_details(self):
        calls = []
        def fetch(url, payload=None):
            calls.append((url, payload))
            if payload is not None:
                return {"total": 21 if payload["offset"] == 0 else 0, "jobPostings": [{"externalPath": f"/job/Vilnius/Engineer_{i}"} for i in range(payload["offset"], min(21,payload["offset"]+20))]}
            return {"jobPostingInfo": {"title": "Engineer", "jobDescription": "<p>Build systems</p>", "location": "Vilnius"}}
        jobs, pages, partial = collect_workday({"ats_url": "https://acme.wd1.myworkdayjobs.com/en-US/Careers"}, 2, fetch)
        self.assertEqual(len(jobs),21)
        self.assertEqual(len(pages),23)
        self.assertFalse(partial)
        self.assertEqual(jobs[0]["description"],"Build systems")
        self.assertEqual(calls[-2][1]["offset"],20)

    def test_cap_and_explicit_empty(self):
        def fetch(url, payload=None):
            return {"total":0,"jobPostings":[]}
        self.assertEqual(collect_workday({"career_url":"https://acme.wd1.myworkdayjobs.com/Careers"},1,fetch),([],['https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs'],False))
        with self.assertRaises(ValueError):
            collect_workday({"career_url":"https://example.com/jobs"},1,fetch)
        with self.assertRaises(ValueError):
            collect_workday({"career_url":"https://acme.wd1.myworkdayjobs.com/Careers"},1,lambda *args: {"total":2,"jobPostings":[]})
