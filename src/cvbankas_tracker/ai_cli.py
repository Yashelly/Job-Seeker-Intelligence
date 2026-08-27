from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_CLI_TIMEOUT = 240


def _resolve_command(command: str) -> str:
    """Return an executable path for ``command``.

    On Windows, npm-installed CLIs are ``.cmd`` shims (e.g. ``codex.cmd``) that
    ``subprocess`` does not find by bare name; ``shutil.which`` resolves the
    right extension. Falls back to the bare name so a genuinely missing command
    still raises FileNotFoundError with a clear message.
    """
    return shutil.which(command) or command

_SCORE_DIGITS = re.compile(r"-?\d+")


class AICLIError(RuntimeError):
    """Raised when a subscription-backed CLI backend fails to produce a usable response."""


def coerce_score(raw: object, *, low: int = 0, high: int = 100) -> int:
    """Best-effort parse of a model-provided score into a clamped integer.

    Language models return scores as ints, floats, or strings ("85", "85%",
    "high"). Anything unparseable collapses to ``low`` rather than raising, so a
    malformed field never crashes the analysis pipeline. ``bool`` is rejected
    explicitly because it is an ``int`` subclass but never a real score.
    """
    if isinstance(raw, bool):
        return low
    if isinstance(raw, (int, float)):
        value: float = raw
    else:
        match = _SCORE_DIGITS.search(str(raw))
        if not match:
            return low
        value = int(match.group())
    return max(low, min(high, int(value)))


def coerce_str_list(raw: object) -> list[str]:
    """Coerce a model field into a list of non-empty strings.

    Tolerates the common malformed shapes: a bare string (wrapped into a single
    item), ``None`` (empty), or a list containing non-string scalars (stringified)
    and nested structures (skipped). Never raises on unexpected types.
    """
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, (list, tuple)):
        return []
    items: list[str] = []
    for value in raw:
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        text = str(value).strip()
        if text:
            items.append(text)
    return items


def _strip_fenced_json(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_response(text: str) -> dict[str, object]:
    """Parse a JSON object out of raw CLI model output, tolerating fences and stray prose."""
    candidate = _strip_fenced_json(text)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise AICLIError(f"CLI backend returned non-JSON output: {text[:500]!r}") from None
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AICLIError(f"CLI backend returned unparseable JSON: {text[:500]!r}") from exc

    if not isinstance(data, dict):
        raise AICLIError("CLI backend JSON payload was not an object.")
    return data


def run_claude_cli(
    prompt: str,
    *,
    command: str = "claude",
    model: str | None = None,
    timeout: int = DEFAULT_CLI_TIMEOUT,
) -> str:
    """Run a headless Claude Code turn using the local subscription auth and return the model text.

    Uses ``claude -p ... --output-format json``; the actual answer is the ``result`` field
    of the JSON envelope printed to stdout.
    """
    args = [_resolve_command(command), "-p", prompt, "--output-format", "json"]
    if model:
        args += ["--model", model]

    # Run in a throwaway working directory, never the project root: vacancy text
    # is untrusted, and an agentic CLI's relative file operations must not be able
    # to touch the app's own files (audit finding #2). Auth lives in the user's
    # home/env, so an isolated cwd does not disturb subscription login.
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_cwd:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                cwd=tmp_cwd,
            )
    except FileNotFoundError as exc:
        raise AICLIError(f"Claude CLI '{command}' was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AICLIError(f"Claude CLI timed out after {timeout}s.") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AICLIError(f"Claude CLI failed (exit {completed.returncode}): {detail[:500]}")

    try:
        envelope = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AICLIError(f"Claude CLI returned unparseable output: {completed.stdout[:500]!r}") from exc

    if isinstance(envelope, dict) and envelope.get("is_error"):
        raise AICLIError(f"Claude CLI reported an error: {str(envelope.get('result', ''))[:500]}")

    if isinstance(envelope, dict):
        return str(envelope.get("result", ""))
    return str(envelope)


def run_codex_cli(
    prompt: str,
    *,
    command: str = "codex",
    model: str | None = None,
    timeout: int = DEFAULT_CLI_TIMEOUT,
) -> str:
    """Run a headless Codex turn using the local ChatGPT subscription auth and return the model text.

    Uses ``codex exec -o <file> ...`` so the final assistant message is written cleanly to a file.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        out_path = Path(tmp_dir) / "codex_last_message.txt"
        args = [_resolve_command(command), "exec", "--skip-git-repo-check", "-o", str(out_path)]
        if model:
            args += ["-m", model]
        args.append(prompt)

        try:
            # Isolate the working directory from the project root: untrusted
            # vacancy text must not steer an agentic CLI into the app's files
            # (audit finding #2).
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                cwd=tmp_dir,
            )
        except FileNotFoundError as exc:
            raise AICLIError(f"Codex CLI '{command}' was not found on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise AICLIError(f"Codex CLI timed out after {timeout}s.") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise AICLIError(f"Codex CLI failed (exit {completed.returncode}): {detail[:500]}")

        try:
            return out_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AICLIError(f"Codex CLI produced no output file: {exc}") from exc
