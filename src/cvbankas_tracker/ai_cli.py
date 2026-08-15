from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

DEFAULT_CLI_TIMEOUT = 240


class AICLIError(RuntimeError):
    """Raised when a subscription-backed CLI backend fails to produce a usable response."""


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
            raise AICLIError(f"CLI backend returned non-JSON output: {text[:500]!r}")
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
    args = [command, "-p", prompt, "--output-format", "json"]
    if model:
        args += ["--model", model]

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
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
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "codex_last_message.txt"
        args = [command, "exec", "--skip-git-repo-check", "-o", str(out_path)]
        if model:
            args += ["-m", model]
        args.append(prompt)

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
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
