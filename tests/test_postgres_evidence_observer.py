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
from max_core.models import OutboxEvent, Task, TaskCheckpoint, TrustAudit, VerificationCheck
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


def test_postgres_upgrade_0002_to_0003_preserves_actual_historical_row(
    postgres_store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrate an isolated temporary schema containing a PRE-0003 check."""
    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command

    schema = "max_observer_test_" + uuid4().hex
    with postgres_store.engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    schema_url = postgres_store.engine.url.set(query={"options": f"-csearch_path={schema}"})
    monkeypatch.setenv("MAX_DATABASE_URL", schema_url.render_as_string(hide_password=False))
    engine = create_engine(schema_url)
    task_id = str(uuid4())
    check_id = str(uuid4())
    try:
        command.upgrade(Config("alembic.ini"), "0002_trust_verification")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO max_tasks (id,owner_id,objective,objective_hash,"
                    "idempotency_key,state,revision,created_at,updated_at) "
                    "VALUES (:id,'owner','legacy test',:hash,:key,'completed',5,now(),now())"
                ),
                {
                    "id": task_id,
                    "hash": hashlib.sha256(b"legacy test").hexdigest(),
                    "key": "migration-legacy-001",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO max_verification_checks "
                    "(id,owner_id,task_id,observed_digest,expected_digest,verdict,checked_at) "
                    "VALUES (:id,'owner',:task,:digest,:digest,'verified',now())"
                ),
                {
                    "id": check_id,
                    "task": task_id,
                    "digest": hashlib.sha256(b"evidence").hexdigest(),
                },
            )
        command.upgrade(Config("alembic.ini"), "0003_independent_observer")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id,task_id,verdict,request_id,checkpoint_id,input_digest "
                    "FROM max_verification_checks WHERE id=:id"
                ),
                {"id": check_id},
            ).one()
            assert row.id == check_id
            assert row.task_id == task_id
            assert row.verdict == "verified"
            assert row.request_id is None
            assert row.checkpoint_id is None
            assert row.input_digest is None
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0003_independent_observer"
            )
    finally:
        engine.dispose()
        with postgres_store.engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_postgres_exact_replay_accepts_unchanged_checkpoint(postgres_store: TaskStore) -> None:
    task_id = waiting(postgres_store)
    request_id = "pg-checkpoint-integrity-001"
    verifier = IndependentVerifier(postgres_store.engine)
    first = verifier.verify_dev_add("owner", task_id, request_id)
    assert first.verdict == "verified"
    assert first.conclusion == "supported"
    assert first.task_state == "completed"

    with Session(postgres_store.engine) as session:
        check = session.scalar(
            select(VerificationCheck).where(
                VerificationCheck.task_id == task_id,
                VerificationCheck.request_id == request_id,
            )
        )
        assert check is not None
        cp = session.get(TaskCheckpoint, check.checkpoint_id)
        assert cp is not None
        assert cp.task_id == task_id
        assert cp.operation_key == "dev-operation-v1"
        assert cp.step_name == check.operation_id == "dev.add"
        assert cp.payload_digest == check.input_digest
        event_keys = session.scalars(
            select(OutboxEvent.event_key).where(OutboxEvent.task_id == task_id)
        ).all()

    # Exact replay returns the historical check and state, not a second decision.
    second = verifier.verify_dev_add("owner", task_id, request_id)
    assert second == first
    assert [row.id for row in checks(postgres_store, task_id)] == [first.check_id]
    with Session(postgres_store.engine) as session:
        replay_event_keys = session.scalars(
            select(OutboxEvent.event_key).where(OutboxEvent.task_id == task_id)
        ).all()
        assert replay_event_keys == event_keys


@pytest.mark.parametrize(
    "mutation",
    [
        "operation_key",
        "step_name",
        "payload_digest",
        "other_task_checkpoint",
        "deleted_checkpoint",
    ],
)
def test_postgres_exact_replay_refuses_tampered_checkpoint_metadata(
    postgres_store: TaskStore, mutation: str
) -> None:
    """Tampering after completion cannot reuse the old verified decision."""
    task_id = waiting(postgres_store)
    request_id = "pg-checkpoint-tamper-" + uuid4().hex
    verifier = IndependentVerifier(postgres_store.engine)
    supported = verifier.verify_dev_add("owner", task_id, request_id)
    assert supported.conclusion == "supported"
    assert supported.verdict == "verified"
    assert supported.task_state == "completed"

    with Session(postgres_store.engine) as session:
        keys_before = session.scalars(
            select(OutboxEvent.event_key).where(OutboxEvent.task_id == task_id)
        ).all()
        rows_before = session.scalars(
            select(VerificationCheck.id).where(VerificationCheck.task_id == task_id)
        ).all()
    assert rows_before == [supported.check_id]
    assert len(keys_before) >= 1

    other_id = waiting(postgres_store) if mutation == "other_task_checkpoint" else None
    with Session(postgres_store.engine) as session, session.begin():
        saved = session.get(VerificationCheck, supported.check_id)
        assert saved is not None
        assert saved.checkpoint_id is not None
        cp = session.get(TaskCheckpoint, saved.checkpoint_id)
        assert cp is not None and cp.task_id == task_id
        if mutation == "operation_key":
            cp.operation_key = "dev-operation-v2"
        elif mutation == "step_name":
            cp.step_name = "dev.health"
        elif mutation == "payload_digest":
            cp.payload_digest = "0" * 64
        elif mutation == "deleted_checkpoint":
            session.delete(cp)
        else:
            assert other_id is not None
            other = session.get(Task, other_id)
            current = session.get(Task, task_id)
            assert other is not None and other.last_checkpoint_id is not None
            assert current is not None
            # A tampered verification check and task both point at a real
            # checkpoint belonging to another task. Equal IDs alone must
            # not satisfy the replay binding.
            saved.checkpoint_id = other.last_checkpoint_id
            current.last_checkpoint_id = other.last_checkpoint_id

    with pytest.raises(StoreError) as exc:
        verifier.verify_dev_add("owner", task_id, request_id)
    assert exc.value.status_code == 409
    assert str(exc.value) == "Observer request binding changed"

    with Session(postgres_store.engine) as session:
        existing = session.get(VerificationCheck, supported.check_id)
        assert existing is not None
        assert existing.verdict == "verified"
        assert existing.reason_code == supported.reason_code
        assert session.scalars(
            select(VerificationCheck.id).where(VerificationCheck.task_id == task_id)
        ).all() == rows_before
        assert session.scalars(
            select(OutboxEvent.event_key).where(OutboxEvent.task_id == task_id)
        ).all() == keys_before
        assert session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.task_id == task_id,
                OutboxEvent.kind == "task.verified",
            )
        ) is not None
    assert postgres_store.get("owner", task_id).state == "completed"
