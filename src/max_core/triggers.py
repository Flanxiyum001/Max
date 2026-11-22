"""Bounded proactive triggers: deterministic observations queued for owner review.

Triggers never call a model, never execute state-changing work, and never
touch the network. Each fired run stores a bounded, display-safe summary and
at most one recommended follow-up proposal. Promoting that proposal into the
Trust Inbox is an explicit owner action that goes through the exact same
single-use R1/R2 approval path as every other tool call. The recurring
schedule is a strict 5-field cron evaluated by a small deterministic stdlib
matcher; a scheduler dependency (APScheduler) was evaluated and deliberately
deferred per docs/DEPENDENCY_EVALUATION.md - MAX owns its own task state.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from max_core.bounded_commands import BoundedCommandError, run_bounded_command
from max_core.gateway.contracts import ToolProposal
from max_core.models import Task, TriggerRule, TriggerRun, utc_now
from max_core.store import TaskStore
from max_core.trust import ActorContext, TrustError, TrustService

_FIELD = re.compile(r"^[0-9*/,-]{1,32}$")
_ACTOR = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_KINDS = ("briefing.daily", "notes.digest", "repo.status")
_MAX_RULES = 10
_MAX_PENDING_RUNS = 20
_FIELD_RANGE = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


class TriggerError(ValueError):
    """Stable, display-safe trigger error code with an HTTP status."""

    def __init__(self, code: str, status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def parse_cron(spec: str) -> tuple[tuple[int, ...], ...]:
    """Strict bounded 5-field cron (minute hour dom mon dow): lists, ranges, steps."""
    if not isinstance(spec, str):
        raise TriggerError("invalid_trigger_schedule")
    parts = spec.strip().split()
    if len(parts) != 5 or any(_FIELD.fullmatch(part) is None for part in parts):
        raise TriggerError("invalid_trigger_schedule")
    fields: list[tuple[int, ...]] = []
    for index, raw in enumerate(parts):
        low, high = _FIELD_RANGE[index]
        allowed: set[int] = set()
        for item in raw.split(","):
            if item.count("/") > 1:
                raise TriggerError("invalid_trigger_schedule")
            base, _sep, step_text = item.partition("/")
            if step_text and (not step_text.isdigit() or int(step_text) < 1):
                raise TriggerError("invalid_trigger_schedule")
            step = int(step_text) if step_text else 1
            if base == "*":
                start, end = low, high
            else:
                bounds = base.split("-")
                if len(bounds) == 2 and all(b.isdigit() for b in bounds):
                    start, end = int(bounds[0]), int(bounds[1])
                elif base.isdigit():
                    start = end = int(base)
                else:
                    raise TriggerError("invalid_trigger_schedule")
            if not low <= start <= end <= high:
                raise TriggerError("invalid_trigger_schedule")
            allowed.update(range(start, end + 1, step))
        if not allowed:
            raise TriggerError("invalid_trigger_schedule")
        fields.append(tuple(sorted(allowed)))
    return tuple(fields)


def cron_matches(spec: str, moment: datetime) -> bool:
    fields = parse_cron(spec)
    # Standard cron: 0 = Sunday. Python: Monday == 0. Shift and wrap.
    cron_weekday = (moment.weekday() + 1) % 7
    return (
        moment.minute in fields[0]
        and moment.hour in fields[1]
        and moment.day in fields[2]
        and moment.month in fields[3]
        and cron_weekday in fields[4]
    )


def minute_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TriggerRuleView:
    id: str
    kind: str
    schedule: str
    enabled: bool
    last_fired_minute: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TriggerRunView:
    id: str
    rule_id: str
    kind: str
    state: str
    output: str
    proposal_json: str | None
    approval_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _Action:
    output: str
    proposal_json: str | None


def _proposal(objective: str) -> str:
    canonical = json.dumps(
        {"name": "task.create", "arguments": {"objective": objective.strip()[:300]}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return canonical[:1_000]


class TriggerService:
    """Owner-scoped rules plus one bounded deterministic evaluation per fire."""

    def __init__(
        self,
        engine: Engine,
        trust: TrustService | None = None,
        *,
        notes_dir: Path | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._engine = engine
        self._trust = trust
        self._notes_dir = notes_dir
        self._workspace_root = workspace_root

    def add_rule(self, owner_id: str, kind: str, schedule: str) -> TriggerRuleView:
        if not isinstance(owner_id, str) or _ACTOR.fullmatch(owner_id) is None:
            raise TriggerError("invalid_trigger_owner")
        if isinstance(kind, bool) or kind not in _KINDS:
            raise TriggerError("unknown_trigger_kind")
        normalized = " ".join(schedule.split()) if isinstance(schedule, str) else ""
        parse_cron(normalized)
        rule = TriggerRule(id=str(uuid4()), owner_id=owner_id, kind=kind, schedule=normalized)
        try:
            with self._sessions.begin() as session:
                count = session.scalar(
                    select(func.count())
                    .select_from(TriggerRule)
                    .where(TriggerRule.owner_id == owner_id)
                )
                if (count or 0) >= _MAX_RULES:
                    raise TriggerError("too_many_trigger_rules", 409)
                session.add(rule)
        except IntegrityError:
            raise TriggerError("duplicate_trigger_kind", 409) from None
        return self._rule_view(rule)

    def list_rules(self, owner_id: str) -> tuple[TriggerRuleView, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(TriggerRule)
                .where(TriggerRule.owner_id == owner_id)
                .order_by(TriggerRule.created_at.asc())
            ).all()
            return tuple(self._rule_view(row) for row in rows)

    def remove_rule(self, owner_id: str, rule_id: str) -> None:
        with self._sessions.begin() as session:
            rule = session.scalar(
                select(TriggerRule).where(
                    TriggerRule.id == rule_id, TriggerRule.owner_id == owner_id
                )
            )
            if rule is None:
                raise TriggerError("unknown_trigger_rule", 404)
            session.delete(rule)

    def runs(self, owner_id: str, limit: int = 20) -> tuple[TriggerRunView, ...]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise TriggerError("invalid_run_limit")
        with self._sessions() as session:
            rows = session.scalars(
                select(TriggerRun)
                .where(TriggerRun.owner_id == owner_id)
                .order_by(TriggerRun.created_at.desc())
                .limit(limit)
            ).all()
            return tuple(self._run_view(row) for row in rows)

    def dismiss_run(self, owner_id: str, run_id: str) -> TriggerRunView:
        return self._finish_run(owner_id, run_id, "dismissed")

    def promote_run(self, owner_id: str, run_id: str) -> TriggerRunView:
        """Queue the run's recommended proposal into the Trust Inbox (single-use R1/R2)."""
        if self._trust is None:
            raise TriggerError("trust_unavailable", 503)
        with self._sessions() as session:
            run = session.scalar(
                select(TriggerRun).where(TriggerRun.id == run_id, TriggerRun.owner_id == owner_id)
            )
            if run is None:
                raise TriggerError("unknown_trigger_run", 404)
            if run.state == "promoted" and run.approval_id is not None:
                if self._pending_grant(owner_id, run.approval_id) is not None:
                    return self._run_view(run)
            if run.state != "pending_review" or not run.proposal_json:
                raise TriggerError("run_not_promotable", 409)
        try:
            recommended = json.loads(run.proposal_json or "")
        except ValueError:
            raise TriggerError("run_not_promotable", 409) from None
        if not isinstance(recommended, dict):
            raise TriggerError("run_not_promotable", 409)
        name = recommended.get("name")
        arguments = recommended.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise TriggerError("run_not_promotable", 409)
        try:
            store = TaskStore(self._engine)
            task, _ = store.create(
                owner_id, f"Trigger run {run_id} follow-up", f"trigger-run-{run_id[:79]}"
            )
        except Exception:
            raise TriggerError("run_not_promotable", 409) from None
        context = ActorContext(
            owner_id, task.id, "proactive-scheduler", "trigger-loop", "local-desktop"
        )
        try:
            grant = self._trust.request(context, ToolProposal(run_id, name, arguments), 900)
        except TrustError as error:
            if error.code == "duplicate_proposal":
                raise TriggerError("run_already_promoted", 409) from None
            if error.code == "invalid_task_objective" or error.code == "capability_denied":
                raise TriggerError("run_not_promotable", 409) from None
            raise TriggerError("run_not_promotable", 409) from None
        with self._sessions.begin() as session:
            record = session.get(TriggerRun, run_id)
            if record is None or record.owner_id != owner_id:
                raise TriggerError("unknown_trigger_run", 404)
            if record.state == "promoted" and record.approval_id is not None:
                return self._run_view(record)
            record.state = "promoted"
            record.approval_id = grant.id
            record.reviewed_at = utc_now()
            session.flush()
            return self._run_view(record)

    def _pending_grant(self, owner_id: str, approval_id: str) -> object | None:
        assert self._trust is not None
        try:
            grant = self._trust.get_grant(owner_id, approval_id)
        except TrustError:
            return None
        return grant if getattr(grant, "state", None) == "pending" else None

    def _finish_run(self, owner_id: str, run_id: str, state: str) -> TriggerRunView:
        with self._sessions.begin() as session:
            run = session.scalar(
                select(TriggerRun).where(TriggerRun.id == run_id, TriggerRun.owner_id == owner_id)
            )
            if run is None:
                raise TriggerError("unknown_trigger_run", 404)
            if run.state != "pending_review":
                raise TriggerError("run_not_pending", 409)
            run.state = state
            run.reviewed_at = utc_now()
            session.flush()
            return self._run_view(run)

    def tick(self, owner_id: str, moment: datetime | None = None) -> int:
        """Fire every enabled rule that is due, exactly once per calendar minute."""
        now = _as_utc(moment) if moment is not None else datetime.now(UTC)
        current = minute_key(now)
        with self._sessions() as session:
            rules = session.scalars(
                select(TriggerRule).where(
                    TriggerRule.owner_id == owner_id, TriggerRule.enabled.is_(True)
                )
            ).all()
            due = [
                (rule.id, rule.kind)
                for rule in rules
                if cron_matches(rule.schedule, now) and rule.last_fired_minute != current
            ]
        if not due:
            return 0
        pending = sum(1 for run in self.runs(owner_id) if run.state == "pending_review")
        fired = 0
        for rule_id, kind in due:
            if pending >= _MAX_PENDING_RUNS:
                break
            action = self._observe(owner_id, kind)
            with self._sessions.begin() as session:
                record = session.get(TriggerRule, rule_id)
                if record is None or record.last_fired_minute == current:
                    continue
                record.last_fired_minute = current
                record.updated_at = utc_now()
                session.add(
                    TriggerRun(
                        id=str(uuid4()),
                        owner_id=owner_id,
                        rule_id=rule_id,
                        kind=kind,
                        output=action.output,
                        proposal_json=action.proposal_json,
                    )
                )
            pending += 1
            fired += 1
        return fired

    def _observe(self, owner_id: str, kind: str) -> _Action:
        if kind == "briefing.daily":
            return self._briefing(owner_id)
        if kind == "notes.digest":
            return self._notes_digest()
        return self._repo_status()

    def _briefing(self, owner_id: str) -> _Action:
        with self._sessions() as session:
            rows = session.execute(
                select(Task.state, func.count())
                .where(Task.owner_id == owner_id)
                .group_by(Task.state)
            ).all()
            counts = {state: total for state, total in rows}
        ordered = ", ".join(f"{state}={total}" for state, total in sorted(counts.items()))
        active = counts.get("queued", 0) + counts.get("running", 0)
        objective = f"Morning briefing review ({active} active tasks)"
        return _Action(
            f"Task journal at briefing time: {ordered or 'no tasks'}. R1/R2 work waits in the Trust Inbox.",
            _proposal(objective),
        )

    def _notes_digest(self) -> _Action:
        directory = self._notes_dir
        if not isinstance(directory, Path) or not directory.is_dir():
            return _Action("Notes directory unavailable; nothing summarized.", None)
        try:
            notes = sorted(
                directory.glob("*.txt"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return _Action("Notes directory unavailable; nothing summarized.", None)
        titles = ", ".join(note.stem[:60] for note in notes[:5]) or "none"
        objective = f"Review notes digest ({len(notes)} bounded note(s))"
        return _Action(f"{len(notes)} bounded note(s); latest: {titles}.", _proposal(objective))

    def _repo_status(self) -> _Action:
        root = self._workspace_root
        if not isinstance(root, Path) or not root.is_dir():
            return _Action("Workspace root unavailable; repository status not checked.", None)
        try:
            result = run_bounded_command(root, ("git", "status", "--porcelain=v1"), 30)
        except BoundedCommandError:
            return _Action("Repository status unavailable (bounded git failed).", None)
        if result["exit_code"] != 0:
            return _Action("Repository status unavailable (bounded git failed).", None)
        lines = [line for line in str(result["output"]).splitlines() if line.strip()]
        summary = f"{len(lines)} changed path(s)" if lines else "clean working tree"
        if not lines:
            return _Action("git status: clean working tree.", None)
        objective = f"Review repository changes ({len(lines)} path(s))"
        return _Action(f"git status: {summary}.", _proposal(objective))

    @staticmethod
    def _rule_view(rule: TriggerRule) -> TriggerRuleView:
        return TriggerRuleView(
            id=rule.id,
            kind=rule.kind,
            schedule=rule.schedule,
            enabled=rule.enabled,
            last_fired_minute=rule.last_fired_minute,
            created_at=_as_utc(rule.created_at),
        )

    @staticmethod
    def _run_view(run: TriggerRun) -> TriggerRunView:
        return TriggerRunView(
            id=run.id,
            rule_id=run.rule_id,
            kind=run.kind,
            state=run.state,
            output=run.output,
            proposal_json=run.proposal_json,
            approval_id=run.approval_id,
            created_at=_as_utc(run.created_at),
        )


class TriggerLoop:
    """Single bounded background ticker; every effect still waits for Trust approval."""

    def __init__(
        self, service: TriggerService, owner_id: str = "owner", interval_seconds: int = 60
    ) -> None:
        if type(interval_seconds) is not int or not 15 <= interval_seconds <= 3600:
            raise TriggerError("invalid_loop_interval")
        self._service = service
        self._owner_id = owner_id
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="max-trigger-loop", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout_seconds)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._service.tick(self._owner_id)
            except Exception:  # noqa: BLE001 - the loop must never die from one bad tick
                return
