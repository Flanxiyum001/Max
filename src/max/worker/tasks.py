"""Coding task lifecycle: create → worktree → run → diff → approve/discard.

Each task is an isolated `git worktree` plus a dedicated branch, so a
coding change can be inspected, tested and discarded without touching the
caller's checkout. Approval is requested by the model and granted only
outside the model (CLI/admin); the model cannot approve its own action.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.tools import ToolError, resolve_within

# Patterns that must never leave the worker as part of a diff.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("discord_token", re.compile(r"\b\d{17,19}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b")),
    ("assigned_secret", re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*[\"']?[^\s\"']{8,}")),
)

_MAX_DIFF_CHARS = 40000
_MAX_TASKS = 16


@dataclass
class Task:
    id: str
    repo: Path
    worktree: Path
    branch: str
    summary: str
    created_at: float = field(default_factory=time.time)
    state: str = "active"  # active | cancelled | discarded
    last_run: dict | None = None
    pending_approval: dict | None = None
    approvals: dict[str, bool] = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "id": self.id,
            "repo": str(self.repo),
            "worktree": str(self.worktree),
            "branch": self.branch,
            "summary": self.summary,
            "state": self.state,
            "created_at": round(self.created_at, 3),
            "pending_approval": self.pending_approval,
            "approvals": dict(self.approvals),
            "last_run": (
                {k: v for k, v in self.last_run.items() if k not in {"stdout", "stderr"}}
                if self.last_run
                else None
            ),
        }


class TaskManager:
    def __init__(
        self,
        allowed_repos: list[Path],
        workspace_dir: Path,
        policy,  # CommandPolicy
        audit=None,
        max_tasks: int = _MAX_TASKS,
    ):
        self.allowed_repos = [Path(p).resolve() for p in allowed_repos]
        self.root = Path(workspace_dir).resolve() / "worker-tasks"
        self.policy = policy
        self.audit = audit
        self.max_tasks = max_tasks
        self.tasks: dict[str, Task] = {}

    # -------------------------------------------------------------- helpers
    def _record(self, event: str, **detail) -> None:
        if self.audit is not None:
            self.audit.record(event, **detail)

    def _git(self, args: list[str], cwd: Path | None = None, timeout: float = 60) -> str:
        """Fixed git invocations built by MAX, never by the model."""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env={
                    "PATH": __import__("os").environ.get("PATH", "/usr/bin:/bin"),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "HOME": str(self.root),
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(f"git failed: {exc}") from None
        if proc.returncode != 0:
            raise ToolError(f"git {' '.join(args[:2])} failed: {(proc.stderr or '').strip()[:300]}")
        return proc.stdout

    def _resolve_repo(self, repo: str | None) -> Path:
        if not self.allowed_repos:
            raise ToolError("no repositories are allowlisted for the coding worker")
        if repo:
            raw = str(repo).strip()
            if raw.startswith("/"):
                candidate = Path(raw).resolve()
                # Absolute paths must still land inside an allowlisted root.
                if not any(candidate == root or root in candidate.parents for root in self.allowed_repos):
                    raise ToolError("repository is not in the worker allowlist")
            else:
                candidate = resolve_within(self.allowed_repos[0], raw)
        else:
            candidate = self.allowed_repos[0]
        if not (candidate / ".git").exists():
            raise ToolError(f"not a git repository: {candidate}")
        return candidate

    def get(self, task_id: str) -> Task:
        task = self.tasks.get(str(task_id or "").strip())
        if task is None:
            raise ToolError(f"unknown task id: {task_id}")
        if task.state == "discarded":
            raise ToolError("task workspace was discarded")
        return task

    # ------------------------------------------------------------ lifecycle
    def create(self, summary: str, repo: str | None = None) -> Task:
        if len(self.tasks) >= self.max_tasks:
            raise ToolError("task limit reached — discard an existing task first")
        target = self._resolve_repo(repo)
        task_id = f"t{secrets.token_hex(4)}"
        worktree = self.root / task_id
        branch = f"worker/{task_id}"
        self.root.mkdir(parents=True, exist_ok=True)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._git(["-C", str(target), "worktree", "add", "-b", branch, str(worktree), "HEAD"])
        task = Task(id=task_id, repo=target, worktree=worktree, branch=branch, summary=str(summary)[:300])
        self.tasks[task_id] = task
        self._record("worker_task_created", task_id=task_id, repo=str(target), branch=branch)
        return task

    def status(self, task_id: str) -> dict:
        task = self.get(task_id)
        changed = self._changed_files(task)
        info = task.public()
        info["changed_files"] = changed
        info["dirty"] = bool(changed)
        return info

    def run(
        self,
        task_id: str,
        argv: list[str],
        cwd: str | None = None,
        timeout_s: float | None = None,
    ) -> dict:
        task = self.get(task_id)
        if task.state != "active":
            raise ToolError(f"task is {task.state}")
        result = self.policy.run(argv, task.worktree, cwd=cwd, timeout_s=timeout_s)
        task.last_run = result
        self._record(
            "worker_command",
            task_id=task_id,
            command=" ".join(argv)[:200],
            exit_code=result["exit_code"],
            timed_out=result["timed_out"],
            duration_s=result["duration_s"],
        )
        return result

    def diff(self, task_id: str) -> dict:
        task = self.get(task_id)
        base = str(task.worktree)
        stat = self._git(["-C", base, "diff", "--stat", "HEAD", "--"], cwd=None)
        full = self._git(["-C", base, "diff", "HEAD", "--"], cwd=None)
        untracked = [
            line
            for line in self._git(
                ["-C", base, "ls-files", "--others", "--exclude-standard"], cwd=None
            ).splitlines()
            if line.strip()
        ]
        if len(full) > _MAX_DIFF_CHARS:
            full = full[:_MAX_DIFF_CHARS] + f"\n...[truncated {len(full) - _MAX_DIFF_CHARS} chars]"
        # Untracked files never appear in `git diff` — scan their contents too,
        # otherwise an accidentally created secret file would pass unnoticed.
        scan_text = full
        for name in untracked[:50]:
            try:
                target = (task.worktree / name)
                if target.is_file() and target.stat().st_size <= 256 * 1024:
                    scan_text += "\n" + target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        findings = scan_for_secrets(scan_text)
        summary = self.summarize_diff(stat, untracked, findings)
        self._record(
            "worker_diff",
            task_id=task_id,
            files=len(_changed_file_names(stat)),
            secret_findings=[k for k, _ in findings],
        )
        return {
            "task_id": task_id,
            "stat": stat.strip(),
            "untracked": untracked[:50],
            "diff": full,
            "secret_findings": [k for k, _ in findings],
            "summary": summary,
        }

    def cancel(self, task_id: str) -> dict:
        task = self.get(task_id)
        task.state = "cancelled"
        self._record("worker_task_cancelled", task_id=task_id)
        return {"task_id": task_id, "state": task.state}

    def discard(self, task_id: str) -> dict:
        task = self.tasks.get(str(task_id or "").strip())
        if task is None:
            raise ToolError(f"unknown task id: {task_id}")
        removed = True
        if task.worktree.exists():
            try:
                self._git(
                    ["-C", str(task.repo), "worktree", "remove", "--force", str(task.worktree)]
                )
            except ToolError:
                removed = False
        try:
            self._git(["-C", str(task.repo), "branch", "-D", task.branch])
        except ToolError:
            pass
        task.state = "discarded"
        self._record("worker_task_discarded", task_id=task_id, worktree_removed=removed)
        return {"task_id": task_id, "state": task.state, "worktree_removed": removed}

    # ------------------------------------------------------------ approvals
    def request_approval(self, task_id: str, action: str, reason: str = "") -> dict:
        """The model may only *request* approval; it never grants it."""
        task = self.get(task_id)
        action = str(action)[:120]
        task.pending_approval = {
            "action": action,
            "reason": str(reason)[:300],
            "requested_at": time.time(),
            "requested_by": "model",
        }
        self._record("worker_approval_requested", task_id=task_id, action=action)
        return {"task_id": task_id, "pending_approval": task.pending_approval, "granted": False}

    def grant_approval(self, task_id: str, action: str, granted_by: str) -> dict:
        """Human entry point (CLI/admin). Never callable from tool output."""
        task = self.get(task_id)
        action = str(action)[:120]
        task.approvals[action] = True
        task.pending_approval = None
        self._record(
            "worker_approval_granted", task_id=task_id, action=action, granted_by=str(granted_by)[:80]
        )
        return {"task_id": task_id, "action": action, "granted": True, "granted_by": str(granted_by)[:80]}

    # -------------------------------------------------------------- helpers
    def _changed_files(self, task: Task) -> list[str]:
        base = str(task.worktree)
        stat = self._git(["-C", base, "diff", "--stat", "HEAD", "--"], cwd=None)
        names = _changed_file_names(stat)
        untracked = [
            line
            for line in self._git(
                ["-C", base, "ls-files", "--others", "--exclude-standard"], cwd=None
            ).splitlines()
            if line.strip()
        ]
        return sorted(set(names) | set(untracked))

    @staticmethod
    def summarize_diff(stat: str, untracked: list[str], findings: list[tuple[str, re.Pattern]]) -> str:
        files = _changed_file_names(stat)
        parts = [f"{len(files)} modified file(s)"]
        if untracked:
            parts.append(f"{len(untracked)} untracked file(s)")
        if findings:
            parts.append(f"WARNING: {len(findings)} possible secret(s) in diff — do not push")
        return ", ".join(parts)


def _changed_file_names(stat: str) -> list[str]:
    names = []
    for line in (stat or "").splitlines():
        if "|" " " not in line:
            continue
        name = line.split("|", 1)[0].strip()
        if name and not name.startswith((" ", "...")):
            names.append(name)
    return names


def scan_for_secrets(text: str) -> list[tuple[str, re.Pattern[str]]]:
    """Return (label, pattern) for every secret-shaped match in ``text``."""
    hits = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text or ""):
            hits.append((label, pattern))
    return hits
