"""Independent-verification test fixtures; a worker cannot verify its own claim."""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.models import OutboxEvent, TrustAudit, VerificationCheck
from max_core.store import StoreError, TaskStore
from max_core.verification import IndependentVerifier

ARTIFACT = b"trusted fixture observed through separate reader"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class FixtureReader:
    def __init__(self, payload: bytes | None) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def observe(self, task_id: str) -> bytes | None:
        self.calls.append(task_id)
        return self.payload


class FailingReader:
    def observe(self, task_id: str) -> bytes | None:
        raise RuntimeError("private reader failure containing secret")


def waiting_task(store: TaskStore, key: str) -> str:
    task, _ = store.create("owner", "Observe effect safely", key)
    store.claim("owner", task.id, "worker-one")
    store.record_outcome("owner", task.id, "worker-one", digest(ARTIFACT))
    return task.id


def test_matching_independent_observation_completes_once(store: TaskStore) -> None:
    task_id = waiting_task(store, "verify-match-001")
    reader = FixtureReader(ARTIFACT)
    result = IndependentVerifier(store.engine, reader).verify("owner", task_id)
    assert result.verdict == "verified"
    assert result.task_state == "completed"
    assert store.get("owner", task_id).state == "completed"
    assert reader.calls == [task_id]
    with pytest.raises(StoreError, match="unavailable for verification"):
        IndependentVerifier(store.engine, reader).verify("owner", task_id)
    with Session(store.engine) as session:
        checks = session.scalars(select(VerificationCheck).where(VerificationCheck.task_id == task_id)).all()
        events = session.scalars(select(OutboxEvent).where(OutboxEvent.task_id == task_id)).all()
        assert len(checks) == 1
        assert checks[0].observed_digest == digest(ARTIFACT)
        assert len([e for e in events if e.kind == "task.verified"]) == 1
        audit = session.scalars(select(TrustAudit).where(TrustAudit.task_id == task_id)).all()
        assert len([x for x in audit if x.reason == "verified"]) == 1


def test_mismatched_worker_claim_is_not_completion(store: TaskStore) -> None:
    task_id = waiting_task(store, "verify-mismatch-001")
    result = IndependentVerifier(store.engine, FixtureReader(b"unrelated")).verify("owner", task_id)
    assert result.verdict == "mismatch"
    assert result.task_state == "awaiting_verification"
    assert store.get("owner", task_id).state == "awaiting_verification"
    with Session(store.engine) as session:
        checks = session.scalars(select(VerificationCheck).where(VerificationCheck.task_id == task_id)).all()
        assert len(checks) == 1 and checks[0].verdict == "mismatch"


@pytest.mark.parametrize("value", [None, b"x" * 1_000_001])
def test_unavailable_or_large_observation_keeps_task_unverified(
    store: TaskStore, value: bytes | None,
) -> None:
    task_id = waiting_task(store, "verify-unavailable-001")
    outcome = IndependentVerifier(store.engine, FixtureReader(value)).verify("owner", task_id)
    assert outcome.verdict == "unavailable"
    assert store.get("owner", task_id).state == "awaiting_verification"


def test_reader_failure_is_sanitized_and_not_completion(store: TaskStore) -> None:
    task_id = waiting_task(store, "verify-exception-001")
    result = IndependentVerifier(store.engine, FailingReader()).verify("owner", task_id)
    assert result.verdict == "unavailable"
    assert "private reader failure" not in repr(result)
    assert store.get("owner", task_id).state == "awaiting_verification"


def test_wrong_owner_and_non_waiting_task_never_contact_reader(store: TaskStore) -> None:
    task, _ = store.create("owner", "Not yet executed", "verify-queued-001")
    reader = FixtureReader(ARTIFACT)
    service = IndependentVerifier(store.engine, reader)
    with pytest.raises(StoreError):
        service.verify("owner", task.id)
    with pytest.raises(StoreError):
        service.verify("not-owner", task.id)
    assert reader.calls == []


def test_verifier_is_internal_not_public_http_capability(store: TaskStore) -> None:
    from fastapi.testclient import TestClient

    from max_core.api import create_app
    from max_core.config import Settings
    from tests.conftest import TEST_TOKEN

    task_id = waiting_task(store, "verify-no-route-001")
    client = TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store=store),
        base_url="http://localhost",
    )
    auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
    assert client.post(f"/v1/tasks/{task_id}/verify", headers=auth).status_code == 404
    assert store.get("owner", task_id).state == "awaiting_verification"


def test_verification_after_mismatch_may_be_reobserved(store: TaskStore) -> None:
    task_id = waiting_task(store, "verify-recheck-001")
    assert IndependentVerifier(store.engine, FixtureReader(b"wrong")).verify("owner", task_id).verdict == "mismatch"
    assert IndependentVerifier(store.engine, FixtureReader(ARTIFACT)).verify("owner", task_id).verdict == "verified"
    with Session(store.engine) as session:
        entries = session.scalars(select(VerificationCheck).where(VerificationCheck.task_id == task_id)).all()
        assert sorted(e.verdict for e in entries) == ["mismatch", "verified"]


def test_missing_task_has_no_verifier_effect(store: TaskStore) -> None:
    reader = FixtureReader(ARTIFACT)
    with pytest.raises(StoreError):
        IndependentVerifier(store.engine, reader).verify("owner", str(uuid4()))
    assert reader.calls == []
