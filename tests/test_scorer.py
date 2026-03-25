import unittest

from src.profile_loader import load_profile
from src.scorer import score_job


class ScorerTests(unittest.TestCase):
    def test_reasonable_score_for_automation_role(self):
        profile = load_profile("profile/user_profile.yaml")
        job = {
            "domain": "ai_process_automation",
            "required_skills": ["python", "make", "api", "documentation"],
            "preferred_skills": ["sql"],
            "tools_and_platforms": ["zapier"],
            "responsibilities": ["Analyze process automation opportunities and document results"],
            "experience_requirements": "minimum 1 year of experience",
            "seniority_hints": [],
            "work_mode": "hybrid",
            "location": "Vilnius",
            "red_flags": [],
        }
        result = score_job(job, profile)
        self.assertIn(result["decision"], {"apply", "stretch"})
        self.assertGreaterEqual(result["score"], 55)


if __name__ == "__main__":
    unittest.main()
