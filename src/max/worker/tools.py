"""Coding-worker tools exposed through MAX's existing tool registry.

All of these sit behind the standard policy/permission boundary:
``worker_run`` is category ``exec`` (denied by default), the rest are
read/write/meta. Nothing here accepts a shell string.
"""

from __future__ import annotations

import json

from ..core.tools import ToolContext, ToolError, ToolRegistry, ToolSpec


def _as_argv(value) -> list[str]:
    if isinstance(value, str):
        raise ToolError("argv must be an array of strings, not a shell string")
    if not isinstance(value, list) or not value:
        raise ToolError("argv must be a non-empty array of strings")
    if not all(isinstance(a, str) for a in value):
        raise ToolError("argv entries must be strings")
    return list(value)


def _fmt(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


def register_worker_tools(registry: ToolRegistry, manager) -> None:
    """Register the coding-worker tool surface against a TaskManager."""

    def create(args: dict, ctx: ToolContext) -> str:
        task = manager.create(summary=args.get("summary", ""), repo=args.get("repo"))
        return _fmt(task.public())

    def run(args: dict, ctx: ToolContext) -> str:
        result = manager.run(
            args.get("task_id", ""),
            _as_argv(args.get("argv")),
            cwd=args.get("cwd"),
            timeout_s=args.get("timeout_s"),
        )
        return _fmt(result)

    def diff(args: dict, ctx: ToolContext) -> str:
        return _fmt(manager.diff(args.get("task_id", "")))

    def status(args: dict, ctx: ToolContext) -> str:
        return _fmt(manager.status(args.get("task_id", "")))

    def cancel(args: dict, ctx: ToolContext) -> str:
        return _fmt(manager.cancel(args.get("task_id", "")))

    def discard(args: dict, ctx: ToolContext) -> str:
        return _fmt(manager.discard(args.get("task_id", "")))

    def request_approval(args: dict, ctx: ToolContext) -> str:
        return _fmt(
            manager.request_approval(
                args.get("task_id", ""), args.get("action", ""), args.get("reason", "")
            )
        )

    common = {
        "task_id": {"type": "string", "description": "task id returned by worker_create"},
    }

    registry.register(
        ToolSpec(
            name="worker_create",
            description=(
                "Create an isolated coding task: a dedicated git worktree and branch for an "
                "allowlisted repository. Returns the task id and workspace path."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "what this task should accomplish"},
                    "repo": {"type": "string", "description": "allowlisted repo path (default: first allowlisted repo)"},
                },
                "required": ["summary"],
            },
            category="write",
            handler=create,
        )
    )
    registry.register(
        ToolSpec(
            name="worker_run",
            description=(
                "Run an approved command inside the task workspace. Only allowlisted argv "
                "prefixes are accepted (test/lint/inspection commands); shell strings, inline "
                "python, network tools and git write operations are rejected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **common,
                    "argv": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"python\",\"-m\",\"pytest\",\"-q\"]"},
                    "cwd": {"type": "string", "description": "relative to the task workspace"},
                    "timeout_s": {"type": "number"},
                },
                "required": ["task_id", "argv"],
            },
            category="exec",
            handler=run,
        )
    )
    registry.register(
        ToolSpec(
            name="worker_diff",
            description="Show the task's diff, changed files and any secret-shaped content findings.",
            parameters={"type": "object", "properties": common, "required": ["task_id"]},
            category="read",
            handler=diff,
        )
    )
    registry.register(
        ToolSpec(
            name="worker_status",
            description="Show task state, branch and changed files.",
            parameters={"type": "object", "properties": common, "required": ["task_id"]},
            category="read",
            handler=status,
        )
    )
    registry.register(
        ToolSpec(
            name="worker_cancel",
            description="Cancel a task (worktree is kept for inspection until discarded).",
            parameters={"type": "object", "properties": common, "required": ["task_id"]},
            category="meta",
            handler=cancel,
        )
    )
    registry.register(
        ToolSpec(
            name="worker_discard",
            description="Remove the task's worktree and branch. Irreversible for uncommitted work.",
            parameters={"type": "object", "properties": common, "required": ["task_id"]},
            category="write",
            handler=discard,
        )
    )
    registry.register(
        ToolSpec(
            name="worker_request_approval",
            description=(
                "Request human approval for a proposed follow-up action (push, PR, merge, "
                "production change). Granting approval is never possible from tool output."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **common,
                    "action": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["task_id", "action"],
            },
            category="meta",
            handler=request_approval,
        )
    )
