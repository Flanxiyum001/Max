"""Central command policy for the coding worker.

Deny-by-default: only argv prefixes explicitly listed here may run, only
inside an isolated task workspace, with a filtered environment, hard time
and output limits, and no inherited credentials. The policy is plain data
plus validation code — it lives outside the model loop, so a model output
can never widen it.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core.tools import ToolError, resolve_within

# Anything that would let one argument become several, or become code.
_SHELL_METACHARS = re.compile(r"[;&|`$><\n\r\\]|\$\(")
_SECRET_NAME = re.compile(
    r"(?i)(_KEY|_TOKEN|_SECRET|_PASSWD|_PASSWORD|_CREDENTIAL|_COOKIE|AUTHORIZATION|AWS_|SSH_)"
)

# Leading argv the worker may execute. A command is allowed only if it
# starts with one of these exact prefixes; everything else is rejected.
ALLOWED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "pytest"),
    ("python", "-m", "unittest"),
    ("python", "-m", "compileall"),
    ("python", "-m", "max"),
    ("pytest",),
    ("git", "diff"),
    ("git", "status"),
    ("git", "log"),
    ("git", "show"),
    ("git", "ls-files"),
    ("ls",),
)

# Programs that reach the network or the host. Never allowed, regardless
# of configuration: the worker has no legitimate need for them.
DENIED_PROGRAMS = frozenset(
    {
        "curl", "wget", "nc", "netcat", "ssh", "scp", "sftp", "ftp",
        "pip", "pip3", "npm", "npx", "node", "yarn", "pnpm", "docker",
        "sudo", "su", "doas", "chmod", "chown", "rm", "mv", "sh", "bash",
        "zsh", "fish", "dash", "screen", "tmux", "systemctl", "service",
        "git-push", "python-c",
    }
)

# git subcommands that move data off the host (prefix rule already blocks
# them; this list exists so the rejection reason is explicit).
DENIED_GIT_SUBCOMMANDS = frozenset(
    {"push", "pull", "fetch", "clone", "remote", "rebase", "reset", "clean", "checkout", "switch"}
)

_MAX_ARGS = 32
_MAX_ARG_LEN = 500


@dataclass(frozen=True)
class CommandVerdict:
    allowed: bool
    reason: str


class CommandPolicy:
    """Validates argv, working directory, environment and resource limits."""

    def __init__(
        self,
        allowed_prefixes: tuple[tuple[str, ...], ...] | None = None,
        timeout_s: float = 120.0,
        max_output_chars: int = 20000,
        allow_network: bool = False,
        passthrough_env: tuple[str, ...] = (),
    ):
        self.allowed_prefixes = tuple(allowed_prefixes or ALLOWED_PREFIXES)
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        self.allow_network = allow_network
        self.passthrough_env = tuple(passthrough_env)

    # ------------------------------------------------------------- checks
    def check(self, argv: list[str]) -> CommandVerdict:
        if not argv or not all(isinstance(a, str) for a in argv):
            return CommandVerdict(False, "empty or non-string command")
        if len(argv) > _MAX_ARGS:
            return CommandVerdict(False, f"too many arguments (>{_MAX_ARGS})")
        for arg in argv:
            if len(arg) > _MAX_ARG_LEN:
                return CommandVerdict(False, "argument longer than 500 chars")
            if _SHELL_METACHARS.search(arg):
                return CommandVerdict(False, f"shell metacharacter in argument: {arg[:40]!r}")
            if ".." in arg.split("/"):
                return CommandVerdict(False, f"path traversal in argument: {arg[:40]!r}")

        program = Path(argv[0]).name
        if program in DENIED_PROGRAMS:
            return CommandVerdict(False, f"program '{program}' is never permitted")
        if program == "git" and len(argv) > 1:
            if argv[1] in DENIED_GIT_SUBCOMMANDS:
                return CommandVerdict(False, f"git subcommand '{argv[1]}' is not permitted")
        if program.startswith("python") and "-c" in argv:
            return CommandVerdict(False, "inline python (-c) is not permitted")
        if not self.allow_network and program in {"pip", "pip3", "npm", "npx", "curl", "wget"}:
            return CommandVerdict(False, "network access is disabled by configuration")

        for prefix in self.allowed_prefixes:
            if len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix:
                return CommandVerdict(True, f"allowed by prefix {' '.join(prefix)}")
        return CommandVerdict(False, "command is not in the worker command allowlist")

    def validate_cwd(self, workspace: Path, cwd: str | None) -> Path:
        """Resolve ``cwd`` strictly inside the task workspace."""
        root = workspace.resolve()
        if not cwd or cwd in {".", "./"}:
            return root
        return resolve_within(root, cwd)

    # ---------------------------------------------------------- execution
    def build_env(self, workspace: Path) -> dict[str, str]:
        """Minimal environment: no host credentials, no provider keys."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(workspace),
            "TMPDIR": str(workspace / ".max-tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PIP_NO_INPUT": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        for name in self.passthrough_env:
            value = os.environ.get(name)
            if value is not None and not _SECRET_NAME.search(name):
                env[name] = value
        # Defence in depth: never forward anything that looks like a secret.
        return {k: v for k, v in env.items() if not _SECRET_NAME.search(k)}

    def run(
        self,
        argv: list[str],
        workspace: Path,
        cwd: str | None = None,
        timeout_s: float | None = None,
    ) -> dict:
        """Run an approved command. Returns a bounded, auditable result."""
        verdict = self.check(argv)
        if not verdict.allowed:
            raise ToolError(f"command rejected: {verdict.reason}")
        workdir = self.validate_cwd(workspace, cwd)
        if not workdir.is_dir():
            raise ToolError(f"working directory does not exist: {cwd}")
        (workdir / ".max-tmp").mkdir(parents=True, exist_ok=True)
        env = self.build_env(workdir)
        limit = min(timeout_s or self.timeout_s, self.timeout_s)
        started = __import__("time").monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(workdir),
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=limit,
            )
            timed_out = False
            code = proc.returncode
            out, err = proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            code = -1
            out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = "timed out"
        except OSError as exc:
            raise ToolError(f"failed to execute: {exc}") from None

        elapsed = round(__import__("time").monotonic() - started, 3)
        out = _truncate(out, self.max_output_chars)
        err = _truncate(err, self.max_output_chars // 2)
        return {
            "argv": argv,
            "cwd": str(workdir),
            "exit_code": code,
            "timed_out": timed_out,
            "duration_s": elapsed,
            "stdout": out,
            "stderr": err,
            "allowed_reason": verdict.reason,
        }


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
