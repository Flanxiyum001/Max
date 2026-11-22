"""Ephemeral, network-disabled Docker sandbox for LLM-generated Python scripts.

Fail-closed by design: when Docker is unavailable, execution is refused with a
fixed safe code instead of falling back to host interpretation. Every run uses
a fixed hardened argv (never a shell), a fresh scratch directory mounted
read-write at /work, no network, no capabilities, non-root uid, bounded
memory/CPU/PIDs, and a scrubbed host environment.
"""

from __future__ import annotations

import shutil
import stat
import uuid
from pathlib import Path

from max_core.bounded_commands import BoundedCommandError, run_bounded_command

_ALLOWED_IMAGES = frozenset({"python:3.11-slim", "python:3.12-slim"})
_DEFAULT_IMAGE = "python:3.12-slim"
_MAX_SCRIPT_CHARS = 32_000
_EXECUTION_TIMEOUT_SECONDS = 120
_SCRATCH_DIR_NAME = ".max-scratch"


def validate_python_script(raw: object) -> str:
    """Only a bounded, NUL-free, UTF-8-clean string is accepted."""
    if not isinstance(raw, str) or not 1 <= len(raw) <= _MAX_SCRIPT_CHARS:
        raise BoundedCommandError("invalid_script")
    if "\x00" in raw:
        raise BoundedCommandError("invalid_script")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise BoundedCommandError("invalid_script") from None
    return raw


def _docker_argv(scratch_host: Path, image: str) -> tuple[str, ...]:
    return (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "256m",
        "--cpus",
        "1.0",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp:rw,size=8m,noexec,nodev,nosuid",
        "-v",
        f"{scratch_host}:/work:rw",
        "-w",
        "/work",
        "-u",
        "65534:65534",
        image,
        "python",
        "-I",
        "-B",
        "/work/script.py",
    )


def run_python_sandboxed(
    workspace_root: Path,
    script: object,
    *,
    image: str = _DEFAULT_IMAGE,
    timeout_seconds: int = _EXECUTION_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Run one validated script in an ephemeral hardened container; clean up after."""
    if image not in _ALLOWED_IMAGES:
        raise BoundedCommandError("invalid_script_image")
    validated = validate_python_script(script)
    if shutil.which("docker") is None:
        raise BoundedCommandError("tool_unavailable")
    scratch_root = workspace_root / _SCRATCH_DIR_NAME
    scratch_dir = scratch_root / f"run-{uuid.uuid4().hex}"
    try:
        try:
            scratch_root.mkdir(parents=True, exist_ok=True)
            scratch_dir.mkdir()
            scratch_dir.chmod(0o777)
            (scratch_dir / "script.py").write_text(validated, encoding="utf-8")
            (scratch_dir / "script.py").chmod(0o644)
        except OSError:
            raise BoundedCommandError("tool_failed") from None
        result = run_bounded_command(
            workspace_root, _docker_argv(scratch_dir, image), timeout_seconds
        )
    finally:
        _force_remove_tree(scratch_dir)
        try:
            scratch_root.rmdir()
        except OSError:
            pass
    result["tool"] = "code.execute_python"
    return result


def _force_remove_tree(target: Path) -> None:
    try:
        for child in target.rglob("*"):
            try:
                child.chmod(child.stat().st_mode | stat.S_IRWXU)
            except OSError:
                pass
        target.chmod(0o755)
    except OSError:
        pass
    shutil.rmtree(target, ignore_errors=True)
