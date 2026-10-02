import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BlindEvaluationScriptTests(unittest.TestCase):
    def test_evaluates_matching_private_labels_without_case_details(self) -> None:
        cases = {
            "schema_version": 1,
            "test_set_id": "test-holdout",
            "profile": {
                "name": "Candidate",
                "target_roles": ["Python Developer"],
                "skills": ["Python"],
                "preferred_locations": ["Remote"],
                "experience_level": "mid",
                "years_of_experience": 2,
                "additional_keywords": [],
                "must_have_skills": [],
                "nice_to_have_skills": [],
                "excluded_keywords": [],
                "max_english_level": None,
                "work_modes": [{"mode": "remote", "country": ""}],
            },
            "cases": {
                "deduplication": [
                    {"id": "dedup-1", "url": "https://example.test/job/1?utm_source=x"},
                    {"id": "dedup-2", "url": "https://example.test/job/1"},
                ],
                "relevance": [
                    {"id": "rel-1", "title": "Python Developer", "text": "Python remote"}
                ],
                "work_mode": [{"id": "mode-1", "text": "Fully remote"}],
                "eligibility": [{"id": "elig-1", "text": "Fully remote"}],
                "seniority": [{"id": "sen-1", "text": "Junior Python Developer"}],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            cases_path = directory_path / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            cases_hash = hashlib.sha256(cases_path.read_bytes()).hexdigest()
            labels = {
                "schema_version": 1,
                "cases_sha256": cases_hash,
                "labels": {
                    "deduplication": [
                        {"id": "dedup-1", "expected_group": "same"},
                        {"id": "dedup-2", "expected_group": "same"},
                    ],
                    "relevance": [{"id": "rel-1", "expected_relevant": True}],
                    "work_mode": [{"id": "mode-1", "expected": "remote"}],
                    "eligibility": [{"id": "elig-1", "expected": True}],
                    "seniority": [{"id": "sen-1", "expected": "junior"}],
                },
            }
            labels_path = directory_path / "labels.json"
            output_path = directory_path / "results.json"
            labels_path.write_text(json.dumps(labels), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/evaluate_blind_set.py",
                    "--cases",
                    str(cases_path),
                    "--labels",
                    str(labels_path),
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertFalse(result["per_case_details_included"])
            self.assertNotIn("errors", result["results"]["relevance"])
            self.assertEqual(result["results"]["work_mode"]["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
