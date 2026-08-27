"""Tests for observability hardening: durable AI-fallback reason (#7) and
restart-surviving per-job logs (#9)."""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.cvbankas_tracker import web_jobs
from src.cvbankas_tracker.ai_cli import AICLIError
from src.cvbankas_tracker.analysis import VacancyAnalysisService
from src.cvbankas_tracker.models import AnalysisMethod, FitLabel
from src.cvbankas_tracker.web_jobs import JobManager


class _RaisingStrategy:
    method = AnalysisMethod.AI_BASED

    def populate_builder(self, builder, _vacancy, _profile) -> None:
        raise AICLIError("provider down")


class _FallbackStrategy:
    method = AnalysisMethod.RULE_BASED

    def __init__(self, note: str = "") -> None:
        self._note = note

    def populate_builder(self, builder, _vacancy, _profile) -> None:
        builder.with_vacancy_source_url("https://example.test/1-1")
        builder.with_analysis_method(AnalysisMethod.RULE_BASED)
        builder.with_score(30)
        builder.with_fit_label(FitLabel.LOW)
        builder.with_explanation("rule-based scoring")
        if self._note:
            builder.with_notes(self._note)


class AIFallbackObservabilityTests(unittest.TestCase):
    def test_fallback_reason_is_recorded_in_notes(self) -> None:
        # FP guard against silent degradation: when the AI provider fails, the
        # reason must be durably visible, not hidden behind a rule-based score.
        service = VacancyAnalysisService(
            primary_strategy=_RaisingStrategy(),
            fallback_strategy=_FallbackStrategy(note="baseline note"),
        )
        with redirect_stdout(io.StringIO()) as out:
            analysis = service.analyze(vacancy=None, profile=None)

        self.assertIn("[AI fallback]", analysis.notes)
        self.assertIn("AICLIError: provider down", analysis.notes)
        self.assertIn("baseline note", analysis.notes)  # rule-based note preserved
        self.assertIn("[AI fallback]", out.getvalue())  # also echoed to the job log

    def test_no_fallback_still_raises(self) -> None:
        service = VacancyAnalysisService(primary_strategy=_RaisingStrategy())
        with self.assertRaises(AICLIError):
            service.analyze(vacancy=None, profile=None)


class DurableJobLogTests(unittest.TestCase):
    def _wait_done(self, jm: JobManager, job_id: int) -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            job = jm.get(job_id)
            if job is not None and job.status not in {"running", "paused"}:
                return
            time.sleep(0.02)
        self.fail("job did not finish in time")

    def test_job_log_persists_to_file_and_survives_memory_prune(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jm = JobManager(log_dir=Path(tmp) / "job_logs")

            def target(_control) -> int:
                print("hello-durable-log")
                return 0

            job = jm.start("search", target)
            self._wait_done(jm, job.id)

            self.assertTrue(job.log_path)
            content = Path(job.log_path).read_text(encoding="utf-8")
            self.assertIn("hello-durable-log", content)
            self.assertEqual(jm.snapshot(job.id)["log_path"], job.log_path)

    def test_error_is_written_to_the_durable_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jm = JobManager(log_dir=Path(tmp) / "job_logs")

            def target(_control) -> int:
                raise RuntimeError("kaboom")

            job = jm.start("search", target)
            self._wait_done(jm, job.id)

            self.assertEqual(jm.get(job.id).status, "error")
            content = Path(job.log_path).read_text(encoding="utf-8")
            self.assertIn("kaboom", content)

    def test_no_log_dir_keeps_in_memory_only_behavior(self) -> None:
        jm = JobManager()  # no log_dir -> unchanged legacy behavior
        job = jm.start("search", lambda _c: 0)
        self._wait_done(jm, job.id)
        self.assertEqual(job.log_path, "")

    def test_old_log_files_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "job_logs"
            log_dir.mkdir()
            files = []
            for i in range(3):
                path = log_dir / f"job-{i}-search.log"
                path.write_text("x", encoding="utf-8")
                os.utime(path, (1000 + i, 1000 + i))  # ascending mtime
                files.append(path)

            jm = JobManager(log_dir=log_dir)
            with patch.object(web_jobs, "MAX_LOG_FILES", 1):
                jm._prune_log_files()

            remaining = sorted(p.name for p in log_dir.glob("job-*.log"))
            self.assertEqual(remaining, ["job-2-search.log"])  # only the newest kept


if __name__ == "__main__":
    unittest.main()
