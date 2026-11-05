"""Real PostgreSQL observer races, durable provenance, and 0003 schema compatibility."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from max_core.evidence_observer import DevAddObserver, ObservationSnapshot
from max_core.models import OutboxEvent, Task, TrustAudit, VerificationCheck
from max_core.store import StoreError, TaskStore
from max_core.verification import IndependentVerifier


def waiting(store: TaskStore, a: int = 4, b: int = 6) -> str:
    objective = json.dumps({"op": "dev.add", "params": {"a": a, "b": b}})
    output = json.dumps(
        {"operation": "dev.add", "result": a + b},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    task, _ = store.create("owner", objective, "pg-evidence-" + uuid4().hex)
    store.claim("owner", task.id, "pg-worker")
    store.checkpoint(
        "owner",
        task.id,
        "pg-worker",
        "dev-operation-v1",
        "dev.add",
        hashlib.sha256(objective.encode("utf-8")).hexdigest(),
    )
    store.record_outcome("owner", task.id, "pg-worker", hashlib.sha256(output).hexdigest())
    return task.id


def checks(store: TaskStore, task_id: str) -> list[VerificationCheck]:
    with Session(store.engine) as session:
        return list(
            session.scalars(
                select(VerificationCheck).where(VerificationCheck.task_id == task_id)
            ).all()
        )


def test_postgres_identical_concurrent_requests_are_one_decision(postgres_store: TaskStore) -> None:
    task_id = waiting(postgres_store)
    gate = Barrier(5)

    def check(_: int) -> object:
        gate.wait()
        return IndependentVerifier(postgres_store.engine).verify_dev_add(
            "owner", task_id, "pg-same-request-001"
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        all_results = list(pool.map(check, range(5)))
    assert len({x.check_id for x in all_results}) == 1
    assert all(x.conclusion == "supported" and x.task_state == "completed" for x in all_results)
    assert len(checks(postgres_store, task_id)) == 1
    with Session(postgres_store.engine) as session:
        events = session.scalars(select(OutboxEvent).where(OutboxEvent.task_id == task_id)).all()
        audits = session.scalars(select(TrustAudit).where(TrustAudit.task_id == task_id)).all()
        assert sum(e.kind == "task.verified" for e in events) == 1
        assert sum(a.reason == "verified" for a in audits) == 1


def test_postgres_distinct_concurrent_requests_only_one_completion(
    postgres_store: TaskStore,
) -> None:
    task_id = waiting(postgres_store)
    gate = Barrier(4)

    def check(n: int) -> bool:
        gate.wait()
        try:
            result = IndependentVerifier(postgres_store.engine).verify_dev_add(
                "owner", task_id, f"pg-distinct-{n:04d}"
            )
            return result.conclusion == "supported"
        except StoreError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(check, range(4)))
    assert sum(results) == 1
    assert len(checks(postgres_store, task_id)) == 1
    with Session(postgres_store.engine) as session:
        kinds = session.scalars(
            select(OutboxEvent.kind).where(OutboxEvent.task_id == task_id)
        ).all()
        assert kinds.count("task.verified") == 1


def test_postgres_request_id_is_owner_global_even_between_tasks(postgres_store: TaskStore) -> None:
    first = waiting(postgres_store)
    second = waiting(postgres_store)
    gate = Barrier(2)

    def observe(task_id: str) -> str:
        gate.wait()
        try:
            return (
                IndependentVerifier(postgres_store.engine)
                .verify_dev_add("owner", task_id, "pg-shared-request-001")
                .conclusion
                or "unknown"
            )
        except StoreError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(observe, [first, second]))
    assert sorted(results) == ["conflict", "supported"]
    with Session(postgres_store.engine) as session:
        matches = session.scalars(
            select(VerificationCheck).where(
                VerificationCheck.owner_id == "owner",
                VerificationCheck.request_id == "pg-shared-request-001",
            )
        ).all()
        assert len(matches) == 1


def test_postgres_modified_checkpoint_during_observer_is_stale(
    postgres_store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = waiting(postgres_store)
    entered, release = Event(), Event()
    original = DevAddObserver.observe

    def pause(self: DevAddObserver, snapshot: ObservationSnapshot):
        entered.set()
        assert release.wait(5)
        return original(self, snapshot)

    monkeypatch.setattr(DevAddObserver, "observe", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            IndependentVerifier(postgres_store.engine).verify_dev_add,
            "owner",
            task_id,
            "pg-stale-attempt-001",
        )
        assert entered.wait(5)
        with Session(postgres_store.engine) as session, session.begin():
            task = session.get(Task, task_id)
            assert task is not None
            task.revision += 1
        release.set()
        with pytest.raises(StoreError, match="Stale observer"):
            future.result(timeout=10)
    assert postgres_store.get("owner", task_id).state == "awaiting_verification"
    assert checks(postgres_store, task_id) == []


def test_postgres_readiness_and_historical_legacy_rows_preserved(postgres_store: TaskStore) -> None:
    assert postgres_store.ready()
    with postgres_store.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0003_independent_observer"
        )
    columns = {
        item["name"]
        for item in inspect(postgres_store.engine).get_columns("max_verification_checks")
    }
    for name in (
        "request_id",
        "request_fingerprint",
        "checkpoint_id",
        "observer_version",
        "result_state",
        "result_revision",
        "reason_code",
    ):
        assert name in columns
    # Existing fixture reader still writes the legacy format with nullable new fields.
    task, _ = postgres_store.create(
        "owner", "legacy observer fixture", "pg-legacy-evidence-" + uuid4().hex
    )
    postgres_store.claim("owner", task.id, "legacy-worker")
    artifact = b"legacy evidence"
    postgres_store.record_outcome(
        "owner", task.id, "legacy-worker", hashlib.sha256(artifact).hexdigest()
    )

    class Reader:
        def observe(self, task_id: str) -> bytes:
            assert task_id == task.id
            return artifact

    result = IndependentVerifier(postgres_store.engine, Reader()).verify("owner", task.id)
    assert result.verdict == "verified"
    with Session(postgres_store.engine) as session:
        row = session.scalar(select(VerificationCheck).where(VerificationCheck.task_id == task.id))
        assert row is not None
        assert row.request_id is None
        assert row.checkpoint_id is None
        assert row.observer_id is None
        assert row.verdict == "verified"


def test_postgres_retry_when_commit_lands_between_prior_lookup_and_snapshot(
    postgres_store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministically exercise the READ COMMITTED snapshot-race recovery."""
    task_id = waiting(postgres_store)
    original = IndependentVerifier._snapshot
    committed: list[str] = []

    def commit_before_snapshot(session: object, owner_id: str, target_id: str):
        # Outer caller already observed no prior check; another trusted caller
        # commits the exact same request before outer re-reads task state.
        monkeypatch.setattr(IndependentVerifier, "_snapshot", staticmethod(original))
        first = IndependentVerifier(postgres_store.engine).verify_dev_add(
            owner_id, target_id, "pg-gap-window-001"
        )
        committed.append(first.check_id)
        monkeypatch.setattr(IndependentVerifier, "_snapshot", staticmethod(commit_before_snapshot))
        return original(session, owner_id, target_id)

    monkeypatch.setattr(IndependentVerifier, "_snapshot", staticmethod(commit_before_snapshot))
    restored = IndependentVerifier(postgres_store.engine).verify_dev_add(
        "owner", task_id, "pg-gap-window-001"
    )
    assert restored.check_id == committed[0]
    assert restored.verdict == "verified"
    assert len(checks(postgres_store, task_id)) == 1
