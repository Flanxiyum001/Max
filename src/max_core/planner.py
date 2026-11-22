"""Deterministic multi-step task plans: visible structure, zero autonomous execution.

Decomposition never calls a model and never invents goals: sub-steps come from
the owner's own explicit structure (numbered lists, bullets, semicolons,
newlines, or ``then`` separators) turned into a bounded task graph in the Task
Journal. Each step is a normal Trust-gated task; R1/R2 boundaries pause in the
Trust Inbox exactly like any other tool call, and the plan view shows which
steps are ready. Nothing executes because a later step is due or the parent
exists - only the owner's existing approval flow moves work forward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from max_core.models import Task, utc_now
from max_core.persistence import StoreError
from max_core.store import TaskStore, TaskView

_MAX_OBJECTIVE = 500
_MAX_STEP_CHARS = 200
_MAX_STEPS = 8
_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")
_UNSAFE_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STEP_LABEL = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*+])\s+")
_SEPARATORS = (";", "\n", " then ", " Then ", " THEN ")
_TERMINAL = {"completed", "cancelled", "failed"}


class PlanError(ValueError):
    """Stable, display-safe planning error code with an HTTP status."""

    def __init__(self, code: str, status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _clean_step(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    if _UNSAFE_CONTROLS.search(raw):
        return None
    step = " ".join(raw.strip().split())
    if not 1 <= len(step) <= _MAX_STEP_CHARS:
        return None
    return step


def decompose(objective: str) -> list[str]:
    """Split an objective on the owner's own explicit structure; [] means no plan."""
    if not isinstance(objective, str) or _UNSAFE_CONTROLS.search(objective):
        raise PlanError("invalid_task_objective")
    text = objective.strip()
    if not 1 <= len(text) <= _MAX_OBJECTIVE:
        raise PlanError("invalid_task_objective")

    lines = text.splitlines()
    labeled = [line for line in lines if _STEP_LABEL.match(line)]
    if len(labeled) >= 2:
        steps = [step for line in labeled if (step := _clean_step(_STEP_LABEL.sub("", line)))]
        return _bounded(steps)
    for separator in _SEPARATORS:
        if separator in text:
            steps = [step for part in text.split(separator) if (step := _clean_step(part))]
            if len(steps) >= 2:
                return _bounded(steps)
    return []


def _bounded(steps: list[str]) -> list[str]:
    if len(steps) > _MAX_STEPS:
        raise PlanError("plan_too_many_steps")
    seen: set[str] = set()
    result: list[str] = []
    for step in steps:
        key = step.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(step)
    return result if len(result) >= 2 else []


@dataclass(frozen=True, slots=True)
class PlanStep:
    task_id: str
    objective: str
    state: str
    position: int
    ready: bool


@dataclass(frozen=True, slots=True)
class PlanView:
    parent: TaskView
    steps: tuple[PlanStep, ...]


class PlanService:
    """Creates and reads bounded plan graphs; execution stays app- and owner-driven."""

    def __init__(self, engine: Engine, store: TaskStore | None = None) -> None:
        self._store = store if store is not None else TaskStore(engine)
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def create_plan(
        self, owner_id: str, objective: str, idempotency_key: str
    ) -> tuple[TaskView, tuple[PlanStep, ...], bool]:
        steps = decompose(objective)
        parent, created = self._store.create(owner_id, objective, idempotency_key)
        existing = self._load_steps(owner_id, parent.id)
        if existing:
            return parent, existing, created
        if not steps:
            return parent, (), created
        for index, step_text in enumerate(steps, start=1):
            key = f"{idempotency_key}:step:{index:02d}"
            if not _KEY.fullmatch(key):
                raise PlanError("invalid_task_idempotency_key")
            try:
                step_task, _ = self._store.create(owner_id, step_text, key)
            except IntegrityError:
                raise PlanError("duplicate_plan_step", 409) from None
            with self._sessions.begin() as session:
                record = session.get(Task, step_task.id)
                if record is None or record.owner_id != owner_id:
                    raise PlanError("persistence_unavailable", 503)
                if record.parent_id is None:
                    record.parent_id = parent.id
                    record.plan_position = index
                    record.updated_at = utc_now()
        return parent, self._load_steps(owner_id, parent.id), created

    def plan_view(self, owner_id: str, task_id: str) -> PlanView:
        try:
            parent = self._store.get(owner_id, task_id)
        except StoreError as error:
            raise PlanError(
                "task_not_found" if error.status_code == 404 else error.reason, error.status_code
            ) from None
        return PlanView(parent, self._load_steps(owner_id, parent.id))

    def complete_step(self, owner_id: str, task_id: str) -> PlanView:
        """Owner-verified finish for one step; the next ready step is `ready` in the view."""
        with self._sessions.begin() as session:
            record = session.get(Task, task_id)
            if record is None or record.owner_id != owner_id:
                raise PlanError("task_not_found", 404)
            parent_id = record.parent_id or record.id
            if record.state in {"cancelled", "cancelling"}:
                raise PlanError("step_not_advanceable", 409)
            if record.state != "completed":
                record.state = "completed"
                record.revision += 1
                record.updated_at = utc_now()
            session.flush()
        return self.plan_view(owner_id, parent_id)

    def next_ready_step(self, owner_id: str, task_id: str) -> PlanStep | None:
        view = self.plan_view(owner_id, task_id)
        return next((step for step in view.steps if step.ready), None)

    def _load_steps(self, owner_id: str, parent_id: str) -> tuple[PlanStep, ...]:
        stored: list[Task] = []
        with self._sessions() as session:
            rows = session.scalars(
                select(Task)
                .where(Task.owner_id == owner_id, Task.parent_id == parent_id)
                .order_by(Task.plan_position.asc(), Task.created_at.asc())
            ).all()
            stored.extend(rows)
        steps: list[PlanStep] = []
        prior_finished = True
        for index, row in enumerate(stored, start=1):
            position = row.plan_position if row.plan_position is not None else index
            ready = prior_finished and row.state not in _TERMINAL
            prior_finished = prior_finished and row.state == "completed"
            steps.append(
                PlanStep(
                    task_id=row.id,
                    objective=row.objective,
                    state=row.state,
                    position=position,
                    ready=ready,
                )
            )
        return tuple(steps)
