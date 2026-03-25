import unittest

from src.extraction_service import extract_job_with_strategy
from src.openai_extractor import extract_job_structured_openai
from src.openai_models import JobExtractionModel, SalaryModel


class _FakeResponse:
    def __init__(self, parsed):
        self.output_parsed = parsed


class _FakeResponses:
    def parse(self, **kwargs):
        parsed = JobExtractionModel(
            company="OpenAI Test Company",
            role_title="AI Automation Specialist",
            location="Vilnius",
            work_mode="hybrid",
            salary=SalaryModel(min=2000, max=2800, currency="EUR", gross_or_net="gross"),
            employment_type="full-time",
            responsibilities=["Automate workflows", "Document process improvements"],
            required_skills=["python", "api", "make"],
            preferred_skills=["sql"],
            tools_and_platforms=["zapier", "make"],
            experience_requirements="minimum 1 year of experience",
            language_requirements=["english", "lithuanian"],
            education_requirements="",
            domain="ai_process_automation",
            seniority_hints=[],
            red_flags=[],
            notes="",
            raw_text_excerpt="AI Automation Specialist vacancy excerpt",
        )
        return _FakeResponse(parsed)


class _FakeClient:
    def __init__(self):
        self.responses = _FakeResponses()


class OpenAIExtractorTests(unittest.TestCase):
    def test_openai_extractor_returns_dict(self):
        payload = extract_job_structured_openai(
            "Some vacancy text",
            model="gpt-4o-mini",
            client=_FakeClient(),
        )
        self.assertEqual(payload["company"], "OpenAI Test Company")
        self.assertEqual(payload["role_title"], "AI Automation Specialist")
        self.assertEqual(payload["salary"]["min"], 2000)

    def test_auto_strategy_falls_back_when_openai_unavailable(self):
        payload = extract_job_with_strategy(
            "Company: Example\nAI Automation Specialist\nRequirements:\n- Python\nResponsibilities:\n- Automate workflows",
            strategy="auto",
            openai_model="gpt-4o-mini",
        )
        self.assertTrue(payload["role_title"])
        self.assertIn("python", [x.lower() for x in payload["required_skills"]])


if __name__ == "__main__":
    unittest.main()
