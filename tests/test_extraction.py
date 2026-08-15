import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.extraction import VacancyAIEnrichmentService
from cvbankas_tracker.main import build_extraction_service
from cvbankas_tracker.models import Vacancy


class StubExtractionClient:
    def extract(self, vacancy: Vacancy, visible_text: str) -> dict[str, object]:
        return {
            "company": "AI Extracted Company",
            "location": "Vilnius",
            "salary_text": "2000 EUR",
            "requirements": ["Python", "SQL"],
            "responsibilities": ["Build automations", "Maintain APIs"],
            "notes": "Stub extraction",
        }


class VacancyAIEnrichmentTests(unittest.TestCase):
    def test_rule_mode_does_not_create_openai_extraction_client(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "configured-key"}):
            with patch(
                "cvbankas_tracker.main.OpenAIVacancyExtractionClient"
            ) as openai_client:
                build_extraction_service("test-model", use_openai=False)

        openai_client.assert_not_called()

    def test_enrichment_fills_missing_fields_only(self) -> None:
        vacancy = Vacancy(
            source_id="1-1",
            source_url="https://www.cvbankas.lt/job/1-1",
            title="Automation Engineer",
            company="",
            location="",
            salary_text="",
            requirements=[],
            responsibilities=[],
            raw_text="<html><body><h1>Automation Engineer</h1><p>Python SQL</p></body></html>",
        )

        enriched = VacancyAIEnrichmentService(StubExtractionClient()).enrich(vacancy)

        self.assertEqual(enriched.company, "AI Extracted Company")
        self.assertEqual(enriched.location, "Vilnius")
        self.assertEqual(enriched.salary_text, "2000 EUR")
        self.assertEqual(enriched.requirements, ["Python", "SQL"])
        self.assertEqual(enriched.responsibilities, ["Build automations", "Maintain APIs"])

    def test_enrichment_preserves_existing_parser_values(self) -> None:
        vacancy = Vacancy(
            source_id="1-2",
            source_url="https://www.cvbankas.lt/job/1-2",
            title="Python Developer",
            company="Parser Company",
            location="Kaunas",
            salary_text="2500 EUR",
            requirements=["Python"],
            responsibilities=["Build services"],
            raw_text="<html><body>Vacancy text</body></html>",
        )

        enriched = VacancyAIEnrichmentService(StubExtractionClient()).enrich(vacancy)

        self.assertEqual(enriched.company, "Parser Company")
        self.assertEqual(enriched.location, "Kaunas")
        self.assertEqual(enriched.salary_text, "2500 EUR")
        self.assertEqual(enriched.requirements, ["Python"])
        self.assertEqual(enriched.responsibilities, ["Build services"])


if __name__ == "__main__":
    unittest.main()
