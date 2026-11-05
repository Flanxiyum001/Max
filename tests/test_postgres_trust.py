"""PostgreSQL row-lock acceptance tests for approval and independent verification."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.gateway.contracts import ToolProposal
from max_core.models import ApprovalGrant, OutboxEvent, TrustAudit, VerificationCheck
from max_core.store import StoreError, TaskStore
from max_core.trust import ActorContext, TrustError, TrustService
from max_core.verification import IndependentVerifier


def test_postgres_concurrent_approval_consume_only_one(
    postgres_store: TaskStore,
) -> None:
    task, _ = postgres_store.create("owner", "Parallel approval", "trust-pg-" + uuid4().hex)
    scope = ActorContext("owner", task.id, "worker-1", "session-1", "device-1")
    action = ToolProposal("tool-001", "task.status", {"task_id": task.id})
    service = TrustService(postgres_store.engine)
    grant = service.request(scope, action)
    service.decide("owner", grant.id, True)

    def invoke(_index: int) -> bool:
        try:
            service.execute(scope, grant.id, action)
            return True
        except TrustError:
            return False

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(invoke, range(5)))
    assert sum(results) == 1
    with Session(postgres_store.engine) as session:
        row = session.get(ApprovalGrant, grant.id)
        assert row is not None and row.state == "consumed"
        audits = session.scalars(select(TrustAudit).where(TrustAudit.approval_id == grant.id)).all()
        assert len([a for a in audits if a.action == "execute" and a.decision == "allow"]) == 1


def test_postgres_concurrent_verify_only_one_completion(
    postgres_store: TaskStore,
) -> None:
    artifact = b"trusted independently observed effect for PostgreSQL integration"
    evidence = hashlib.sha256(artifact).hexdigest()
    task, _ = postgres_store.create("owner", "Parallel verification", "verify-pg-" + uuid4().hex)
    postgres_store.claim("owner", task.id, "worker")
    postgres_store.record_outcome("owner", task.id, "worker", evidence)

    class Reader:
        def observe(self, task_id: str) -> bytes:
            assert task_id == task.id
            return artifact

    verifier = IndependentVerifier(postgres_store.engine, Reader())

    def check(_index: int) -> bool:
        try:
            return verifier.verify("owner", task.id).verdict == "verified"
        except StoreError:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(check, range(4)))
    assert sum(results) == 1
    assert postgres_store.get("owner", task.id).state == "completed"
    with Session(postgres_store.engine) as session:
        checks = session.scalars(
            select(VerificationCheck).where(VerificationCheck.task_id == task.id)
        ).all()
        assert len(checks) == 1
        events = session.scalars(select(OutboxEvent).where(OutboxEvent.task_id == task.id)).all()
        assert len([e for e in events if e.kind == "task.verified"]) == 1


def test_postgres_ready_requires_migration_two(postgres_store: TaskStore) -> None:
    assert postgres_store.ready() is True
