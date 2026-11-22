"""Bounded, shell-free subprocess runner for host-declared developer commands.

Lives outside local_tools on purpose: the static doctrine test forbids raw
subprocess use inside the tool executor module. Only fixed argv lists
(never a shell) run here, with a scrubbed environment, hard timeout and
bounded, control-character-sanitized output.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

_MAX_OUTPUT_CHARS = 8_000
_TIMEOUT_SECONDS_DEFAULT = 30
ENV_KEYS = (
    "PATH",
    "HOME",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
)


class BoundedCommandError(Exception):
    """Fixed safe codes only; never embeds raw stderr or paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _bounded_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _sanitize_output(value: str) -> str:
    cleaned = "".join(
        ch if ch in ("\t", "\n") or (ord(ch) > 0x1F and ch != "\x7f") else "\ufffd" for ch in value
    )
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    if len(cleaned) <= _MAX_OUTPUT_CHARS:
        return cleaned
    return cleaned[:_MAX_OUTPUT_CHARS] + "\n(max_dev_output_truncated)"


def run_bounded_command(
    workspace_root: Path,
    argv: Sequence[str],
    timeout_seconds: int = _TIMEOUT_SECONDS_DEFAULT,
) -> dict[str, object]:
    """Run one fixed argv list (never a shell) with bounded env, time and output."""
    try:
        completed = subprocess.run(
            [str(part) for part in argv],
            cwd=str(workspace_root),
            env=_bounded_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise BoundedCommandError("tool_timeout") from None
    except FileNotFoundError:
        raise BoundedCommandError("tool_unavailable") from None
    except OSError:
        raise BoundedCommandError("tool_failed") from None
    output = _sanitize_output(completed.stdout + completed.stderr)
    return {
        "exit_code": completed.returncode,
        "truncated": len(completed.stdout) + len(completed.stderr) > _MAX_OUTPUT_CHARS,
        "output": output,
    }
