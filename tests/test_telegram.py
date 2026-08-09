from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.cvbankas_tracker.models import (
    AnalysisMethod,
    FitLabel,
    Vacancy,
    VacancyAnalysis,
)
from src.cvbankas_tracker.telegram import (
    TelegramNotifier,
    build_daily_summary,
    discover_telegram_chats,
    split_telegram_text,
)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def vacancy_row(
    *,
    title: str,
    score: int,
    url: str,
) -> tuple[Vacancy, VacancyAnalysis, None]:
    vacancy = Vacancy(
        source_name="test",
        source_id=title,
        source_url=url,
        title=title,
        company="Example & Co",
        location="Remote, EU",
        salary_text="",
    )
    analysis = VacancyAnalysis(
        vacancy_source_url=url,
        analysis_method=AnalysisMethod.RULE_BASED,
        score=score,
        fit_label=FitLabel.HIGH,
        explanation="Relevant automation role.",
        matched_points=("automation",),
        missing_points=(),
    )
    return vacancy, analysis, None


class TelegramNotificationTests(unittest.TestCase):
    def test_daily_summary_sorts_by_score_and_contains_exact_links(self) -> None:
        lower = vacancy_row(
            title="Automation Specialist",
            score=70,
            url="https://example.test/lower",
        )
        higher = vacancy_row(
            title="AI Automation Engineer",
            score=95,
            url="https://example.test/higher",
        )

        summary = build_daily_summary(
            [lower, higher],
            source_names=["test"],
            attempted_count=2,
            failed_count=0,
        )

        self.assertLess(
            summary.index("AI Automation Engineer"),
            summary.index("Automation Specialist"),
        )
        self.assertIn('href="https://example.test/higher"', summary)
        self.assertIn("Example &amp; Co", summary)

    def test_split_telegram_text_respects_message_limit(self) -> None:
        text = "\n\n".join(f"Vacancy {index}: {'x' * 80}" for index in range(20))

        chunks = split_telegram_text(text, limit=300)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_notifier_sends_each_message_chunk(self, urlopen_mock) -> None:
        urlopen_mock.return_value = FakeResponse({"ok": True, "result": {}})
        notifier = TelegramNotifier("token", "123")

        sent_count = notifier.send_text("a" * 3900)

        self.assertEqual(sent_count, 2)
        self.assertEqual(urlopen_mock.call_count, 2)

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_discovers_chat_id_from_recent_updates(self, urlopen_mock) -> None:
        urlopen_mock.return_value = FakeResponse(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {
                                "id": 12345,
                                "first_name": "Robert",
                                "type": "private",
                            }
                        },
                    }
                ],
            }
        )

        chats = discover_telegram_chats("token")

        self.assertEqual(chats[0].chat_id, "12345")
        self.assertEqual(chats[0].label, "Robert")

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_empty_summary_can_be_silently_skipped(self, urlopen_mock) -> None:
        notifier = TelegramNotifier("token", "123")

        sent_count = notifier.send_daily_summary(
            [],
            source_names=["cvbankas"],
            attempted_count=0,
            failed_count=0,
            notify_when_empty=False,
        )

        self.assertEqual(sent_count, 0)
        urlopen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
