"""Worker authorization: profile intent ∩ MAX's real permission policy.

This module deliberately does *not* define a second permission system. It
derives a per-task grant by intersecting:

  1. what the worker profile requests,
  2. what actually exists in the tool registry,
  3. what MAX's ``PermissionPolicy`` (configuration-owned, model-blind) allows,
  4. the task's workspace boundary.

Every execution then goes through the same ``ToolExecutor.dispatch`` used by
the single-agent path, so auditing, output truncation and prompt-injection
flagging behave identically for workers.

Failure mode is always deny: if a grant, tool, argument or workspace cannot be
validated, the call is rejected and audited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.tools import Decision, ToolError, resolve_within
from .models import Task
from .workers import PROFILES


class ToolDenied(Exception):
    """Raised when a worker asks for something the grant does not cover."""


@dataclass
class WorkerGrant:
    task_id: str
    profile: str
    owner_id: int
    tools: tuple[str, ...]
    categories: tuple[str, ...]
    can_write: bool = False
    can_execute: bool = False
    workspace: Path | None = None
    read_roots: tuple[Path, ...] = ()
    reason: str = ""
    used: dict = field(default_factory=dict)  # tool -> count

    def describes(self) -> dict:
        return {
            "task_id": self.task_id,
            "profile": self.profile,
            "tools": list(self.tools),
            "categories": list(self.categories),
            "can_write": self.can_write,
            "can_execute": self.can_execute,
            "workspace": str(self.workspace) if self.workspace else None,
        }


class OrchestrationPolicy:
    """Single authorization boundary for every worker tool invocation."""

    def __init__(
        self,
        base_policy: Any,
        registry: Any,
        executor: Any,
        audit: Any,
        *,
        read_roots: list[Path] | None = None,
        max_calls_per_task: int = 40,
        exec_tools: tuple[str, ...] = ("worker_run",),
        exec_enabled: bool = False,
        memory: Any = None,
        skill_registry: Any = None,
    ) -> None:
        self.base_policy = base_policy
        self.registry = registry
        self.executor = executor
        self.audit = audit
        self.read_roots = tuple(read_roots or ())
        self.max_calls_per_task = int(max_calls_per_task)
        self.memory = memory
        self.skill_registry = skill_registry
        # Guarded runners (allowlisted argv, filtered env, cwd confinement,
        # timeouts) that orchestration may enable *in addition* to the model-facing
        # default-deny on category `exec`. Explicit tool denies always win.
        self.exec_tools = tuple(exec_tools)
        self.exec_enabled = bool(exec_enabled)
        # plan-wide budget shared by every worker in the current plan
        self._plan_budget: int | None = None
        self._plan_used = 0

    def begin_plan(self, budget: int | None) -> None:
        """Reset the shared, plan-wide tool-call budget before scheduling."""
        self._plan_budget = int(budget) if budget else None
        self._plan_used = 0

    # ------------------------------------------------------------- grants
    def grant_for(self, task: Task) -> WorkerGrant:
        profile = PROFILES.get(task.profile)
        if profile is None:
            raise ToolDenied(f"unknown worker profile {task.profile!r}")

        requested = tuple(profile.requested_tools)
        if task.tools:
            # a task may narrow its profile's tool set, never widen it
            requested = tuple(t for t in task.tools if t in requested)

        workspace = None
        if profile.requires_workspace and task.workspace:
            workspace = Path(task.workspace)

        registry_names = set(self.registry.names()) if self.registry is not None else set()
        # A workspace may be created *during* the task (worker_create), so the
        # grant records the capability; check() enforces the workspace itself.
        can_execute = (
            bool(profile.may_execute)
            and self.exec_enabled
            and any(name in registry_names for name in self.exec_tools)
        )

        effective: list[str] = []
        categories: set[str] = set(profile.requested_categories)
        for name in requested:
            spec = self.registry.get(name) if self.registry is not None else None
            if spec is None:
                continue
            decision = self.base_policy.check(spec.name, spec.category)
            if not decision.allowed:
                # the guarded runner is the only category-deny override, and
                # only when orchestration execution is enabled
                if not (spec.name in self.exec_tools and can_execute):
                    continue
                categories.add(spec.category)
            effective.append(spec.name)
            categories.add(spec.category)
        grant = WorkerGrant(
            task_id=task.id,
            profile=task.profile,
            owner_id=task.owner_id,
            tools=tuple(effective),
            categories=tuple(sorted(categories)),
            can_write=bool(profile.may_write),
            can_execute=can_execute,
            workspace=workspace,
            read_roots=self.read_roots,
            reason="derived from profile ∩ registry ∩ permission policy",
        )
        self.audit.record(
            "worker_grant_issued",
            task=task.id,
            profile=task.profile,
            actor=task.owner_id,
            tools=list(grant.tools),
            can_write=grant.can_write,
            can_execute=grant.can_execute,
        )
        return grant

    # ------------------------------------------------------------ execution
    def executor_for(self, grant: WorkerGrant) -> Any:
        """Same ToolExecutor class and policy, scoped to the task workspace.

        Path-taking tools then resolve *inside the task's worktree*, so a coder
        can never write into MAX's own workspace (and vice versa).
        """
        if grant.workspace is None:
            # no workspace means no exec dispatch can reach this point
            # (check() rejects execution without a task worktree)
            return self.executor
        from ..core.tools import ToolContext, ToolExecutor

        context = ToolContext(
            workspace_dir=grant.workspace,
            read_roots=[*self.read_roots, grant.workspace],
            memory=self.memory,
            registry=self.skill_registry,
        )
        return ToolExecutor(
            self.registry, self._policy_for(grant), self.audit, context
        )

    def _policy_for(self, grant: WorkerGrant) -> Any:
        """Base permission policy plus the guarded-runner override for this grant.

        ``ToolExecutor`` re-checks the policy internally, so the override must be
        visible there too; it never widens anything beyond this one tool and this
        one task.
        """
        base = self.base_policy
        override = self

        class _GrantPolicy:
            def check(self, name: str, category: str) -> Decision:
                decision = base.check(name, category)
                if decision.allowed:
                    return decision
                special = override._override(name, grant.can_execute)
                return special if special is not None else decision

        return _GrantPolicy()

    def _override(self, name: str, can_execute: bool):
        if not self.exec_enabled or not can_execute or name not in self.exec_tools:
            return None
        if name in getattr(self.base_policy, "deny", set()):
            return None
        if getattr(self.base_policy, "restrictive", False):
            return None
        return Decision(
            True,
            "guarded runner enabled by MAX_ORCH_EXEC (allowlisted argv, filtered env)",
        )

    def check(self, grant: WorkerGrant, name: str, arguments: dict) -> str:
        """Authorize then execute one worker tool call. Deny by default."""
        arguments = arguments if isinstance(arguments, dict) else {}

        declared = arguments.get("_task")
        if declared is not None and declared != grant.task_id:
            self._deny(grant, name, "task_identity_mismatch")
            raise ToolDenied("task identity mismatch")

        if grant.used.get("_total", 0) >= self.max_calls_per_task:
            self._deny(grant, name, "call_budget_exhausted")
            raise ToolDenied("per-task tool call budget exhausted")
        if self._plan_budget is not None and self._plan_used >= self._plan_budget:
            self._deny(grant, name, "plan_budget_exhausted")
            raise ToolDenied(
                f"plan-wide tool budget exhausted ({self._plan_budget} calls shared by all workers)"
            )
        self._plan_used += 1

        spec = self.registry.get(name) if self.registry is not None else None
        if spec is None:
            self._deny(grant, name, "unknown_tool")
            raise ToolDenied(f"unknown tool '{name}'")

        # 1. the central policy is authoritative and is checked first
        decision = self.base_policy.check(spec.name, spec.category)
        if not decision.allowed:
            if spec.name in self.exec_tools and not grant.can_execute:
                self._deny(grant, name, "execution_disabled")
                raise ToolDenied(
                    "task execution is disabled (enable MAX_ORCH_EXEC for guarded runners)"
                )
            overridden = self._override(spec.name, grant.can_execute)
            if overridden is None:
                self._deny(grant, name, decision.reason)
                raise ToolDenied(decision.reason)
            decision = overridden

        # 2. then the profile's narrowed grant
        if name not in grant.tools and spec.category not in grant.categories:
            self._deny(grant, name, "tool_not_in_grant")
            raise ToolDenied(f"tool '{name}' is not part of this worker's grant")

        if spec.category == "write":
            if not grant.can_write:
                self._deny(grant, name, "profile_may_not_write")
                raise ToolDenied("this worker profile has no write permission")
            # bootstrapping the isolated workspace is the one write allowed
            # before it exists; every other write must land inside it.
            if grant.workspace is None and name != "worker_create":
                self._deny(grant, name, "no_isolated_workspace")
                raise ToolDenied("no isolated workspace: this worker may only write inside its task worktree")
        if name in self.exec_tools:
            if not grant.can_execute:
                self._deny(grant, name, "execution_disabled")
                raise ToolDenied(
                    "task execution is disabled (enable MAX_ORCH_EXEC for guarded runners)"
                )
            if grant.workspace is None:
                self._deny(grant, name, "no_isolated_workspace")
                raise ToolDenied("no isolated workspace: execution needs a task worktree")

        # 3. workspace boundary for path-taking arguments
        if grant.workspace is not None and "path" in arguments:
            try:
                resolve_within(grant.workspace, str(arguments["path"]))
            except ToolError as exc:
                self._deny(grant, name, "path_outside_workspace")
                raise ToolDenied(str(exc)) from exc

        grant.used[name] = grant.used.get(name, 0) + 1
        grant.used["_total"] = grant.used.get("_total", 0) + 1

        self.audit.record(
            "worker_tool_allowed",
            task=grant.task_id,
            profile=grant.profile,
            actor=grant.owner_id,
            tool=name,
            calls=grant.used[name],
        )
        try:
            output, meta = self.executor_for(grant).dispatch(name, arguments)
        except Exception as exc:  # noqa: BLE001 - never propagate a tool crash
            self.audit.record(
                "worker_tool_crashed", task=grant.task_id, tool=name, error=type(exc).__name__
            )
            raise ToolDenied(f"tool failed ({type(exc).__name__})") from exc

        if not meta.get("allowed", False):
            grant.used[name] = grant.used.get(name, 0) - 1
            grant.used["_total"] = grant.used.get("_total", 0) - 1
            self._deny(grant, name, meta.get("reason", "denied_by_policy"))
            raise ToolDenied(meta.get("reason", "denied by policy"))
        return output

    def _deny(self, grant: WorkerGrant, name: str, reason: str) -> None:
        self.audit.record(
            "worker_tool_denied",
            task=grant.task_id,
            profile=grant.profile,
            actor=grant.owner_id,
            tool=name,
            reason=str(reason)[:300],
        )
