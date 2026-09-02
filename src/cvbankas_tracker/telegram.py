from __future__ import annotations

import html
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import ApplicationRecord, Vacancy, VacancyAnalysis

TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_CHUNK_LIMIT = 3800
# Transient Telegram failures (rate limits, 5xx, network blips) are retried with
# exponential backoff so a single hiccup does not drop the daily summary.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0


class TelegramNotificationError(RuntimeError):
    """Raised when Telegram configuration or delivery fails.

    ``sent_count``/``total_chunks`` are populated when a multi-chunk message was
    *partially* delivered (some chunks reached Telegram before a later one
    failed), so the caller can record that partial state instead of treating the
    whole summary as unsent.
    """

    def __init__(
        self,
        *args: object,
        sent_count: int | None = None,
        total_chunks: int | None = None,
    ) -> None:
        super().__init__(*args)
        self.sent_count = sent_count
        self.total_chunks = total_chunks


@dataclass(frozen=True, slots=True)
class TelegramChat:
    chat_id: str
    label: str


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        api_base: str = "https://api.telegram.org",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self._bot_token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._api_base = api_base.rstrip("/")
        # Injectable so retry/backoff can be exercised without real delays.
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_base = max(0.0, float(backoff_base))
        self._sleep = sleep_func
        if not self._bot_token:
            raise TelegramNotificationError("TELEGRAM_BOT_TOKEN is not configured.")
        if not self._chat_id:
            raise TelegramNotificationError("TELEGRAM_CHAT_ID is not configured.")

    def _backoff_delay(self, attempt: int) -> float:
        return min(BACKOFF_CAP_SECONDS, self._backoff_base * (2 ** (attempt - 1)))

    @classmethod
    def from_env(cls) -> TelegramNotifier:
        return cls(
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_CHAT_ID", ""),
        )

    def send_text(self, text: str) -> int:
        chunks = split_telegram_text(text)
        for index, chunk in enumerate(chunks):
            try:
                self._call_api(
                    "sendMessage",
                    {
                        "chat_id": self._chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                    },
                )
            except TelegramNotificationError as error:
                if index == 0:
                    raise  # nothing was delivered; surface the plain error
                # Some chunks already reached Telegram: report the partial state
                # so the caller can log it rather than treating it as unsent.
                raise TelegramNotificationError(
                    f"Partial Telegram delivery: {index}/{len(chunks)} chunk(s) sent "
                    f"before failure: {error}",
                    sent_count=index,
                    total_chunks=len(chunks),
                ) from error
        return len(chunks)

    def send_daily_summary(
        self,
        rows: list[tuple[Vacancy, VacancyAnalysis, ApplicationRecord | None]],
        *,
        source_names: list[str],
        attempted_count: int,
        failed_count: int,
        max_vacancies: int = 10,
        notify_when_empty: bool = False,
        source_errors: list[str] | None = None,
        recovered_count: int = 0,
    ) -> int:
        if not rows and not notify_when_empty and not source_errors:
            return 0

        text = build_daily_summary(
            rows,
            source_names=source_names,
            attempted_count=attempted_count,
            failed_count=failed_count,
            max_vacancies=max_vacancies,
            source_errors=source_errors,
            recovered_count=recovered_count,
        )
        return self.send_text(text)

    def _call_api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self._api_base}/bot{self._bot_token}/{method}"
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_error: TelegramNotificationError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                with urlopen(request, timeout=30) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                detail = _telegram_error_detail(error)
                # 429 (rate limit) and 5xx are transient -> back off and retry;
                # other 4xx (bad request, bad token/chat) are permanent.
                if error.code == 429 or error.code >= 500:
                    last_error = TelegramNotificationError(
                        f"Telegram API rejected {method}: {detail}"
                    )
                    if attempt < self._max_attempts:
                        self._sleep(_retry_after_seconds(error) or self._backoff_delay(attempt))
                        continue
                    raise last_error from error
                raise TelegramNotificationError(
                    f"Telegram API rejected {method}: {detail}"
                ) from error
            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = TelegramNotificationError(
                    f"Telegram API request failed during {method}: {type(error).__name__}"
                )
                if attempt < self._max_attempts:
                    self._sleep(self._backoff_delay(attempt))
                    continue
                raise last_error from error

            if not data.get("ok"):
                description = str(data.get("description") or "unknown Telegram error")
                retry_after = _ok_false_retry_after(data)
                if retry_after is not None and attempt < self._max_attempts:
                    last_error = TelegramNotificationError(description)
                    self._sleep(retry_after)
                    continue
                raise TelegramNotificationError(description)
            return data

        raise last_error or TelegramNotificationError(
            f"Telegram {method} failed after {self._max_attempts} attempts."
        )


def discover_telegram_chats(bot_token: str) -> list[TelegramChat]:
    token = bot_token.strip()
    if not token:
        raise TelegramNotificationError("TELEGRAM_BOT_TOKEN is not configured.")

    endpoint = f"https://api.telegram.org/bot{token}/getUpdates"
    request = Request(
        endpoint,
        data=json.dumps({"limit": 100, "timeout": 0}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = _telegram_error_detail(error)
        raise TelegramNotificationError(
            f"Telegram API rejected getUpdates: {detail}"
        ) from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise TelegramNotificationError(
            f"Telegram API request failed during getUpdates: {type(error).__name__}"
        ) from error

    if not data.get("ok"):
        raise TelegramNotificationError(
            str(data.get("description") or "Could not read Telegram updates.")
        )

    chats: dict[str, TelegramChat] = {}
    for update in data.get("result", []):
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
        )
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or "id" not in chat:
            continue
        chat_id = str(chat["id"])
        label = _chat_label(chat)
        chats[chat_id] = TelegramChat(chat_id=chat_id, label=label)
    return list(chats.values())


def build_daily_summary(
    rows: list[tuple[Vacancy, VacancyAnalysis, ApplicationRecord | None]],
    *,
    source_names: list[str],
    attempted_count: int,
    failed_count: int,
    max_vacancies: int = 10,
    source_errors: list[str] | None = None,
    recovered_count: int = 0,
) -> str:
    sorted_rows = sorted(
        rows,
        key=lambda row: (-row[1].score, row[0].title.lower()),
    )
    date_label = datetime.now().strftime("%Y-%m-%d")
    source_label = ", ".join(source_names) or "-"
    lines = [
        f"<b>Job Seeker daily summary - {date_label}</b>",
        "",
        f"New vacancies: <b>{len(rows)}</b>",
        f"Checked vacancy pages: {attempted_count}",
        f"Source errors: {failed_count}",
        f"Sources: {html.escape(source_label)}",
    ]
    if recovered_count:
        lines.insert(3, f"Recovered after interrupted run: <b>{recovered_count}</b>")

    if source_errors:
        visible_errors = source_errors[:8]
        lines.extend(["", "<b>Errors</b>"])
        lines.extend(f"• {html.escape(error)}" for error in visible_errors)
        hidden_error_count = len(source_errors) - len(visible_errors)
        if hidden_error_count > 0:
            lines.append(f"• Plus {hidden_error_count} more error(s) in the local log.")

    if not sorted_rows:
        lines.extend(["", "No new matching vacancies today."])
        return "\n".join(lines)

    visible_rows = sorted_rows[: max(1, max_vacancies)]
    lines.extend(["", f"<b>Top {len(visible_rows)}</b>"])
    for index, (vacancy, analysis, _application) in enumerate(visible_rows, start=1):
        title = html.escape(vacancy.title or "Untitled vacancy")
        company = html.escape(vacancy.company or "Unknown company")
        location = html.escape(vacancy.location or "Location not specified")
        source = html.escape(vacancy.source_name)
        url = html.escape(vacancy.source_url, quote=True)
        lines.extend(
            [
                "",
                f"{index}. <b>{title}</b>",
                f"{company} | {location}",
                f"Score: <b>{analysis.score}</b> ({html.escape(analysis.fit_label.value)}) | {source}",
                f'<a href="{url}">Open vacancy</a>',
            ]
        )

    hidden_count = len(sorted_rows) - len(visible_rows)
    if hidden_count > 0:
        lines.extend(["", f"Plus {hidden_count} more new vacancies in the local report."])
    return "\n".join(lines)


def split_telegram_text(text: str, limit: int = DEFAULT_CHUNK_LIMIT) -> list[str]:
    if limit <= 0 or limit > TELEGRAM_MESSAGE_LIMIT:
        raise ValueError(f"Telegram chunk limit must be between 1 and {TELEGRAM_MESSAGE_LIMIT}.")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(block) > limit:
            split_at = block.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(block[:split_at].rstrip())
            block = block[split_at:].lstrip()
        current = block
    if current:
        chunks.append(current)
    return chunks


def _chat_label(chat: dict[str, Any]) -> str:
    title = str(chat.get("title") or "").strip()
    username = str(chat.get("username") or "").strip()
    full_name = " ".join(
        part
        for part in (
            str(chat.get("first_name") or "").strip(),
            str(chat.get("last_name") or "").strip(),
        )
        if part
    )
    return title or (f"@{username}" if username else full_name) or str(chat.get("type", "chat"))


def _retry_after_seconds(error: HTTPError) -> float | None:
    """Honor Telegram's ``Retry-After`` header on a 429, if present and sane."""
    try:
        raw = error.headers.get("Retry-After") if error.headers else None
    except AttributeError:
        raw = None
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, BACKOFF_CAP_SECONDS)


def _ok_false_retry_after(data: dict[str, Any]) -> float | None:
    """Extract ``parameters.retry_after`` from an ``ok:false`` rate-limit body."""
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        return None
    raw = parameters.get("retry_after")
    try:
        seconds = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, BACKOFF_CAP_SECONDS)


def _telegram_error_detail(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        return str(payload.get("description") or f"HTTP {error.code}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"HTTP {error.code}"
