"""Specialist worker profiles and the worker registry.

A *profile* is a static, configuration-independent description of what a
worker may do: its responsibilities, the tools it may ask for, and whether it
may write. A *worker* is the on-demand callable that executes one task.

Profiles never carry permission authority: ``max.orchestration.policy``
intersects the profile's request with MAX's real ``PermissionPolicy`` and the
tool registry, and every call is audited with the task id.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import Task, WorkerResult


@dataclass(frozen=True)
class WorkerProfile:
    name: str
    summary: str
    responsibilities: tuple[str, ...]
    # Tools the profile *requests*. The effective set is the intersection with
    # the tool registry and MAX's PermissionPolicy — never a grant by itself.
    requested_tools: tuple[str, ...] = ()
    requested_categories: tuple[str, ...] = ()
    may_write: bool = False
    may_execute: bool = False
    requires_workspace: bool = False
    max_attempts: int = 1
    timeout_s: float = 120.0


PROFILES: dict[str, WorkerProfile] = {
    "researcher": WorkerProfile(
        name="researcher",
        summary="Read-only inspection of repositories, files and documentation.",
        responsibilities=(
            "inspect repository structure and source files",
            "use file search and tree-sitter symbol navigation when available",
            "return concise findings with file paths and line ranges",
        ),
        requested_tools=("read_file", "list_files", "search_files", "list_symbols", "find_symbol"),
        requested_categories=("read",),
        may_write=False,
        may_execute=False,
    ),
    "coder": WorkerProfile(
        name="coder",
        summary="Implements a scoped change inside an isolated task workspace.",
        responsibilities=(
            "modify only files inside the assigned task workspace",
            "run the approved test command for the assigned scope",
            "return a bounded diff, changed files and real test results",
        ),
        requested_tools=(
            "read_file",
            "list_files",
            "search_files",
            "list_symbols",
            "find_symbol",
            "write_file",
            "worker_create",
            "worker_run",
            "worker_diff",
            "worker_status",
            "worker_discard",
        ),
        requested_categories=("read", "write"),
        may_write=True,
        may_execute=True,  # only through the approved command runner
        requires_workspace=True,
        timeout_s=180.0,
    ),
    "tester": WorkerProfile(
        name="tester",
        summary="Runs approved tests and reports exactly what ran.",
        responsibilities=(
            "run the approved test command inside the workspace",
            "reproduce reported failures",
            "report blocked or unavailable tests instead of guessing",
        ),
        requested_tools=(
            "read_file",
            "list_files",
            "search_files",
            "worker_create",
            "worker_run",
            "worker_status",
            "worker_diff",
        ),
        requested_categories=("read",),
        may_write=False,
        may_execute=True,
        requires_workspace=True,
        timeout_s=180.0,
    ),
    "reviewer": WorkerProfile(
        name="reviewer",
        summary="Independent read-only review of a proposed diff.",
        responsibilities=(
            "inspect the proposed diff, changed files and test evidence",
            "check scope, secrets, policy compliance and missing tests",
            "return a clear verdict the supervisor aggregates — it cannot approve its own work",
        ),
        requested_tools=("read_file", "list_files", "search_files"),
        requested_categories=("read",),
        may_write=False,
        may_execute=False,
    ),
}


# ------------------------------------------------------------------ workers

WorkerFn = Callable[[Task, "WorkerContext"], Any]


@dataclass
class WorkerContext:
    """Everything a worker gets — and nothing more."""

    task: Task
    grant: Any  # orchestration.policy.WorkerGrant
    workspace: Any = None
    audit: Any = None
    extras: dict = field(default_factory=dict)

    def tool(self, name: str, arguments: dict | None = None) -> str:
        """Run a tool through the central authorization boundary."""
        from .policy import ToolDenied

        runner = self.extras.get("tool_runner")
        if runner is None:
            raise ToolDenied("no tool runner bound to this worker context")
        return runner(self.grant, name, arguments or {})


@dataclass
class Worker:
    name: str
    profile: str
    fn: WorkerFn
    description: str = ""


class WorkerRegistry:
    """Named, on-demand workers. No worker runs until a task selects it."""

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}

    def register(self, worker: Worker) -> None:
        if worker.profile not in PROFILES:
            raise KeyError(f"unknown profile {worker.profile!r}")
        self._workers[worker.name] = worker

    def get(self, name: str) -> Worker | None:
        return self._workers.get(name)

    def for_profile(self, profile: str) -> Worker | None:
        for worker in self._workers.values():
            if worker.profile == profile:
                return worker
        return None

    def names(self) -> list[str]:
        return sorted(self._workers)


def function_worker(name: str, profile: str, fn: WorkerFn, description: str = "") -> Worker:
    return Worker(name=name, profile=profile, fn=fn, description=description)


def ok_result(task: Task, summary: str, **kwargs) -> WorkerResult:
    payload = {"status": "completed", "summary": summary, "task_id": task.id, **kwargs}
    return WorkerResult.from_payload(task.id, payload)


def fail_result(task: Task, summary: str, **kwargs) -> WorkerResult:
    payload = {"status": "failed", "summary": summary, "task_id": task.id, **kwargs}
    return WorkerResult.from_payload(task.id, payload)


def elapsed(started: float | None) -> float:
    return round(time.monotonic() - started, 3) if started else 0.0
