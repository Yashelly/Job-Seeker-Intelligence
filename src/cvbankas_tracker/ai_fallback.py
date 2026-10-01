from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if code:
        return str(code).strip().lower()

    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested = body.get("error", body)
        if isinstance(nested, dict) and nested.get("code"):
            return str(nested["code"]).strip().lower()
    return ""


def permanent_ai_error_reason(error: Exception) -> str | None:
    """Return a safe user-facing reason when retrying this run cannot help."""

    code = _error_code(error)
    detail = str(error).lower()
    if code == "insufficient_quota" or "insufficient_quota" in detail:
        return "OpenAI API quota exhausted"
    if "no credits remaining" in detail:
        return "OpenAI API quota exhausted"
    if code in {"invalid_api_key", "authentication_error"}:
        return "OpenAI API authentication failed"
    if getattr(error, "status_code", None) == 401:
        return "OpenAI API authentication failed"
    return None


def safe_ai_error_reason(error: Exception) -> str:
    """Return a concise reason without embedding a provider response body."""

    permanent_reason = permanent_ai_error_reason(error)
    if permanent_reason:
        return permanent_reason
    return f"{type(error).__name__}: AI provider request failed"


@dataclass(slots=True)
class AIProviderState:
    """Run-scoped circuit breaker shared by extraction and scoring workers."""

    _disabled_reason: str | None = None
    _analysis_fallback_count: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    @property
    def disabled_reason(self) -> str | None:
        with self._lock:
            return self._disabled_reason

    @property
    def analysis_fallback_count(self) -> int:
        with self._lock:
            return self._analysis_fallback_count

    def record_failure(self, error: Exception) -> str:
        reason = safe_ai_error_reason(error)
        permanent_reason = permanent_ai_error_reason(error)
        if permanent_reason:
            with self._lock:
                if self._disabled_reason is None:
                    self._disabled_reason = permanent_reason
        return reason

    def record_analysis_fallback(self) -> None:
        with self._lock:
            self._analysis_fallback_count += 1
