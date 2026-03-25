import unittest

from src.validator import validate_job_payload


class ValidatorTests(unittest.TestCase):
    def test_valid_payload(self):
        ok, errors = validate_job_payload({
            "company": "Example",
            "role_title": "Automation Specialist",
            "work_mode": "hybrid",
            "responsibilities": ["Analyze workflows"],
            "required_skills": [],
            "tools_and_platforms": [],
            "salary": {"min": 1000, "max": 2000},
        })
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_invalid_payload(self):
        ok, errors = validate_job_payload({
            "company": "",
            "role_title": "",
            "work_mode": "weird",
            "responsibilities": [],
            "required_skills": [],
            "tools_and_platforms": [],
            "salary": {"min": 3000, "max": 1000},
        })
        self.assertFalse(ok)
        self.assertGreaterEqual(len(errors), 3)


if __name__ == "__main__":
    unittest.main()
