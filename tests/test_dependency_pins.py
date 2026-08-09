from pathlib import Path
import unittest


class DependencyPinTests(unittest.TestCase):
    def test_project_dependencies_are_exactly_pinned_without_direct_starlette(self) -> None:
        text = Path("pyproject.toml").read_text(encoding="utf-8")
        for requirement in (
            '"openai==2.29.0"',
            '"playwright==1.54.0"',
            '"PyYAML==6.0.2"',
            '"rich==13.9.4"',
            '"fastapi==0.116.1"',
            '"Jinja2==3.1.6"',
            '"uvicorn==0.52.1"',
            '"httpx==0.28.1"',
        ):
            self.assertIn(requirement, text)
        self.assertNotIn('"starlette', text.lower())

    def test_clean_dependency_verifier_is_wired_into_ci(self) -> None:
        workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
        script = Path("scripts/verify_clean_deps.py").read_text(encoding="utf-8")
        self.assertIn("python scripts/verify_clean_deps.py --install", workflow)
        self.assertIn("venv.EnvBuilder", script)
        self.assertIn("pip", script)
        self.assertIn("check", script)


if __name__ == "__main__":
    unittest.main()
