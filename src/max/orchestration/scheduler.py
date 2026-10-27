"""Bounded, dependency-aware task scheduler.

Design constraints (from the brief and from the 8 GB target host):
* a hard ceiling on concurrently running workers (default 2, hard cap 3),
* dependencies are respected — a task never starts before its prerequisites
  reach a terminal state,
* per-task timeout, cancellation and bounded retries,
* workers run in threads; a timed-out worker is abandoned by the scheduler and
  its task is failed, so unbounded work cannot accumulate silently.

The scheduler never decides *what* to run — that is the supervisor's job — and
never grants permissions.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable

from .models import TERMINAL_STATES, Task, TaskState, TransitionError

DEFAULT_MAX_WORKERS = 2
HARD_MAX_WORKERS = 3
DEFAULT_DEPTH = 1


class SchedulerError(Exception):
    pass


@dataclass
class ExecutionOutcome:
    task: Task
    ok: bool
    reason: str = ""
    duration_s: float = 0.0
    timed_out: bool = False


Runner = Callable[[Task], Any]  # returns a WorkerResult (already validated)


class TaskScheduler:
    def __init__(
        self,
        max_concurrency: int = DEFAULT_MAX_WORKERS,
        max_depth: int = DEFAULT_DEPTH,
        clock=time.monotonic,
        default_timeout_s: float = 120.0,
        max_total_tasks: int = 24,
    ) -> None:
        if max_concurrency < 1:
            raise SchedulerError("max_concurrency must be >= 1")
        # configured value may go up to the documented cap of 3
        self.max_concurrency = min(int(max_concurrency), HARD_MAX_WORKERS)
        self.max_depth = max(0, int(max_depth))
        self.clock = clock
        self.default_timeout_s = float(default_timeout_s)
        self.max_total_tasks = int(max_total_tasks)

    # ------------------------------------------------------------- guards
    def check_plan(self, tasks: list[Task]) -> None:
        if len(tasks) > self.max_total_tasks:
            raise SchedulerError(
                f"plan has {len(tasks)} tasks (limit {self.max_total_tasks}) — refusing unbounded delegation"
            )
        ids = [t.id for t in tasks]
        if len(ids) != len(set(ids)):
            raise SchedulerError("duplicate task ids in plan")
        known = set(ids)
        for task in tasks:
            if task.parent_id is not None and task.parent_id not in known and task.parent_id != "":
                # parents may live outside this batch (delegated sub-plans)
                pass
            depth = 0
            seen = {task.id}
            cursor = task
            while cursor.depends_on:
                nxt = cursor.depends_on[0]
                if nxt in seen:
                    raise SchedulerError("dependency cycle in plan")
                seen.add(nxt)
                depth += 1
                parent = next((t for t in tasks if t.id == nxt), None)
                if parent is None:
                    break
                cursor = parent
            if depth > self.max_depth + 1:
                raise SchedulerError(
                    f"delegation depth {depth} exceeds configured limit {self.max_depth + 1}"
                )

    # ----------------------------------------------------------- execution
    def run(self, tasks: list[Task], runner: Runner) -> list[ExecutionOutcome]:
        """Execute a plan. Returns one outcome per task that was attempted."""
        self.check_plan(tasks)
        by_id = {t.id: t for t in tasks}
        outcomes: list[ExecutionOutcome] = []
        pending = [t for t in tasks if not t.is_terminal]
        if not pending:
            return outcomes

        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            while pending:
                ready, blocked = [], []
                for task in pending:
                    deps = [by_id[d] for d in task.depends_on if d in by_id]
                    if any(d.state == TaskState.CANCELLED for d in deps):
                        self._fail(task, "dependency cancelled", outcomes)
                    elif any(d.state == TaskState.FAILED for d in deps):
                        self._fail(task, "dependency failed", outcomes)
                    elif all(d.is_terminal for d in deps):
                        ready.append(task)
                    else:
                        blocked.append(task)
                pending = blocked
                if not ready:
                    if not pending:
                        break
                    # everything left is blocked by unfinished tasks
                    time.sleep(0.005)
                    continue

                batch = ready[: self.max_concurrency]
                extra = ready[self.max_concurrency :]
                futures = {}
                for task in batch:
                    if task.state in TERMINAL_STATES:
                        continue
                    futures[pool.submit(self._invoke, task, runner)] = task
                for task in extra:
                    pending.append(task)

                for future, task in futures.items():
                    start = self.clock()
                    timeout = task.timeout_s or self.default_timeout_s
                    try:
                        future.result(timeout=timeout)
                    except FutureTimeout:
                        task.transition(TaskState.FAILED, "timeout")
                        task.failure = f"timeout after {timeout:.0f}s"
                        outcomes.append(
                            ExecutionOutcome(
                                task, False, task.failure, self.clock() - start, timed_out=True
                            )
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001 - worker crash isolation
                        task.transition(TaskState.FAILED, "worker crashed")
                        task.failure = f"{type(exc).__name__}: {exc}"[:400]
                        outcomes.append(
                            ExecutionOutcome(task, False, task.failure, self.clock() - start)
                        )
                        continue
                    duration = self.clock() - start
                    ok = task.state == TaskState.COMPLETED
                    reason = "" if ok else (task.failure or task.state.value)
                    outcomes.append(ExecutionOutcome(task, ok, reason, duration))
        return outcomes

    # ------------------------------------------------------------ helpers
    def _invoke(self, task: Task, runner: Runner) -> None:
        if task.state == TaskState.CANCELLED:
            return
        try:
            task.transition(TaskState.RUNNING, "worker started")
        except TransitionError:
            return
        task.attempts += 1
        result = runner(task)
        if task.state == TaskState.CANCELLED:
            return
        task.result = result
        if result.status == "completed":
            task.transition(TaskState.COMPLETED, "worker reported success")
        else:
            task.transition(TaskState.FAILED, f"worker reported {result.status}")
            task.failure = result.summary[:400]

    def _fail(self, task: Task, reason: str, outcomes: list[ExecutionOutcome]) -> None:
        if task.state in TERMINAL_STATES:
            return
        task.transition(TaskState.FAILED, reason)
        task.failure = reason
        outcomes.append(ExecutionOutcome(task, False, reason))

    # -------------------------------------------------------- cancellation
    @staticmethod
    def cancel(task: Task, requester_id: int) -> bool:
        """Cancel a queued/waiting/running task. Terminal tasks are immutable."""
        if task.is_terminal:
            return False
        task.cancel(requester_id)
        return True


def requeue(task: Task, max_attempts: int) -> bool:
    """Failure recovery: return a failed task to the queue while attempts remain."""
    if task.state != TaskState.FAILED:
        return False
    if task.attempts >= max_attempts:
        return False
    task.failure = ""
    task.transition(TaskState.QUEUED, "retrying after failure")
    return True
