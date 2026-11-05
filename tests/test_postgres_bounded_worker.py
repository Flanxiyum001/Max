"""PostgreSQL concurrency tests for manual bounded-worker lease and cancel races."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core import bounded_worker
from max_core.bounded_worker import BoundedWorker, WorkerError
from max_core.models import OutboxEvent
from max_core.store import StoreError, TaskStore


def objective(a: int = 9, b: int = 3) -> str:
    return json.dumps({"op": "dev.add", "params": {"a": a, "b": b}})


def test_postgres_two_workers_never_execute_same_task(
    postgres_store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _ = postgres_store.create(
        "owner", objective(), "pg-worker-double-" + uuid4().hex
    )
    entering, release = Event(), Event()
    original = bounded_worker.execute_operation
    calls = 0

    def bounded(op: bounded_worker.ValidatedOperation) -> bytes:
        nonlocal calls
        calls += 1
        entering.set()
        assert release.wait(5)
        return original(op)

    monkeypatch.setattr(bounded_worker, "execute_operation", bounded)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winning = pool.submit(BoundedWorker(postgres_store, "worker-one").run_one, task.id)
        assert entering.wait(5), "First worker did not execute"
        with pytest.raises(WorkerError):
            BoundedWorker(postgres_store, "worker-two").run_one(task.id)
        release.set()
        report = winning.result(timeout=10)

    # The injected synchronization is longer than the real one-second budget on
    # a heavily loaded CI runner, so either quarantine or unverified is safe.
    assert report.state in {"awaiting_verification", "needs_reconciliation"}
    assert calls == 1
    with Session(postgres_store.engine) as session:
        kinds = session.scalars(
            select(OutboxEvent.kind).where(OutboxEvent.task_id == task.id)
        ).all()
    assert kinds.count("task.running") == 1
    assert "task.verified" not in kinds


def test_postgres_concurrent_cancel_after_start_acknowledges_real_stop(
    postgres_store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _ = postgres_store.create(
        "owner", objective(3, 2), "pg-worker-cancel-" + uuid4().hex
    )
    entering, release = Event(), Event()
    original = bounded_worker.execute_operation

    def paused(op: bounded_worker.ValidatedOperation) -> bytes:
        entering.set()
        assert release.wait(5)
        return original(op)

    monkeypatch.setattr(bounded_worker, "execute_operation", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(BoundedWorker(postgres_store, "worker-cancel").run_one, task.id)
        assert entering.wait(5)
        assert postgres_store.cancel("owner", task.id).state == "cancelling"
        # This is still only a request: the worker is paused and has not stopped.
        assert postgres_store.get("owner", task.id).state == "cancelling"
        release.set()
        report = future.result(timeout=10)

    assert report.state == "cancelled"
    assert report.outcome_digest is None
    assert postgres_store.get("owner", task.id).state == "cancelled"
    with Session(postgres_store.engine) as session:
        kinds = session.scalars(
            select(OutboxEvent.kind).where(OutboxEvent.task_id == task.id)
        ).all()
    assert kinds.count("task.cancel_requested") == 1
    assert kinds.count("task.cancelled") == 1
    assert "task.awaiting_verification" not in kinds
    assert "task.verified" not in kinds


def test_postgres_cancel_races_unverified_outcome_without_completion(
    postgres_store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _ = postgres_store.create(
        "owner", objective(1, 1), "pg-worker-completion-race-" + uuid4().hex
    )
    entering, release = Event(), Event()
    original = postgres_store.record_outcome

    def paused_outcome(*args: str) -> object:
        entering.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(postgres_store, "record_outcome", paused_outcome)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(BoundedWorker(postgres_store, "race-worker").run_one, task.id)
        assert entering.wait(5)
        # Cancellation wins the race while worker pauses before record_outcome.
        assert postgres_store.cancel("owner", task.id).state == "cancelling"
        release.set()
        result = future.result(timeout=10)

    assert result.state == "cancelled"
    assert result.outcome_digest is None
    assert postgres_store.get("owner", task.id).state == "cancelled"
    with Session(postgres_store.engine) as session:
        kinds = session.scalars(
            select(OutboxEvent.kind).where(OutboxEvent.task_id == task.id)
        ).all()
    assert "task.awaiting_verification" not in kinds
    assert "task.verified" not in kinds


def test_postgres_expired_claim_requires_manual_reconciliation(
    postgres_store: TaskStore
) -> None:
    task, _ = postgres_store.create(
        "owner", objective(), "pg-worker-interrupted-" + uuid4().hex
    )
    postgres_store.claim("owner", task.id, "interrupted-worker", ttl_seconds=5)
    with pytest.raises(WorkerError, match="task_not_queued"):
        BoundedWorker(postgres_store).run_one(task.id)
    # Simulate a lost worker's expired lease using a trusted test transaction.
    from datetime import UTC, datetime, timedelta

    from max_core.models import Task

    with Session(postgres_store.engine) as session, session.begin():
        record = session.get(Task, task.id)
        assert record
        record.lease_until = datetime.now(UTC) - timedelta(seconds=2)
    assert postgres_store.reconcile_expired("owner") >= 1
    assert postgres_store.get("owner", task.id).state == "needs_reconciliation"
    with pytest.raises(WorkerError, match="task_not_queued"):
        BoundedWorker(postgres_store).run_one(task.id)
