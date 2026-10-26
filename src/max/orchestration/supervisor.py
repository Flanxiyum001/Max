"""The MAX supervisor: delegation decisions, planning, execution and aggregation.

MAX stays the only user-facing assistant. This layer is only entered when
``should_delegate`` says a request benefits from parallel specialist work;
everything else stays on the existing single-agent path.

Flow: classify → plan (bounded, explicit dependencies) → authorize → schedule
within configured limits → validate each structured result → detect conflicts
and unverified claims → one consolidated reply + audit events.

A worker result is evidence, never authority: the supervisor checks that
reported tests actually ran, that two workers did not touch the same file, and
that no worker's output tries to inject instructions.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..core.tools import find_injection_markers
from .models import ResultValidationError, Task, TaskState, WorkerResult, new_task_id
from .policy import OrchestrationPolicy, ToolDenied
from .scheduler import DEFAULT_MAX_WORKERS, ExecutionOutcome, SchedulerError, TaskScheduler
from .storage import TaskStore
from .workers import PROFILES, WorkerContext, WorkerRegistry, function_worker

# --------------------------------------------------------------- classification

CODING_SIGNALS = (
    "fix the bug", "fix bug", "failing test", "tests fail", "write code", "implement",
    "refactor", "patch", "add a test", "add tests", "change the code", "edit the",
    "produce a diff", "show me the diff", "bug in", "regression",
)
RESEARCH_SIGNALS = (
    "investigate", "inspect the repo", "inspect this repo", "research", "document",
    "compare", "explain the architecture", "where is", "find the file", "search the repo",
    "read the code", "why does", "root cause",
)
SIMPLE_SIGNALS = ("what is", "who are", "define ", "translate", "hello", "hi ", "thanks")

_DELEGATE_PREFIXES = ("delegate:", "task:")


def classify(text: str) -> tuple[bool, bool]:
    """Return (needs_coding, needs_research) for a request. Deterministic."""
    lowered = (text or "").lower()
    code = any(sig in lowered for sig in CODING_SIGNALS)
    research = any(sig in lowered for sig in RESEARCH_SIGNALS)
    if not code and not research and len(lowered) < 80:
        return False, False
    if not code and not research and any(sig in lowered for sig in SIMPLE_SIGNALS):
        return False, False
    # a bare question without an actionable signal stays single-agent
    if not code and not research:
        return False, False
    return code, research


def should_delegate(text: str, enabled: bool = True) -> bool:
    """Cheap pre-check: never spawn workers for a simple message."""
    if not enabled:
        return False
    stripped = (text or "").strip().lower()
    if stripped.startswith(_DELEGATE_PREFIXES):
        return True
    if len(stripped) < 40:
        return False
    code, research = classify(text)
    return code or research


# ------------------------------------------------------------- built-in workers

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[\"']?[^\s\"']{8,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _llm(ctx: WorkerContext, prompt: str) -> str:
    fn = ctx.extras.get("llm")
    if fn is None:
        return ""
    try:
        return str(fn(prompt))[:4000]
    except Exception:  # noqa: BLE001 - provider failure degrades the worker
        return ""


def researcher_worker(task: Task, ctx: WorkerContext) -> dict:
    """Read-only inspection: structure + targeted search, evidence with paths."""
    scope = task.scope or "."
    evidence: list[str] = []
    try:
        listing = ctx.tool("list_files", {"path": scope})
        evidence.append(f"listing of {scope}: {listing.splitlines()[0][:200] if listing else '(empty)'}")
    except ToolDenied as exc:
        return {"status": "blocked", "summary": f"cannot read repository: {exc}"}

    keywords = [
        w for w in re.findall(r"[a-z_]{4,}", task.description.lower())
        if w not in {"this", "that", "with", "from", "repo", "code", "file"}
    ][:4]
    hits: list[str] = []
    for word in keywords:
        try:
            found = ctx.tool("search_files", {"pattern": rf"\b{re.escape(word)}\b", "limit": 10})
        except ToolDenied:
            continue
        for line in found.splitlines()[:10]:
            if ":" in line:
                hits.append(line.strip()[:300])
        if len(hits) >= 12:
            break
    evidence.extend(hits[:12])

    summary = _llm(
        ctx,
        f"Summarise MAX's repository findings for: {task.description}\n"
        f"Evidence:\n" + "\n".join(evidence),
    )
    if not summary:
        summary = (
            f"Scanned {scope}: {len(hits)} matching line(s) for "
            f"{', '.join(keywords) or 'keywords'}."
        )
    return {
        "status": "completed",
        "summary": summary[:1800],
        "evidence": evidence,
    }


def coder_worker(task: Task, ctx: WorkerContext) -> dict:
    """Isolated edit: create worktree → apply scoped edits → diff → tests → cleanup."""
    if not task.params.get("edits") and not ctx.extras.get("llm"):
        return {
            "status": "blocked",
            "summary": "no scoped edits supplied and no model configured to derive them",
            "next_steps": ["supply task params 'edits' or configure MAX_PROVIDER"],
        }

    try:
        created = ctx.tool("worker_create", {"summary": task.description[:200]})
        info = json.loads(created)
    except ToolDenied as exc:
        return {"status": "blocked", "summary": f"cannot create isolated workspace: {exc}"}
    except ValueError:
        return {"status": "failed", "summary": "workspace creation returned malformed output"}

    worker_task_id = str(info.get("id", ""))
    workspace = Path(str(info.get("worktree", "")))
    task.workspace = str(workspace)
    task.params["worker_task_id"] = worker_task_id
    # scope every subsequent write to the new worktree
    ctx.grant.workspace = workspace
    errors: list[str] = []

    for edit in list(task.params.get("edits") or [])[:8]:
        path, content = str(edit.get("path", "")), str(edit.get("content", ""))
        if not path:
            continue
        try:
            ctx.tool("write_file", {"path": path, "content": content})
        except ToolDenied as exc:
            errors.append(f"write {path}: {exc}")

    test_argv = list(task.params.get("test_argv") or [])
    tests_run: list[dict] = []
    if test_argv:
        if not ctx.grant.can_execute:
            errors.append("execution disabled (MAX_ORCH_EXEC=0)")
            tests_run.append({"name": " ".join(test_argv)[:120], "outcome": "blocked", "detail": errors[-1]})
        else:
            try:
                out = json.loads(
                    ctx.tool("worker_run", {"task_id": worker_task_id, "argv": test_argv, "timeout_s": 60})
                )
                tests_run.append(
                    {
                        "name": " ".join(test_argv)[:120],
                        "outcome": "passed" if out.get("exit_code") == 0 else "failed",
                        "detail": f"exit={out.get('exit_code')} timed_out={out.get('timed_out')}",
                    }
                )
            except ToolDenied as exc:
                errors.append(f"test run denied: {exc}")
                tests_run.append({"name": " ".join(test_argv)[:120], "outcome": "blocked", "detail": str(exc)})
            except ValueError:
                errors.append("test runner returned malformed output")
                tests_run.append({"name": " ".join(test_argv)[:120], "outcome": "blocked", "detail": "malformed"})

    changed: list[str] = []
    diff = ""
    try:
        diff_info = json.loads(ctx.tool("worker_diff", {"task_id": worker_task_id}))
        diff = str(diff_info.get("diff", ""))[:20000]
        stat = str(diff_info.get("stat", ""))
        changed = [line.split("|")[0].strip() for line in stat.splitlines() if "|" in line]
        for finding in diff_info.get("secret_findings") or []:
            errors.append(f"secret-shaped content detected in diff: {finding}")
    except (ToolDenied, ValueError) as exc:
        errors.append(f"diff unavailable: {exc}")

    if not ctx.extras.get("keep_workspace"):
        # The workspace is cleaned up by the supervisor *after* dependent
        # tasks (tester, reviewer) have finished — never here.
        task.params["cleanup"] = "supervisor"

    return {
        "status": "failed" if (errors and not changed and not tests_run) else "completed",
        "summary": (
            f"changed {len(changed)} file(s) in an isolated worktree"
            + (f"; {len(tests_run)} test command(s) run" if tests_run else "")
        ),
        "changed_files": changed,
        "tests_run": tests_run,
        "errors": errors,
        "diff": diff,
        "evidence": [f"worktree branch {info.get('branch', '-')}"],
    }


def tester_worker(task: Task, ctx: WorkerContext) -> dict:
    """Run the approved test command in the coder's workspace and report reality."""
    worker_task_id = task.params.get("worker_task_id")
    test_argv = list(task.params.get("test_argv") or [])
    if not worker_task_id or not test_argv:
        return {
            "status": "blocked",
            "summary": "no workspace or test command available for this task",
            "errors": ["tester depends on a coder task with a test command"],
        }
    if not ctx.grant.can_execute:
        return {
            "status": "blocked",
            "summary": "test execution is disabled (set MAX_ORCH_EXEC=1)",
            "errors": ["execution disabled by configuration"],
        }
    try:
        out = json.loads(
            ctx.tool("worker_run", {"task_id": str(worker_task_id), "argv": test_argv, "timeout_s": 120})
        )
    except ToolDenied as exc:
        return {"status": "blocked", "summary": f"test run denied: {exc}", "errors": [str(exc)]}
    except ValueError:
        return {"status": "failed", "summary": "test runner returned malformed output"}

    passed = out.get("exit_code") == 0 and not out.get("timed_out")
    return {
        "status": "completed",
        "summary": (
            f"test command {'passed' if passed else 'failed'} (exit={out.get('exit_code')})"
        ),
        "tests_run": [
            {
                "name": " ".join(test_argv)[:120],
                "outcome": "passed" if passed else "failed",
                "detail": f"exit={out.get('exit_code')} duration={out.get('duration_s')}s",
            }
        ],
        "evidence": [str(out.get("stdout", ""))[-400:] or "(no stdout)"],
    }


def reviewer_worker(task: Task, ctx: WorkerContext) -> dict:
    """Independent read-only review of the proposed change and its evidence."""
    diff = str(task.params.get("diff") or "")
    changed = list(task.params.get("changed_files") or [])
    tests = list(task.params.get("tests_run") or [])
    risks: list[str] = []

    if not diff and not changed:
        return {
            "status": "blocked",
            "summary": "no proposed diff available to review",
            "errors": ["reviewer received no change set"],
        }
    if _SECRET_RE.search(diff):
        risks.append("diff contains secret-shaped content — must not be merged")
    if not tests:
        risks.append("no test evidence accompanied this change")
    elif any(t.get("outcome") in {"failed", "error", "blocked"} for t in tests):
        risks.append("reported tests did not all pass")
    for name in changed:
        if name.startswith(("..", "/")) or ".." in Path(name).parts:
            risks.append(f"changed file outside the workspace: {name}")
    for marker in find_injection_markers(diff):
        risks.append(f"possible prompt injection marker in diff: {marker[:60]}")

    verdict = "changes requested" if risks else "approve"
    return {
        "status": "completed",
        "summary": f"{verdict} — {len(changed)} file(s) changed, {len(tests)} test run(s)",
        "risks": risks,
        "evidence": [f"reviewed diff of {len(changed)} file(s)"],
    }


def default_worker_registry() -> WorkerRegistry:
    registry = WorkerRegistry()
    registry.register(function_worker("researcher", "researcher", researcher_worker))
    registry.register(function_worker("coder", "coder", coder_worker))
    registry.register(function_worker("tester", "tester", tester_worker))
    registry.register(function_worker("reviewer", "reviewer", reviewer_worker))
    return registry


# ------------------------------------------------------------------ supervisor


@dataclass
class PlanReport:
    tasks: list[Task] = field(default_factory=list)
    outcomes: list[ExecutionOutcome] = field(default_factory=list)
    started_at: float = 0.0
    duration_s: float = 0.0
    conflicts: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return sum(1 for t in self.tasks if t.state == TaskState.COMPLETED)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tasks if t.state == TaskState.FAILED)

    @property
    def cancelled(self) -> int:
        return sum(1 for t in self.tasks if t.state == TaskState.CANCELLED)


class Supervisor:
    def __init__(
        self,
        config: Config,
        agent=None,
        base_policy=None,
        registry=None,
        executor=None,
        audit=None,
        worker_registry: WorkerRegistry | None = None,
        store: TaskStore | None = None,
        scheduler: TaskScheduler | None = None,
        llm: Callable[[str], str] | None = None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.audit = audit
        self.registry = registry if registry is not None else (agent.registry if agent else None)
        self.base_policy = base_policy if base_policy is not None else (agent.policy if agent else None)
        self.executor = executor if executor is not None else (agent.executor if agent else None)
        self.workers = worker_registry or default_worker_registry()
        self.store = store or TaskStore(config.data_dir / "tasks")
        self.scheduler = scheduler or TaskScheduler(
            max_concurrency=config.orch_max_workers,
            max_depth=config.orch_depth,
            default_timeout_s=config.orch_task_timeout_s,
        )
        self.policy = OrchestrationPolicy(
            self.base_policy,
            self.registry,
            self.executor,
            audit,
            exec_enabled=config.orch_exec,
            read_roots=[p.resolve() for p in config.skills_dirs] + [config.repo_root],
            memory=getattr(agent, "memory", None),
            skill_registry=getattr(agent, "skill_registry", None),
        )
        self.llm = llm
        self.keep_workspaces = config.orch_keep_workspaces
        self._active: dict[str, Task] = {}

    # ------------------------------------------------------------ decisions
    def enabled(self) -> bool:
        return bool(self.config.orch_enabled)

    def should_delegate(self, text: str) -> bool:
        return should_delegate(text, enabled=self.enabled() and self.config.orch_auto)

    # --------------------------------------------------------------- planning
    def plan(
        self,
        text: str,
        owner_id: int,
        session_id: str = "",
        guild_id: int | None = None,
        channel_id: int | None = None,
        forced: bool = False,
    ) -> list[Task]:
        if not forced and not self.should_delegate(text):
            return []
        code, research = classify(text)
        if not code and not research and forced:
            code = True

        now = time.time()
        tasks: list[Task] = []
        common = dict(
            owner_id=owner_id,
            session_id=session_id,
            guild_id=guild_id,
            channel_id=channel_id,
            timeout_s=self.config.orch_task_timeout_s,
            max_attempts=1,
        )

        researcher = coder = tester = reviewer = None
        if research:
            researcher = Task(
                id=new_task_id(),
                description=text[:500],
                profile="researcher",
                scope=".",
                created_at=now,
                updated_at=now,
                **common,
            )
            tasks.append(researcher)
        if code:
            coder = Task(
                id=new_task_id(),
                description=text[:500],
                profile="coder",
                scope=".",
                created_at=now,
                updated_at=now,
                params=self._coder_params(text),
                # researcher and coder are independent here: the edit directive is
                # supplied with the request, so both may run concurrently.
                depends_on=[],
                **common,
            )
            tester = Task(
                id=new_task_id(),
                description=f"verify: {text[:400]}",
                profile="tester",
                scope=".",
                created_at=now,
                updated_at=now,
                depends_on=[coder.id],
                **common,
            )
            reviewer = Task(
                id=new_task_id(),
                description=f"review: {text[:400]}",
                profile="reviewer",
                scope=".",
                created_at=now,
                updated_at=now,
                depends_on=[coder.id, tester.id],
                **common,
            )
            tasks.extend([coder, tester, reviewer])

        # conservative bound: never more than the documented plan size
        if len(tasks) > 4:
            tasks = tasks[:4]
        return tasks

    def _coder_params(self, text: str) -> dict:
        """Extract an explicit, operator-supplied edit directive if present.

        Format (documented in docs/ARCHITECTURE.md): a fenced ``max:edit`` block
        with JSON ``{"edits": [...], "test_argv": [...]}``. Without it the coder
        can only propose changes when a model is configured — MAX never invents
        an edit it was not given or able to derive from a configured provider.
        """
        params: dict = {}
        match = re.search(r"max:edit\s*(\{.*\})", text, re.DOTALL)
        if not match:
            return params
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            return params
        if isinstance(payload, dict):
            if isinstance(payload.get("edits"), list):
                params["edits"] = payload["edits"][:8]
            if isinstance(payload.get("test_argv"), list):
                params["test_argv"] = [str(v) for v in payload["test_argv"][:6]]
        return params

    # -------------------------------------------------------------- execution
    def execute(self, tasks: list[Task]) -> PlanReport:
        report = PlanReport(tasks=tasks, started_at=time.monotonic())
        if not tasks:
            return report
        for task in tasks:
            self._active[task.id] = task
            self.store.save(task)
        # plan-wide tool budget shared by every worker in this plan
        self.policy.begin_plan(self.config.orch_tool_budget)
        try:
            report.outcomes = self.scheduler.run(tasks, self._run_task)
        except SchedulerError as exc:
            for task in tasks:
                if not task.is_terminal:
                    task.transition(TaskState.FAILED, str(exc))
                    task.failure = str(exc)
            report.flags.append(str(exc))
        finally:
            report.duration_s = round(time.monotonic() - report.started_at, 3)
            for task in tasks:
                self.store.save(task)
        report.conflicts, report.flags = self._verify(tasks, report.flags)
        self._cleanup(tasks)
        for task in tasks:
            self.store.save(task)
        self.audit_event(
            "orchestrator_plan_finished",
            tasks=len(tasks),
            completed=report.completed,
            failed=report.failed,
            cancelled=report.cancelled,
            conflicts=len(report.conflicts),
            duration_s=report.duration_s,
        )
        return report

    def _cleanup(self, tasks: list[Task]) -> None:
        """Explicit workspace lifecycle: discard isolated worktrees once every
        dependent task is terminal, unless MAX_ORCH_KEEP_WORKSPACES=1."""
        if self.keep_workspaces:
            return
        seen: set[str] = set()
        for task in tasks:
            worker_task_id = task.params.get("worker_task_id")
            if not worker_task_id or worker_task_id in seen:
                continue
            seen.add(str(worker_task_id))
            try:
                grant = self.policy.grant_for(task)
                self.policy.check(grant, "worker_discard", {"task_id": worker_task_id})
            except Exception:  # noqa: BLE001 - cleanup is best effort, never fatal
                self.audit_event("orchestrator_cleanup_failed", task=task.id)
            else:
                task.params.pop("worker_task_id", None)
                self.audit_event("orchestrator_workspace_discarded", task=task.id)

    def _run_task(self, task: Task) -> WorkerResult:
        worker = self.workers.for_profile(task.profile)
        if worker is None:
            task.failure = f"no worker registered for profile {task.profile}"
            return WorkerResult.from_payload(
                task.id, {"status": "failed", "summary": task.failure}
            )
        # inherit workspace/worker-task identity and evidence from dependencies
        # (before the grant is derived: the grant's scope depends on the workspace)
        for dep_id in task.depends_on:
            dep = self._active.get(dep_id)
            if dep is None:
                continue
            if dep.workspace and not task.workspace:
                task.workspace = dep.workspace
            if dep.params.get("worker_task_id"):
                task.params.setdefault("worker_task_id", dep.params["worker_task_id"])
            if dep.params.get("test_argv"):
                task.params.setdefault("test_argv", dep.params["test_argv"])
            if dep.result is None or task.profile != "reviewer":
                continue
            if not task.params.get("diff") and dep.result.diff:
                task.params["diff"] = dep.result.diff
            for path in dep.result.changed_files:
                if path not in task.params.setdefault("changed_files", []):
                    task.params["changed_files"].append(path)
            task.params.setdefault("tests_run", []).extend(dep.result.tests_run)

        try:
            grant = self.policy.grant_for(task)
        except ToolDenied as exc:
            task.failure = str(exc)
            return WorkerResult.from_payload(
                task.id, {"status": "failed", "summary": f"authorization failed: {exc}"}
            )

        ctx = WorkerContext(
            task=task,
            grant=grant,
            audit=self.audit,
            extras={
                "tool_runner": self._tool_runner(),
                "llm": self.llm,
                "keep_workspace": self.keep_workspaces,
            },
        )
        started = time.monotonic()
        try:
            payload = worker.fn(task, ctx)
        except ToolDenied as exc:
            task.failure = str(exc)
            payload = {"status": "failed", "summary": f"authorization failed: {exc}"}
        except Exception as exc:  # noqa: BLE001 - a worker crash must not kill the plan
            task.failure = f"{type(exc).__name__}: {exc}"[:300]
            payload = {"status": "failed", "summary": task.failure}

        duration = round(time.monotonic() - started, 3)
        task.resources["worker_duration_s"] = duration
        task.resources["calls"] = grant.used.get("_total", 0)

        if isinstance(payload, WorkerResult):
            result = payload
            if result.task_id != task.id:
                result = WorkerResult.from_payload(
                    task.id, {"status": "failed", "summary": "worker returned a foreign task id"}
                )
        else:
            try:
                result = WorkerResult.from_payload(task.id, payload)
            except ResultValidationError as exc:
                task.failure = f"malformed worker result: {exc}"
                result = WorkerResult.from_payload(
                    task.id,
                    {"status": "failed", "summary": f"malformed worker result: {exc}"[:400]},
                )
        self.audit_event(
            "worker_task_finished",
            task=task.id,
            profile=task.profile,
            actor=task.owner_id,
            status=result.status,
            duration_s=duration,
            calls=task.resources.get("calls", 0),
        )
        return result

    def _tool_runner(self):
        def runner(grant, name, arguments):
            return self.policy.check(grant, name, arguments)

        return runner

    # ---------------------------------------------------------- verification
    def _verify(self, tasks: list[Task], flags: list[str]) -> tuple[list[str], list[str]]:
        conflicts: list[str] = []
        owners: dict[str, list[str]] = {}
        for task in tasks:
            if task.result is None:
                continue
            for path in task.result.changed_files:
                if task.profile not in owners.setdefault(path, []):
                    owners[path].append(task.profile)
        for path, profiles in owners.items():
            if len(profiles) > 1:
                conflicts.append(f"{path}: modified by {', '.join(profiles)}")

        for task in tasks:
            if task.result is None:
                continue
            for marker in find_injection_markers(
                task.result.summary + "\n" + "\n".join(task.result.evidence)
            ):
                flags.append(f"{task.profile}: untrusted content flagged ({marker[:40]})")

        # claimed tests must have actually run somewhere in the plan
        if any(t.profile == "coder" for t in tasks):
            ran = any(
                t.result is not None and t.result.tests_reported for t in tasks
            )
            tested = any(t.profile == "tester" for t in tasks)
            if tested and not ran:
                flags.append("no test evidence: tester reported no executed tests")
        return conflicts, flags

    def audit_event(self, event: str, **detail) -> None:
        if self.audit is not None:
            self.audit.record(event, **detail)

    # ------------------------------------------------------------------ API
    def handle(
        self,
        text: str,
        owner_id: int,
        session_id: str = "",
        guild_id: int | None = None,
        channel_id: int | None = None,
    ) -> str | None:
        """Entry point from the Discord conversation path.

        Returns None when MAX should stay on the normal single-agent path.
        """
        if not self.should_delegate(text):
            return None
        tasks = self.plan(text, owner_id, session_id, guild_id, channel_id)
        if not tasks:
            return None
        self.audit_event(
            "orchestrator_plan_created",
            actor=owner_id,
            session=session_id,
            profiles=[t.profile for t in tasks],
        )
        report = self.execute(tasks)
        return self.render(report)

    # ------------------------------------------------------------- rendering
    def render(self, report: PlanReport) -> str:
        lines = [
            f"Delegated plan: {len(report.tasks)} task(s) in {report.duration_s}s — "
            f"{report.completed} completed, {report.failed} failed, {report.cancelled} cancelled"
        ]
        for task in report.tasks:
            state = task.state.value
            body = (task.result.summary if task.result else (task.failure or task.progress)) or "-"
            lines.append(f"• {task.profile} [{state}] {body[:300]}")
            if task.result:
                if task.result.changed_files:
                    lines.append(f"    files: {', '.join(task.result.changed_files[:8])}")
                for test in task.result.tests_run[:4]:
                    lines.append(f"    test: {test['name']} → {test['outcome']}")
                for risk in task.result.risks[:3]:
                    lines.append(f"    risk: {risk[:200]}")
        if report.conflicts:
            lines.append("Conflicts: " + "; ".join(report.conflicts[:5]))
        if report.flags:
            lines.append("Flags: " + "; ".join(sorted(set(report.flags))[:5]))
        lines.append("Nothing was pushed, merged or deployed; approval is required for that.")
        return "\n".join(lines)

    # ------------------------------------------------------- task operations
    def run_command(
        self,
        name: str,
        args: str,
        owner_id: int,
        *,
        is_admin: bool = False,
        session_id: str = "",
        guild_id: int | None = None,
        channel_id: int | None = None,
    ) -> list[str]:
        """Discord-facing task operations. Ownership is checked on every call."""
        if not self.enabled():
            return ["orchestration is disabled (MAX_ORCH_ENABLE=0)"]
        if name == "tasks":
            return self.list_tasks(owner_id, is_admin)

        parts = (args or "").split()
        action = parts[0].lower() if parts else ""
        rest = " ".join(parts[1:])

        if action == "run":
            if not rest:
                return ["usage: !max task run <description>"]
            tasks = self.plan(
                rest,
                owner_id,
                session_id=session_id,
                guild_id=guild_id,
                channel_id=channel_id,
                forced=True,
            )
            if not tasks:
                return ["could not build a bounded task plan for that request"]
            self.audit_event(
                "orchestrator_plan_created",
                actor=owner_id,
                session=session_id,
                profiles=[t.profile for t in tasks],
                source="command",
            )
            return [self.render(self.execute(tasks))]
        if action in {"cancel", "discard"}:
            if not rest:
                return ["usage: !max task cancel <id>"]
            return self.cancel(rest.split()[0], owner_id, is_admin)
        if action == "diff":
            if not rest:
                return ["usage: !max task diff <id>"]
            return self.diff(rest.split()[0], owner_id, is_admin)
        if action in {"show", "status"}:
            if not rest:
                return ["usage: !max task show <id>"]
            return self.show(rest.split()[0], owner_id, is_admin)
        if action.startswith("t_") and not rest:
            return self.show(action, owner_id, is_admin)
        if not action:
            return self.list_tasks(owner_id, is_admin)
        return [f"unknown task action '{action}' — try: run | show <id> | diff <id> | cancel <id>"]

    def _get(self, task_id: str, requester_id: int, is_admin: bool) -> Task | None:
        task = self.store.load(task_id.strip())
        if task is None:
            return None
        if task.owner_id != requester_id and not is_admin:
            self.audit_event(
                "orchestrator_access_denied",
                task=task.id,
                actor=requester_id,
                reason="not_owner",
            )
            return None
        return task

    def list_tasks(self, requester_id: int, is_admin: bool = False) -> list[str]:
        tasks = self.store.list() if is_admin else self.store.list_for_owner(requester_id)
        if not tasks:
            return ["no tasks yet — start one with `!max task run <description>`"]
        lines = [f"{t.id} | {t.profile} | {t.state.value} | owner={t.owner_id} | {t.description[:60]}" for t in tasks[:15]]
        scope = "all visible" if is_admin else "yours only"
        return [f"Tasks ({scope}, {len(tasks)}):\n" + "\n".join(lines)]

    def show(self, task_id: str, requester_id: int, is_admin: bool = False) -> list[str]:
        task = self._get(task_id, requester_id, is_admin)
        if task is None:
            return ["task not found or not visible to you"]
        data = task.to_dict()
        data.pop("result", None)
        result = task.result.to_dict() if task.result else None
        if result:
            result["diff"] = f"<{len(result['diff'])} chars — use `!max task diff {task.id}`>"
        return [json.dumps({"task": data, "result": result}, indent=2, default=str)[:3500]]

    def diff(self, task_id: str, requester_id: int, is_admin: bool = False) -> list[str]:
        task = self._get(task_id, requester_id, is_admin)
        if task is None:
            return ["task not found or not visible to you"]
        if task.result is None or not task.result.diff:
            return [f"no diff recorded for {task.id}"]
        body = task.result.diff[:3500]
        return [f"diff for {task.id}:\n```diff\n{body}\n```"]

    def cancel(self, task_id: str, requester_id: int, is_admin: bool = False) -> list[str]:
        task = self._get(task_id, requester_id, is_admin)
        if task is None:
            return ["task not found or not visible to you"]
        if task.is_terminal:
            return [f"{task.id} is already {task.state.value}"]
        if TaskScheduler.cancel(task, requester_id):
            self.store.save(task)
            self.audit_event(
                "orchestrator_task_cancelled", task=task.id, actor=requester_id, owner=task.owner_id
            )
            return [f"{task.id} cancelled"]
        return [f"{task.id} could not be cancelled (state {task.state.value})"]
