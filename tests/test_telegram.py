from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from src.cvbankas_tracker.models import (
    AnalysisMethod,
    FitLabel,
    Vacancy,
    VacancyAnalysis,
)
from src.cvbankas_tracker.telegram import (
    TelegramNotificationError,
    TelegramNotifier,
    build_daily_summary,
    discover_telegram_chats,
    split_telegram_text,
)


def _http_error(code: int, *, retry_after: int | None = None) -> HTTPError:
    hdrs: dict[str, str] = {}
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return HTTPError(
        "http://telegram.test", code, "err", hdrs, io.BytesIO(b'{"description":"boom"}')
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


class TelegramRetryTests(unittest.TestCase):
    def _notifier(self, sleeps: list[float]):
        return TelegramNotifier(
            "token", "123", max_attempts=3, backoff_base=0.01, sleep_func=sleeps.append
        )

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_retries_on_429_then_succeeds_and_honors_retry_after(self, urlopen_mock) -> None:
        sleeps: list[float] = []
        urlopen_mock.side_effect = [
            _http_error(429, retry_after=5),
            FakeResponse({"ok": True, "result": {}}),
        ]
        sent = self._notifier(sleeps).send_text("hi")
        self.assertEqual(sent, 1)
        self.assertEqual(urlopen_mock.call_count, 2)
        self.assertEqual(sleeps, [5.0])  # honored Retry-After header

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_retries_exhaust_on_persistent_5xx(self, urlopen_mock) -> None:
        sleeps: list[float] = []
        urlopen_mock.side_effect = _http_error(503)
        with self.assertRaises(TelegramNotificationError):
            self._notifier(sleeps).send_text("hi")
        self.assertEqual(urlopen_mock.call_count, 3)  # max_attempts
        self.assertEqual(len(sleeps), 2)  # slept between the three attempts

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_permanent_4xx_is_not_retried(self, urlopen_mock) -> None:
        sleeps: list[float] = []
        urlopen_mock.side_effect = _http_error(400)
        with self.assertRaises(TelegramNotificationError):
            self._notifier(sleeps).send_text("hi")
        self.assertEqual(urlopen_mock.call_count, 1)  # gave up immediately
        self.assertEqual(sleeps, [])

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_ok_false_rate_limit_is_retried(self, urlopen_mock) -> None:
        sleeps: list[float] = []
        urlopen_mock.side_effect = [
            FakeResponse({"ok": False, "description": "Too Many Requests",
                          "parameters": {"retry_after": 4}}),
            FakeResponse({"ok": True, "result": {}}),
        ]
        sent = self._notifier(sleeps).send_text("hi")
        self.assertEqual(sent, 1)
        self.assertEqual(sleeps, [4.0])

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_partial_delivery_reports_sent_chunk_count(self, urlopen_mock) -> None:
        # First chunk delivered, second fails permanently: the caller must learn
        # that 1/2 chunks were sent, not that nothing arrived.
        sleeps: list[float] = []
        urlopen_mock.side_effect = [
            FakeResponse({"ok": True, "result": {}}),
            _http_error(400),
        ]
        with self.assertRaises(TelegramNotificationError) as ctx:
            self._notifier(sleeps).send_text("a" * 3900)  # -> 2 chunks
        self.assertEqual(ctx.exception.sent_count, 1)
        self.assertEqual(ctx.exception.total_chunks, 2)

    @patch("src.cvbankas_tracker.telegram.urlopen")
    def test_first_chunk_failure_is_a_plain_error_not_partial(self, urlopen_mock) -> None:
        sleeps: list[float] = []
        urlopen_mock.side_effect = _http_error(400)
        with self.assertRaises(TelegramNotificationError) as ctx:
            self._notifier(sleeps).send_text("a" * 3900)
        self.assertIsNone(ctx.exception.sent_count)  # nothing delivered


if __name__ == "__main__":
    unittest.main()
