"""MAX orchestration — a small, supervised multi-agent layer.

Optional and off the hot path: when disabled (``MAX_ORCH_ENABLE=0``) MAX's
single-agent text chat behaves exactly as before.

Entry points:
* ``build_supervisor(config, agent)`` — wire the supervisor to an existing MAX agent,
* ``Supervisor.should_delegate`` / ``Supervisor.handle`` — dynamic delegation,
* ``Supervisor.list_tasks/show/diff/cancel`` — Discord task operations.
"""

from __future__ import annotations

from .models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    ResultValidationError,
    Task,
    TaskState,
    TransitionError,
    WorkerResult,
    new_task_id,
)
from .policy import OrchestrationPolicy, ToolDenied, WorkerGrant
from .scheduler import (
    DEFAULT_MAX_WORKERS,
    ExecutionOutcome,
    HARD_MAX_WORKERS,
    SchedulerError,
    TaskScheduler,
)
from .storage import TaskStore
from .supervisor import (
    PlanReport,
    Supervisor,
    classify,
    default_worker_registry,
    should_delegate,
)
from .workers import PROFILES, Worker, WorkerContext, WorkerProfile, WorkerRegistry, function_worker

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_MAX_WORKERS",
    "HARD_MAX_WORKERS",
    "PROFILES",
    "PlanReport",
    "ResultValidationError",
    "SchedulerError",
    "Supervisor",
    "TERMINAL_STATES",
    "Task",
    "TaskScheduler",
    "TaskState",
    "TaskStore",
    "ToolDenied",
    "TransitionError",
    "Worker",
    "WorkerContext",
    "WorkerGrant",
    "WorkerProfile",
    "WorkerRegistry",
    "WorkerResult",
    "build_supervisor",
    "classify",
    "default_worker_registry",
    "function_worker",
    "new_task_id",
    "should_delegate",
]


def build_supervisor(config, agent=None, audit=None, **kwargs) -> Supervisor:
    """Build a supervisor from an existing MAX agent (reusing its policy,
    registry, executor and audit log — never a second permission system)."""
    from ..core.factory import build_agent

    agent = agent or build_agent(config)
    supervisor = Supervisor(
        config,
        agent=agent,
        audit=audit or agent.audit,
        llm=kwargs.pop("llm", None),
        **kwargs,
    )
    if supervisor.llm is None:
        def _llm(prompt: str) -> str:
            try:
                return str(agent.ask(prompt, session_id="orchestrator").text)
            except Exception:  # noqa: BLE001 - provider failure degrades, never crashes
                return ""

        supervisor.llm = _llm
    return supervisor
