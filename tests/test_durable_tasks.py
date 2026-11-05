"""Durability, idempotency, negative authorization and worker-recovery tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from max_core.api import create_app
from max_core.config import Settings
from max_core.models import Base, OutboxEvent, Task, utc_now
from max_core.store import StoreError, TaskStore, _is_expired
from tests.conftest import TEST_TOKEN


@pytest.fixture
def store(tmp_path: object) -> TaskStore:
    from pathlib import Path

    engine = create_engine(f"sqlite+pysqlite:///{Path(str(tmp_path)) / 'max-test.sqlite3'}")
    Base.metadata.create_all(engine)
    return TaskStore(engine)


@pytest.fixture
def durable_client(store: TaskStore) -> TestClient:
    return TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store=store), base_url="http://localhost"
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}", "Idempotency-Key": "sample-create-001"}


def _hash(data: bytes = b"observed local output") -> str:
    return sha256(data).hexdigest()


def test_persistent_post_replay_returns_same_task_and_no_duplicate_event(
    durable_client: TestClient, store: TaskStore
) -> None:
    headers = _auth()
    created = durable_client.post(
        "/v1/tasks", json={"objective": "Run safe checks"}, headers=headers
    )
    assert created.status_code == 201
    duplicate = durable_client.post(
        "/v1/tasks", json={"objective": "Run safe checks"}, headers=headers
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == created.json()["id"]
    assert store.get("owner", created.json()["id"]).state == "queued"
    with Session(store.engine) as session:
        assert len(session.scalars(select(OutboxEvent)).all()) == 1


def test_idempotency_key_with_different_body_is_conflict(durable_client: TestClient) -> None:
    headers = _auth()
    assert (
        durable_client.post("/v1/tasks", headers=headers, json={"objective": "First"}).status_code
        == 201
    )
    conflict = durable_client.post("/v1/tasks", headers=headers, json={"objective": "Second"})
    assert conflict.status_code == 409
    assert "Idempotency key" in conflict.json()["detail"]


@pytest.mark.parametrize("value", ["short", "bad key with space", "invalid@symbol"])
def test_bad_idempotency_keys_are_rejected(durable_client: TestClient, value: str) -> None:
    h = {**_auth(), "Idempotency-Key": value}
    response = durable_client.post("/v1/tasks", json={"objective": "Test"}, headers=h)
    assert response.status_code == 422


def test_no_unauthed_task_creation_or_read(durable_client: TestClient) -> None:
    assert durable_client.post("/v1/tasks", json={"objective": "Not allowed"}).status_code == 401
    assert durable_client.get("/v1/tasks").status_code == 401
    assert durable_client.post(f"/v1/tasks/{uuid4()}/cancel").status_code == 401


def test_persistence_survives_second_store_and_app(store: TaskStore) -> None:
    task, _ = store.create("owner", "Persistent test", "persist-test-001")
    another = TaskStore(store.engine)
    assert another.get("owner", task.id).id == task.id
    client = TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store=another),
        base_url="http://localhost",
    )
    assert client.get(f"/v1/tasks/{task.id}", headers=_auth()).json()["state"] == "queued"
    assert client.get("/v1/tasks", headers=_auth()).json()[0]["id"] == task.id


def test_cross_owner_does_not_disclose_task(store: TaskStore) -> None:
    task, _ = store.create("owner", "Private objective", "private-objective-001")
    with pytest.raises(StoreError) as err:
        store.get("different-owner", task.id)
    assert err.value.status_code == 404
    with pytest.raises(StoreError):
        store.cancel("different-owner", task.id)


def test_cancel_queued_is_atomic_idempotent_and_emits_one_event(store: TaskStore) -> None:
    task, _ = store.create("owner", "Cancel this", "cancel-queued-001")
    cancelled = store.cancel("owner", task.id)
    assert cancelled.state == "cancelled"
    assert store.cancel("owner", task.id).revision == cancelled.revision
    with Session(store.engine) as session:
        events = session.scalars(select(OutboxEvent).where(OutboxEvent.task_id == task.id)).all()
        assert [e.kind for e in events] == ["task.queued", "task.cancelled"]
    with pytest.raises(StoreError):
        store.claim("owner", task.id, "worker-one")


def test_running_cancel_prevents_new_checkpoint_or_outcome(store: TaskStore) -> None:
    task, _ = store.create("owner", "Long task", "running-cancel-001")
    store.claim("owner", task.id, "worker-one")
    assert store.cancel("owner", task.id).state == "cancelling"
    with pytest.raises(StoreError):
        store.checkpoint("owner", task.id, "worker-one", "step-key-001", "safe-step", _hash())
    with pytest.raises(StoreError):
        store.record_outcome("owner", task.id, "worker-one", _hash())


def test_checkpoint_replay_and_conflict(store: TaskStore) -> None:
    task, _ = store.create("owner", "Checkpoint", "checkpoint-task-001")
    store.claim("owner", task.id, "worker-one")
    saved = store.checkpoint("owner", task.id, "worker-one", "checkpoint-op-001", "step-a", _hash())
    assert saved.last_checkpoint_id
    assert (
        store.checkpoint(
            "owner", task.id, "worker-one", "checkpoint-op-001", "step-a", _hash()
        ).revision
        == saved.revision
    )
    with pytest.raises(StoreError):
        store.checkpoint("owner", task.id, "worker-one", "checkpoint-op-001", "step-b", _hash())


def test_stale_or_wrong_worker_cannot_checkpoint(store: TaskStore) -> None:
    task, _ = store.create("owner", "Worker lease", "worker-lease-001")
    store.claim("owner", task.id, "worker-one")
    with pytest.raises(StoreError):
        store.checkpoint("owner", task.id, "other-worker", "operation-001", "one", _hash())
    with Session(store.engine) as session, session.begin():
        record = session.get(Task, task.id)
        assert record
        record.lease_until = utc_now() - timedelta(minutes=1)
    with pytest.raises(StoreError):
        store.checkpoint("owner", task.id, "worker-one", "operation-002", "one", _hash())


def test_expired_worker_is_quarantined_not_automatically_retried(store: TaskStore) -> None:
    task, _ = store.create("owner", "Potential side effect", "reconcile-task-001")
    store.claim("owner", task.id, "worker-one")
    with Session(store.engine) as session, session.begin():
        record = session.get(Task, task.id)
        assert record
        record.lease_until = utc_now() - timedelta(minutes=2)
    assert store.reconcile_expired("owner") == 1
    assert store.reconcile_expired("owner") == 0
    assert store.get("owner", task.id).state == "needs_reconciliation"
    with pytest.raises(StoreError):
        store.claim("owner", task.id, "worker-two")


def test_worker_observation_is_not_marked_verified(store: TaskStore) -> None:
    task, _ = store.create("owner", "Observe only", "observation-001")
    store.claim("owner", task.id, "worker-one")
    view = store.record_outcome("owner", task.id, "worker-one", _hash())
    assert view.state == "awaiting_verification"
    with pytest.raises(StoreError):
        store.cancel("owner", task.id)


def test_outbox_lease_ack_replay_and_wrong_token(store: TaskStore) -> None:
    task, _ = store.create("owner", "Outbox", "outbox-task-001")
    assert task
    leased = store.lease_outbox()
    assert len(leased) == 1
    assert store.lease_outbox() == []
    assert not store.ack_outbox(leased[0].id, "not-the-token")
    assert store.ack_outbox(leased[0].id, leased[0].claim_token)
    assert not store.ack_outbox(leased[0].id, leased[0].claim_token)
    assert store.lease_outbox() == []


def test_outbox_expired_claim_can_be_released_to_new_worker(store: TaskStore) -> None:
    store.create("owner", "Outbox lease", "outbox-recovery-001")
    first = store.lease_outbox()[0]
    with Session(store.engine) as session, session.begin():
        event = session.get(OutboxEvent, first.id)
        assert event
        event.lease_until = utc_now() - timedelta(minutes=1)
    second = store.lease_outbox()[0]
    assert second.id == first.id
    assert second.claim_token != first.claim_token
    assert not store.ack_outbox(first.id, first.claim_token)
    assert store.ack_outbox(second.id, second.claim_token)


def test_concurrent_duplicate_create_is_atomic(store: TaskStore) -> None:
    def send(_index: int) -> str:
        view, _ = store.create("owner", "Concurrent", "concurrent-create-001")
        return view.id

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(send, range(4)))
    assert len(set(ids)) == 1
    with Session(store.engine) as session:
        assert len(session.scalars(select(OutboxEvent)).all()) == 1


def test_without_db_task_routes_return_503_but_auth_still_required(client: TestClient) -> None:
    assert client.post("/v1/tasks", json={"objective": "one"}).status_code == 401
    response = client.post(
        "/v1/tasks",
        json={"objective": "one"},
        headers=_auth(),
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_readiness_does_not_claim_sqlite_is_production_migrated(
    durable_client: TestClient,
) -> None:
    assert durable_client.get("/health/ready").status_code == 503


def test_expiry_conversion_respects_offset_aware_datetimes() -> None:
    future = (utc_now() + timedelta(minutes=2)).astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert not _is_expired(future)
    past = (utc_now() - timedelta(minutes=2)).astimezone(timezone(timedelta(hours=-4)))
    assert _is_expired(past)
