"""Task and worker data model for MAX's supervisor layer.

The supervisor is a *bounded* extension of MAX's existing single-agent loop:
a task is a small, validated record with an explicit state machine. Nothing
here grants permission — authorisation lives in ``max.core.tools`` and is
re-checked by ``max.orchestration.policy`` for every worker tool call.

Invariants:
* state transitions are explicit and validated (no silent jumps),
* every worker result is validated against a schema before MAX reports it,
* results are bounded in size so a worker cannot flood Discord or the audit
  log with output.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ------------------------------------------------------------------ states


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"  # blocked on a dependency or an approval
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset(
        {TaskState.RUNNING, TaskState.WAITING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.WAITING: frozenset(
        {TaskState.QUEUED, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.WAITING,
        }
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class TransitionError(Exception):
    """Raised when a task state change is not permitted."""


class ResultValidationError(Exception):
    """Raised when a worker returns a malformed structured result."""


def new_task_id() -> str:
    return "t_" + uuid.uuid4().hex[:12]


# ------------------------------------------------------------------ results

_SUMMARY_LIMIT = 2000
_LIST_LIMIT = 12
_ITEM_LIMIT = 400
_TESTS_LIMIT = 24
_TEST_OUTCOMES = frozenset({"passed", "failed", "skipped", "error", "blocked"})
_RESULT_STATUSES = frozenset({"completed", "failed", "blocked"})


def _bounded_str(value: Any, limit: int, field_name: str, errors: list[str]) -> str:
    if not isinstance(value, str):
        errors.append(f"{field_name} must be a string")
        return ""
    if len(value) > limit:
        errors.append(f"{field_name} exceeds {limit} chars")
        return value[:limit]
    return value


def _bounded_list(value: Any, limit: int, field_name: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list")
        return []
    if len(value) > limit:
        errors.append(f"{field_name} has more than {limit} items")
        value = value[:limit]
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            errors.append(f"{field_name} contains a non-string item")
            continue
        out.append(item[:_ITEM_LIMIT])
    return out


@dataclass
class WorkerResult:
    """Structured, bounded result every worker must return."""

    task_id: str
    status: str  # completed | failed | blocked
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    tests_run: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    diff: str = ""
    # True only when a *separate* verification step inspected this result.
    verified: bool = False

    # ------------------------------------------------------------ validation
    @classmethod
    def from_payload(cls, task_id: str, payload: Any) -> "WorkerResult":
        """Validate a worker payload; raise ResultValidationError on problems."""
        errors: list[str] = []
        if not isinstance(payload, dict):
            raise ResultValidationError(
                f"worker result must be an object, got {type(payload).__name__}"
            )

        status = payload.get("status")
        if status not in _RESULT_STATUSES:
            errors.append(f"status must be one of {sorted(_RESULT_STATUSES)}")

        declared_task = payload.get("task_id")
        if declared_task is not None and declared_task != task_id:
            errors.append("task_id does not match the assigned task")

        result = cls(
            task_id=task_id,
            status=str(status) if status in _RESULT_STATUSES else "failed",
            summary=_bounded_str(payload.get("summary", ""), _SUMMARY_LIMIT, "summary", errors),
            evidence=_bounded_list(payload.get("evidence"), _LIST_LIMIT, "evidence", errors),
            changed_files=_bounded_list(
                payload.get("changed_files"), _LIST_LIMIT, "changed_files", errors
            ),
            errors=_bounded_list(payload.get("errors"), _LIST_LIMIT, "errors", errors),
            risks=_bounded_list(payload.get("risks"), _LIST_LIMIT, "risks", errors),
            next_steps=_bounded_list(payload.get("next_steps"), _LIST_LIMIT, "next_steps", errors),
            diff=_bounded_str(payload.get("diff", ""), 20000, "diff", errors),
        )

        tests = payload.get("tests_run") or []
        if not isinstance(tests, list):
            errors.append("tests_run must be a list")
            tests = []
        if len(tests) > _TESTS_LIMIT:
            errors.append(f"tests_run has more than {_TESTS_LIMIT} items")
            tests = tests[:_TESTS_LIMIT]
        for entry in tests:
            if not isinstance(entry, dict):
                errors.append("tests_run entries must be objects")
                continue
            name = entry.get("name")
            outcome = entry.get("outcome")
            if not isinstance(name, str) or not name:
                errors.append("tests_run entry missing name")
            if outcome not in _TEST_OUTCOMES:
                errors.append(f"tests_run outcome must be one of {sorted(_TEST_OUTCOMES)}")
            result.tests_run.append(
                {
                    "name": str(name)[:200],
                    "outcome": str(outcome),
                    "detail": str(entry.get("detail", ""))[:400],
                }
            )

        if errors:
            raise ResultValidationError("; ".join(errors))
        return result

    @property
    def tests_failed(self) -> bool:
        return any(t["outcome"] in {"failed", "error"} for t in self.tests_run)

    @property
    def tests_reported(self) -> bool:
        return any(t["outcome"] != "blocked" for t in self.tests_run)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "evidence": self.evidence,
            "changed_files": self.changed_files,
            "tests_run": self.tests_run,
            "errors": self.errors,
            "risks": self.risks,
            "next_steps": self.next_steps,
            "diff": self.diff,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerResult":
        return cls(
            task_id=str(data.get("task_id", "")),
            status=str(data.get("status", "failed")),
            summary=str(data.get("summary", "")),
            evidence=list(data.get("evidence") or []),
            changed_files=list(data.get("changed_files") or []),
            tests_run=list(data.get("tests_run") or []),
            errors=list(data.get("errors") or []),
            risks=list(data.get("risks") or []),
            next_steps=list(data.get("next_steps") or []),
            diff=str(data.get("diff", "")),
            verified=bool(data.get("verified", False)),
        )


# -------------------------------------------------------------------- tasks


@dataclass
class Task:
    id: str
    description: str
    profile: str
    owner_id: int
    session_id: str = ""
    guild_id: int | None = None
    channel_id: int | None = None
    parent_id: str | None = None
    scope: str = ""
    workspace: str = ""
    tools: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    state: TaskState = TaskState.QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    max_attempts: int = 1
    timeout_s: float = 120.0
    progress: str = ""
    result: WorkerResult | None = None
    failure: str = ""
    cancelled_by: int | None = None
    approval: str = "not_required"  # not_required | pending | granted | denied
    resources: dict = field(default_factory=dict)
    # worker inputs (edit directives, inherited workspace ids, test argv)
    params: dict = field(default_factory=dict)

    # ---------------------------------------------------------- transitions
    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition(self, new_state: TaskState, reason: str = "") -> None:
        if not isinstance(new_state, TaskState):
            raise TransitionError(f"unknown state {new_state!r}")
        allowed = ALLOWED_TRANSITIONS[self.state]
        if new_state not in allowed:
            raise TransitionError(
                f"illegal transition {self.state.value} -> {new_state.value}"
            )
        self.state = new_state
        self.updated_at = time.time()
        if new_state == TaskState.RUNNING and self.started_at is None:
            self.started_at = self.updated_at
        if new_state in TERMINAL_STATES:
            self.finished_at = self.updated_at
        if reason:
            self.progress = reason[:400]

    def cancel(self, requester_id: int) -> None:
        self.cancelled_by = requester_id
        self.transition(TaskState.CANCELLED, "cancelled by operator")

    # --------------------------------------------------------------- serial
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "profile": self.profile,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "parent_id": self.parent_id,
            "scope": self.scope,
            "workspace": self.workspace,
            "tools": self.tools,
            "depends_on": self.depends_on,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "timeout_s": self.timeout_s,
            "progress": self.progress,
            "result": self.result.to_dict() if self.result else None,
            "failure": self.failure,
            "cancelled_by": self.cancelled_by,
            "approval": self.approval,
            "resources": self.resources,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        task = cls(
            id=str(data.get("id") or new_task_id()),
            description=str(data.get("description", "")),
            profile=str(data.get("profile", "researcher")),
            owner_id=int(data.get("owner_id", 0)),
            session_id=str(data.get("session_id", "")),
            guild_id=data.get("guild_id"),
            channel_id=data.get("channel_id"),
            parent_id=data.get("parent_id"),
            scope=str(data.get("scope", "")),
            workspace=str(data.get("workspace", "")),
            tools=list(data.get("tools") or []),
            depends_on=list(data.get("depends_on") or []),
            state=TaskState(data.get("state", "queued")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 1)),
            timeout_s=float(data.get("timeout_s", 120.0)),
            progress=str(data.get("progress", "")),
            failure=str(data.get("failure", "")),
            cancelled_by=data.get("cancelled_by"),
            approval=str(data.get("approval", "not_required")),
            resources=dict(data.get("resources") or {}),
            params=dict(data.get("params") or {}),
        )
        result = data.get("result")
        if isinstance(result, dict):
            task.result = WorkerResult.from_dict(result)
        return task
