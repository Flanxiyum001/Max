"""Milestone 7: pure allowlist, fenced worker execution and mandatory verification."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core import bounded_worker
from max_core.bounded_worker import (
    BoundedWorker,
    WorkerError,
    execute_operation,
    parse_operation,
)
from max_core.models import OutboxEvent, Task, TaskCheckpoint
from max_core.store import StoreError, TaskStore
from max_core.verification import IndependentVerifier


def task_for(store: TaskStore, objective: str, key: str | None = None) -> str:
    task, _ = store.create("owner", objective, key or f"worker-{uuid4().hex}")
    return task.id


def objective(op: str, params: dict[str, object]) -> str:
    return json.dumps({"op": op, "params": params}, separators=(",", ":"))


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (objective("dev.health", {}), b'{"operation":"dev.health","status":"ok"}'),
        (objective("dev.add", {"a": -10, "b": 12}), b'{"operation":"dev.add","result":2}'),
    ],
)
def test_each_registered_operation_records_unverified_outcome(
    store: TaskStore, operation: str, expected: bytes
) -> None:
    task_id = task_for(store, operation)
    result = BoundedWorker(store, "worker-a").run_one(task_id)
    assert result.state == "awaiting_verification"
    assert result.result == expected.decode("ascii")
    assert result.outcome_digest == hashlib.sha256(expected).hexdigest()
    assert "Unverified local result" in result.display()
    assert "NOT independent verification" in result.display()
    assert store.get("owner", task_id).state == "awaiting_verification"

    with Session(store.engine) as session:
        checkpoints = session.scalars(
            select(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id)
        ).all()
        assert len(checkpoints) == 1
        assert checkpoints[0].step_name == json.loads(operation)["op"]
        events = session.scalars(
            select(OutboxEvent).where(OutboxEvent.task_id == task_id)
        ).all()
        assert len(events) == 4
        assert sorted(x.kind for x in events) == [
            "task.awaiting_verification", "task.checkpointed", "task.queued", "task.running"
        ]
        assert all(x.delivery_state == "pending" for x in events)


@pytest.mark.parametrize(
    "bad",
    [
        "hello world",
        "[]",
        "null",
        "123",
        objective("shell.run", {"cmd": "whoami"}),
        objective("dev.health", {"extra": 1}),
        objective("dev.add", {"a": True, "b": 4}),
        objective("dev.add", {"a": 1.2, "b": 4}),
        objective("dev.add", {"a": 1_000_001, "b": 1}),
        objective("dev.add", {"a": 1}),
        objective("dev.add", {"a": 1, "b": 3, "c": 4}),
        '{"op":"dev.health","op":"dev.add","params":{}}',
        '{"op":"dev.health","params":{},"command":"sh"}',
        '{"op":"dev.add","params":{"a":1,"a":2,"b":3}}',
    ],
)
def test_bad_or_unknown_operation_does_not_claim(store: TaskStore, bad: str) -> None:
    task_id = task_for(store, bad)
    with pytest.raises(WorkerError):
        BoundedWorker(store).run_one(task_id)
    assert store.get("owner", task_id).state == "queued"
    with Session(store.engine) as session:
        events = session.scalars(
            select(OutboxEvent).where(OutboxEvent.task_id == task_id)
        ).all()
        assert len(events) == 1 and events[0].kind == "task.queued"


def test_operation_bounds_and_zero_dynamic_execution() -> None:
    for params in ({"a": -1_000_000, "b": 1_000_000}, {"a": 1_000_000, "b": 1_000_000}):
        op = parse_operation(objective("dev.add", params))
        raw = execute_operation(op)
        assert len(raw) <= 256
        assert json.loads(raw)["result"] == params["a"] + params["b"]
    assert set(bounded_worker.OPERATIONS) == {"dev.health", "dev.add"}
    assert all(
        x.permission == "local_owner_development" and x.max_seconds == 1.0
        for x in bounded_worker.OPERATIONS.values()
    )


def test_cancel_queued_prevents_execution(store: TaskStore) -> None:
    task_id = task_for(store, objective("dev.add", {"a": 3, "b": 4}))
    assert store.cancel("owner", task_id).state == "cancelled"
    with pytest.raises(WorkerError, match="task_not_queued"):
        BoundedWorker(store).run_one(task_id)
    assert store.get("owner", task_id).state == "cancelled"


def test_cancellation_after_worker_stopped_is_acknowledged(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = task_for(store, objective("dev.add", {"a": 8, "b": 9}))
    real_execute = bounded_worker.execute_operation

    def simulate_cancel(op: bounded_worker.ValidatedOperation) -> bytes:
        assert store.cancel("owner", task_id).state == "cancelling"
        return real_execute(op)

    monkeypatch.setattr(bounded_worker, "execute_operation", simulate_cancel)
    report = BoundedWorker(store, "worker-cancel").run_one(task_id)
    assert report.state == "cancelled"
    assert report.outcome_digest is None
    assert report.result is None
    assert store.get("owner", task_id).state == "cancelled"
    with Session(store.engine) as session:
        kinds = session.scalars(
            select(OutboxEvent.kind).where(OutboxEvent.task_id == task_id)
        ).all()
        assert kinds.count("task.cancel_requested") == 1
        assert kinds.count("task.cancelled") == 1
        assert "task.awaiting_verification" not in kinds


def test_cancel_while_running_requires_correct_active_lease(store: TaskStore) -> None:
    task_id = task_for(store, objective("dev.health", {}))
    store.claim("owner", task_id, "actual-worker")
    store.cancel("owner", task_id)
    with pytest.raises(StoreError):
        store.acknowledge_stopped_cancel("owner", task_id, "impostor")
    assert store.get("owner", task_id).state == "cancelling"
    confirmed = store.acknowledge_stopped_cancel("owner", task_id, "actual-worker")
    assert confirmed.state == "cancelled"
    with pytest.raises(StoreError):
        store.acknowledge_stopped_cancel("owner", task_id, "actual-worker")


def test_failure_and_timeout_quarantine_instead_of_retry(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = task_for(store, objective("dev.health", {}))

    def fail(_: bounded_worker.ValidatedOperation) -> bytes:
        raise ValueError("SECRET-EXECUTION-ERROR")

    monkeypatch.setattr(bounded_worker, "execute_operation", fail)
    report = BoundedWorker(store, "worker-failed").run_one(task_id)
    assert report.state == "needs_reconciliation"
    assert report.outcome_digest is None
    assert "SECRET" not in report.display()
    assert store.get("owner", task_id).state == "needs_reconciliation"
    with pytest.raises(WorkerError, match="task_not_queued"):
        BoundedWorker(store).run_one(task_id)


def test_output_size_limit_quarantines(store: TaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = task_for(store, objective("dev.health", {}))
    monkeypatch.setattr(bounded_worker, "execute_operation", lambda _: b"x" * 300)
    report = BoundedWorker(store).run_one(task_id)
    assert report.state == "needs_reconciliation"
    assert report.result is None


def test_execution_time_overrun_is_quarantined(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = task_for(store, objective("dev.health", {}))
    times = iter((100.0, 102.0))
    monkeypatch.setattr(bounded_worker.time, "monotonic", lambda: next(times))
    report = BoundedWorker(store).run_one(task_id)
    assert report.state == "needs_reconciliation"
    assert report.outcome_digest is None


def test_keyboard_interrupt_does_not_trigger_blind_replay(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = task_for(store, objective("dev.health", {}))

    def interrupt(_: bounded_worker.ValidatedOperation) -> bytes:
        raise KeyboardInterrupt

    monkeypatch.setattr(bounded_worker, "execute_operation", interrupt)
    report = BoundedWorker(store).run_one(task_id)
    assert report.state == "needs_reconciliation"
    assert store.get("owner", task_id).state == "needs_reconciliation"


def test_expired_lease_never_becomes_verified_or_claimed_again(store: TaskStore) -> None:
    task_id = task_for(store, objective("dev.health", {}))
    store.claim("owner", task_id, "crashed-worker", ttl_seconds=5)
    with Session(store.engine) as session, session.begin():
        record = session.get(Task, task_id)
        assert record
        record.lease_until = datetime.now(UTC) - timedelta(seconds=5)
    with pytest.raises(WorkerError, match="task_not_queued"):
        BoundedWorker(store).run_one(task_id)
    with pytest.raises(StoreError):
        store.quarantine_claim("owner", task_id, "crashed-worker")
    assert store.reconcile_expired("owner") == 1
    assert store.get("owner", task_id).state == "needs_reconciliation"
    assert store.reconcile_expired("owner") == 0


def test_verifier_needs_independent_observation(
    store: TaskStore
) -> None:
    task_id = task_for(store, objective("dev.add", {"a": 3, "b": 5}))
    report = BoundedWorker(store).run_one(task_id)
    assert report.state == "awaiting_verification"

    class MissingEvidence:
        def observe(self, _: str) -> None:
            return None

    pending = IndependentVerifier(store.engine, MissingEvidence()).verify("owner", task_id)
    assert pending.task_state == "awaiting_verification"
    assert store.get("owner", task_id).state == "awaiting_verification"

    # A separately supplied test-only observer can later independently confirm bytes.
    class TrustedFixture:
        def observe(self, _: str) -> bytes:
            return b'{"operation":"dev.add","result":8}'

    proof = IndependentVerifier(store.engine, TrustedFixture()).verify("owner", task_id)
    assert proof.task_state == "completed"
    assert store.get("owner", task_id).state == "completed"


def test_no_network_shell_or_file_capability_in_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import os
    import socket
    import subprocess

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("forbidden side effect invoked")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert execute_operation(parse_operation(objective("dev.add", {"a": 1, "b": 2}))) == (
        b'{"operation":"dev.add","result":3}'
    )


def test_worker_does_not_reveal_rejected_objective(
    store: TaskStore
) -> None:
    sensitive = '{"op":"dev.health","params":{},"secret":"VERY_PRIVATE_TEST_VALUE"}'
    task_id = task_for(store, sensitive)
    with pytest.raises(WorkerError) as exc:
        BoundedWorker(store).run_one(task_id)
    assert "VERY_PRIVATE_TEST_VALUE" not in str(exc.value)
    assert store.get("owner", task_id).state == "queued"
