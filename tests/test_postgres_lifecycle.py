"""Milestone 6 PostgreSQL races reuse the existing task state machine."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.models import OutboxEvent
from max_core.store import StoreError, TaskStore


def test_postgres_claim_vs_cancel_is_serialized_and_never_claims_completion(
    postgres_store: TaskStore,
) -> None:
    task, _ = postgres_store.create(
        "owner", "Safe lifecycle race fixture", "race-cancel-" + uuid4().hex
    )
    gate = Barrier(2)

    def try_claim() -> str:
        gate.wait()
        try:
            return postgres_store.claim("owner", task.id, "fixture-worker").state
        except StoreError as exc:
            assert exc.status_code == 409
            return "claim_rejected"

    def request_cancel() -> str:
        gate.wait()
        return postgres_store.cancel("owner", task.id).state

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = pool.submit(try_claim)
        cancelled = pool.submit(request_cancel)
        claim_result = claimed.result()
        cancel_result = cancelled.result()

    latest = postgres_store.get("owner", task.id)
    assert latest.state in {"cancelled", "cancelling"}
    assert cancel_result == latest.state
    if claim_result == "claim_rejected":
        assert latest.state == "cancelled"
    else:
        assert claim_result == "running"
        assert latest.state == "cancelling"

    with Session(postgres_store.engine) as session:
        kinds = session.scalars(
            select(OutboxEvent.kind)
            .where(OutboxEvent.task_id == task.id)
            .order_by(OutboxEvent.task_revision)
        ).all()
    assert kinds[0] == "task.queued"
    assert kinds[-1] in {"task.cancelled", "task.cancel_requested"}
    assert kinds.count("task.cancelled") + kinds.count("task.cancel_requested") == 1
    assert "task.verified" not in kinds


def test_postgres_simultaneous_cancel_idempotent_no_duplicate_effect(
    postgres_store: TaskStore,
) -> None:
    task, _ = postgres_store.create(
        "owner", "Double cancellation fixture", "double-cancel-" + uuid4().hex
    )
    gate = Barrier(2)

    def cancel() -> str:
        gate.wait()
        return postgres_store.cancel("owner", task.id).state

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cancel)
        second = pool.submit(cancel)
        assert first.result() == second.result() == "cancelled"

    final = postgres_store.get("owner", task.id)
    assert final.state == "cancelled"
    assert final.revision == 2
    with Session(postgres_store.engine) as session:
        events = session.scalars(
            select(OutboxEvent).where(OutboxEvent.task_id == task.id)
        ).all()
    assert sorted(event.kind for event in events) == ["task.cancelled", "task.queued"]
    assert all(event.delivery_state == "pending" for event in events)
