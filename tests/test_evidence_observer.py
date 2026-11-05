"""Restricted independent dev.add observer and existing trusted verifier integration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core import bounded_worker, evidence_observer, verification
from max_core.bounded_worker import BoundedWorker
from max_core.evidence_observer import DevAddObserver, ObservationSnapshot
from max_core.models import OutboxEvent, Task, TaskCheckpoint, TrustAudit, VerificationCheck
from max_core.store import StoreError, TaskStore
from max_core.verification import IndependentVerifier

_OWNER = "owner"


def _objective(a: object = 2, b: object = 3) -> str:
    return json.dumps({"op": "dev.add", "params": {"a": a, "b": b}}, separators=(",", ":"))


def _digest(a: int = 2, b: int = 3) -> str:
    raw = json.dumps(
        {"operation": "dev.add", "result": a + b},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _record(
    store: TaskStore,
    *,
    objective: str | None = None,
    claim_digest: str | None = None,
    checkpoint_step: str = "dev.add",
) -> str:
    text = _objective() if objective is None else objective
    task, _ = store.create(_OWNER, text, "observer-" + uuid4().hex)
    store.claim(_OWNER, task.id, "fixture-worker")
    store.checkpoint(
        _OWNER,
        task.id,
        "fixture-worker",
        "dev-operation-v1",
        checkpoint_step,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    store.record_outcome(_OWNER, task.id, "fixture-worker", claim_digest or _digest())
    return task.id


def _verify(store: TaskStore, task_id: str, request_id: str = "observe-test-001"):
    return IndependentVerifier(store.engine).verify_dev_add(_OWNER, task_id, request_id)


def test_real_worker_claim_is_independently_recomputed(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _ = store.create(_OWNER, _objective(12, 30), "observer-real-worker-001")
    worker = BoundedWorker(store).run_one(task.id)
    assert worker.state == "awaiting_verification"

    def forbidden(*_: object, **__: object) -> bytes:
        raise AssertionError("verification re-executed worker code")

    monkeypatch.setattr(bounded_worker, "execute_operation", forbidden)
    result = _verify(store, task.id)
    assert (result.conclusion, result.verdict, result.task_state) == (
        "supported", "verified", "completed"
    )
    assert "correctness" not in (result.reason_code or "")
    assert store.get(_OWNER, task.id).state == "completed"
    with Session(store.engine) as session:
        checks = session.scalars(
            select(VerificationCheck).where(VerificationCheck.task_id == task.id)
        ).all()
        assert len(checks) == 1
        check = checks[0]
        assert check.observed_digest == worker.outcome_digest
        assert check.request_id == "observe-test-001"
        assert check.task_revision == 4
        assert check.result_revision == 5
        assert check.result_state == "completed"
        assert check.observer_id == evidence_observer.OBSERVER_ID
        assert check.source_kind == evidence_observer.SOURCE_KIND
        events = session.scalars(select(OutboxEvent).where(OutboxEvent.task_id == task.id)).all()
        assert [e.kind for e in events].count("task.verified") == 1
        assert max(e.task_revision for e in events) == 5
        audits = session.scalars(select(TrustAudit).where(TrustAudit.task_id == task.id)).all()
        assert len([a for a in audits if a.reason == "verified"]) == 1


def test_wrong_worker_digest_is_contradicted_without_completion(store: TaskStore) -> None:
    task_id = _record(store, claim_digest=hashlib.sha256(b"incorrect").hexdigest())
    report = _verify(store, task_id)
    assert report.conclusion == "contradicted"
    assert report.verdict == "mismatch"
    assert report.task_state == "awaiting_verification"
    assert report.reason_code == "expected_result_differs_from_claim"
    assert store.get(_OWNER, task_id).state == "awaiting_verification"
    with Session(store.engine) as session:
        check = session.scalar(
            select(VerificationCheck).where(VerificationCheck.task_id == task_id)
        )
        assert check is not None and check.observed_digest == _digest()
        kinds = session.scalars(select(OutboxEvent.kind).where(OutboxEvent.task_id == task_id)).all()
        assert "task.verified" not in kinds


@pytest.mark.parametrize(
    "invalid",
    [
        "non-json",
        '{"op":"dev.add","params":{"a":1,"a":2,"b":3}}',
        _objective(True, 2),
        _objective(1_000_001, 0),
        _objective(1.5, 1),
        '{"op":"dev.add","params":{"a":1,"b":2,"other":3}}',
        '{"op":"dev.add","params":{"a":NaN,"b":2}}',
        '{"op":"dev.add","params":{"a":1}}',
        '{"op":"shell.run","params":{}}',
        '{"op":"dev.health","params":{}}',
        "x" * 500,
    ],
)
def test_invalid_or_unsupported_input_never_completes(
    store: TaskStore, invalid: str
) -> None:
    step = "dev.health" if '"dev.health"' in invalid else "dev.add"
    task_id = _record(store, objective=invalid, checkpoint_step=step)
    result = _verify(store, task_id)
    assert result.conclusion == "inconclusive"
    assert result.verdict == "unavailable"
    assert store.get(_OWNER, task_id).state == "awaiting_verification"


def test_missing_worker_digest_or_checkpoint_refuses_verification(store: TaskStore) -> None:
    task_id = _record(store)
    with Session(store.engine) as session, session.begin():
        task = session.get(Task, task_id)
        assert task is not None
        task.evidence_digest = None
    with pytest.raises(StoreError, match="worker result claim"):
        _verify(store, task_id)
    assert store.get(_OWNER, task_id).state == "awaiting_verification"
    with Session(store.engine) as session, session.begin():
        task = session.get(Task, task_id)
        assert task is not None
        task.evidence_digest = _digest()
        task.last_checkpoint_id = None
    with pytest.raises(StoreError, match="checkpoint is missing"):
        _verify(store, task_id)


def test_checkpoint_belongs_to_task_and_payload(store: TaskStore) -> None:
    first = _record(store)
    second = _record(store)
    with Session(store.engine) as session, session.begin():
        task = session.get(Task, first)
        other = session.get(Task, second)
        assert task is not None and other is not None
        task.last_checkpoint_id = other.last_checkpoint_id
    with pytest.raises(StoreError, match="checkpoint is invalid"):
        _verify(store, first)
    assert store.get(_OWNER, first).state == "awaiting_verification"


def test_objective_integrity_hash_detects_tampering(store: TaskStore) -> None:
    task_id = _record(store)
    with Session(store.engine) as session, session.begin():
        task = session.get(Task, task_id)
        assert task is not None
        task.objective = _objective(91, 3)
    with pytest.raises(StoreError, match="input integrity"):
        _verify(store, task_id)
    assert store.get(_OWNER, task_id).state == "awaiting_verification"


@pytest.mark.parametrize("field", ["revision", "objective", "evidence_digest", "last_checkpoint_id"])
def test_changed_snapshot_rejected_before_commit(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    task_id = _record(store)
    original = DevAddObserver.observe

    def mutate(self: DevAddObserver, snap: ObservationSnapshot):
        observation = original(self, snap)
        with Session(store.engine) as session, session.begin():
            task = session.get(Task, task_id)
            assert task is not None
            if field == "revision":
                task.revision += 1
            elif field == "objective":
                task.objective = _objective(8, 9)
            elif field == "evidence_digest":
                task.evidence_digest = hashlib.sha256(b"changed").hexdigest()
            else:
                task.last_checkpoint_id = str(uuid4())
        return observation

    monkeypatch.setattr(DevAddObserver, "observe", mutate)
    with pytest.raises(StoreError, match="Stale observer"):
        _verify(store, task_id)
    with Session(store.engine) as session:
        assert session.scalars(
            select(VerificationCheck).where(VerificationCheck.task_id == task_id)
        ).all() == []
        assert not session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.task_id == task_id, OutboxEvent.kind == "task.verified"
            )
        ).all()


def test_checkpoint_metadata_mutation_rejected(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _record(store)
    original = DevAddObserver.observe

    def mutate(self: DevAddObserver, snap: ObservationSnapshot):
        observed = original(self, snap)
        with Session(store.engine) as session, session.begin():
            cp = session.get(TaskCheckpoint, snap.checkpoint_id)
            assert cp is not None
            cp.step_name = "dev.health"
        return observed

    monkeypatch.setattr(DevAddObserver, "observe", mutate)
    with pytest.raises(StoreError, match="Stale observer"):
        _verify(store, task_id)
    assert store.get(_OWNER, task_id).state == "awaiting_verification"


def test_same_request_is_exact_replay_and_new_request_is_not(store: TaskStore) -> None:
    task_id = _record(store)
    first = _verify(store, task_id, "observer-replay-001")
    second = _verify(store, task_id, "observer-replay-001")
    assert first == second
    with pytest.raises(StoreError, match="unavailable for observation"):
        _verify(store, task_id, "observer-new-002")
    with Session(store.engine) as session:
        checks = session.scalars(select(VerificationCheck).where(VerificationCheck.task_id == task_id)).all()
        assert len(checks) == 1
        assert checks[0].request_fingerprint == IndependentVerifier._fingerprint(_OWNER, task_id)


def test_same_request_id_changed_task_conflicts(store: TaskStore) -> None:
    first = _record(store)
    second = _record(store)
    _verify(store, first, "observer-shared-001")
    with pytest.raises(StoreError, match="idempotency conflict"):
        _verify(store, second, "observer-shared-001")
    assert store.get(_OWNER, second).state == "awaiting_verification"


def test_same_request_id_changed_observer_version_conflicts(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _record(store)
    _verify(store, task_id, "observer-version-001")
    monkeypatch.setattr(evidence_observer, "OBSERVER_VERSION", "2")
    with pytest.raises(StoreError, match="idempotency conflict"):
        _verify(store, task_id, "observer-version-001")


def test_prior_mismatch_replay_after_later_success_preserves_original_decision(
    store: TaskStore
) -> None:
    task_id = _record(store, claim_digest=hashlib.sha256(b"bad").hexdigest())
    mismatch = _verify(store, task_id, "observer-mismatch-001")
    # A distinct request with same authoritative source cannot turn the wrong claim
    # into a match, so a later success here requires a test-only DB modification.
    # The old exact replay must fail closed if the claim has been changed.
    with Session(store.engine) as session, session.begin():
        task = session.get(Task, task_id)
        assert task is not None
        task.evidence_digest = _digest()
    with pytest.raises(StoreError, match="binding changed"):
        _verify(store, task_id, "observer-mismatch-001")
    assert mismatch.verdict == "mismatch"


def test_missing_reader_does_not_allow_legacy_verify(store: TaskStore) -> None:
    task_id = _record(store)
    with pytest.raises(StoreError, match="not configured"):
        IndependentVerifier(store.engine).verify(_OWNER, task_id)


def test_wrong_owner_and_terminal_or_reconciliation_rejected(store: TaskStore) -> None:
    waiting = _record(store)
    with pytest.raises(StoreError):
        IndependentVerifier(store.engine).verify_dev_add("foreign", waiting, "foreign-obs-001")
    assert store.get(_OWNER, waiting).state == "awaiting_verification"
    _verify(store, waiting)
    with pytest.raises(StoreError):
        _verify(store, waiting, "after-complete-001")
    cancelled, _ = store.create(_OWNER, _objective(), "cancelled-obs-001")
    store.cancel(_OWNER, cancelled.id)
    with pytest.raises(StoreError):
        _verify(store, cancelled.id, "cancelled-obs-002")
    recon, _ = store.create(_OWNER, _objective(), "recon-obs-001")
    store.claim(_OWNER, recon.id, "recon-worker")
    store.quarantine_claim(_OWNER, recon.id, "recon-worker")
    with pytest.raises(StoreError):
        _verify(store, recon.id, "recon-obs-002")


def test_crash_before_transaction_commit_allows_exact_request_retry(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _record(store)
    original = IndependentVerifier._still_bound

    def crash(session: object, snapshot: ObservationSnapshot) -> bool:
        raise RuntimeError("INTERNAL-SECRET-DO-NOT-LOG")

    monkeypatch.setattr(IndependentVerifier, "_still_bound", staticmethod(crash))
    with pytest.raises(RuntimeError):
        _verify(store, task_id, "observer-crash-001")
    with Session(store.engine) as session:
        checks = session.scalars(select(VerificationCheck).where(VerificationCheck.task_id == task_id)).all()
        assert checks == []
    monkeypatch.setattr(IndependentVerifier, "_still_bound", staticmethod(original))
    first = _verify(store, task_id, "observer-crash-001")
    assert first.conclusion == "supported"
    assert first == _verify(store, task_id, "observer-crash-001")


def test_verifier_fails_closed_on_reader_exception(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _record(store)

    def crash(*args: object) -> None:
        raise RuntimeError("secret error")

    monkeypatch.setattr(DevAddObserver, "observe", crash)
    result = _verify(store, task_id)
    assert result.verdict == "unavailable"
    assert result.reason_code == "observer_unavailable"
    assert store.get(_OWNER, task_id).state == "awaiting_verification"
    assert "secret" not in repr(result)


def test_direct_observer_has_no_worker_execution_path(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _record(store)
    with Session(store.engine) as session:
        snapshot = IndependentVerifier._snapshot(session, _OWNER, task_id)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("forbidden I/O or worker invocation")

    monkeypatch.setattr(bounded_worker, "execute_operation", forbidden)
    import builtins
    import os
    import socket
    import subprocess

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    result = DevAddObserver().observe(snapshot)
    assert result.conclusion == "supported"
    assert result.observed_digest == _digest()


def test_cli_sensitive_values_not_exposed(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _record(store)
    from contextlib import nullcontext

    original_client = evidence_observer.create_store
    monkeypatch.setenv("MAX_API_BEARER_TOKEN", "test-secret-" + "0123456789abcdef" * 3)
    monkeypatch.setenv("MAX_DATABASE_URL", "postgresql+psycopg://SECRET_PASSWORD@127.0.0.1/test")
    monkeypatch.setattr(evidence_observer, "create_store", lambda url: store)
    monkeypatch.setattr(store, "ready", lambda: True)
    result = evidence_observer.main(["verify", task_id, "--request-id", "observer-cli-001"])
    assert result == 0
    captured = capsys.readouterr()
    assert "supported" in captured.out
    assert "SECRET_PASSWORD" not in captured.out + captured.err
    assert "test-secret-" not in captured.out + captured.err
    assert _objective() not in captured.out + captured.err


def test_invalid_request_id_never_creates_check(store: TaskStore) -> None:
    task_id = _record(store)
    with pytest.raises(StoreError, match="Invalid observer"):
        _verify(store, task_id, "bad key")
    with Session(store.engine) as session:
        assert session.scalars(
            select(VerificationCheck).where(VerificationCheck.task_id == task_id)
        ).all() == []
