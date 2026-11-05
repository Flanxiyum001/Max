"""Independent observation gate for a worker's untrusted completion assertion.

A trusted host supplies EvidenceReader, NOT the worker/model's claimed hash.
No filesystem, network, external tool or active worker is implemented here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from max_core import evidence_observer
from max_core.models import Task, TaskCheckpoint, VerificationCheck
from max_core.store import StoreError, _advance, _event
from max_core.trust import _audit

_MAX_EVIDENCE_BYTES = 1_000_000


class EvidenceReader(Protocol):
    """Implemented by a separately trusted observer with scoped, read-only access."""

    def observe(self, task_id: str) -> bytes | None: ...


@dataclass(frozen=True, slots=True)
class VerificationResult:
    task_id: str
    verdict: str
    task_state: str
    task_revision: int
    check_id: str
    reason_code: str | None = None
    conclusion: str | None = None


class IndependentVerifier:
    """Internal-only finalization; never call this from a model-supplied tool proposal."""

    def __init__(self, engine: Engine, reader: EvidenceReader | None = None) -> None:
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.reader = reader

    def verify(self, owner_id: str, task_id: str) -> VerificationResult:
        if self.reader is None:
            raise StoreError("Independent evidence reader not configured", 409)
        # Check ownership and state *before* any observer may learn a task identifier.
        with self.sessions() as session:
            allowed = session.scalar(
                select(Task.id).where(
                    Task.id == task_id,
                    Task.owner_id == owner_id,
                    Task.state == "awaiting_verification",
                )
            )
            if allowed is None:
                raise StoreError("Task unavailable for verification", 404)

        # The independent source is injected by trusted host code. A returned digest
        # provided by the worker is NOT accepted; hash independently read bytes.
        try:
            observed = self.reader.observe(task_id)
        except Exception:
            observed = None  # Do not expose observer internals, paths, or credentials.
        if not isinstance(observed, bytes) or len(observed) > _MAX_EVIDENCE_BYTES:
            observed = None
        observed_hash = hashlib.sha256(observed).hexdigest() if observed is not None else None

        with self.sessions.begin() as session:
            task = session.scalar(
                select(Task).where(Task.id == task_id, Task.owner_id == owner_id).with_for_update()
            )
            if task is None or task.state != "awaiting_verification":
                raise StoreError("Task unavailable for verification", 409)
            expected = task.evidence_digest
            if expected is None:
                raise StoreError("No worker evidence claim to verify", 409)
            if observed_hash is None:
                verdict = "unavailable"
            elif observed_hash == expected:
                verdict = "verified"
            else:
                verdict = "mismatch"
            check = VerificationCheck(
                id=str(uuid4()),
                owner_id=owner_id,
                task_id=task.id,
                observed_digest=observed_hash,
                expected_digest=expected,
                verdict=verdict,
            )
            session.add(check)
            _audit(
                session,
                owner_id,
                task.id,
                None,
                "verification",
                "allow" if verdict == "verified" else "deny",
                verdict,
            )
            if verdict == "verified":
                _advance(task, "completed")
                _event(session, task, "task.verified")
            session.flush()
            return VerificationResult(task.id, verdict, task.state, task.revision, check.id)

    @staticmethod
    def _fingerprint(owner_id: str, task_id: str) -> str:
        """Stable request parameters. No worker bytes, task revisions or secrets."""
        canonical = json.dumps(
            {
                "owner": owner_id,
                "task": task_id,
                "operation": "dev.add",
                "observer": evidence_observer.OBSERVER_ID,
                "version": evidence_observer.OBSERVER_VERSION,
                "source": evidence_observer.SOURCE_KIND,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    @staticmethod
    def _prior(
        session: Any, owner_id: str, task_id: str, request_id: str, fingerprint: str
    ) -> VerificationResult | None:
        """Replay only the same owner's exact prior request and intact input binding."""
        prior = session.scalar(
            select(VerificationCheck).where(
                VerificationCheck.owner_id == owner_id,
                VerificationCheck.request_id == request_id,
            )
        )
        if prior is None:
            return None
        if prior.task_id != task_id or prior.request_fingerprint != fingerprint:
            raise StoreError("Observer request idempotency conflict", 409)
        # Revalidate the *row*, not just the checkpoint UUID. Its task
        # association provides the owner boundary through the locked task.
        # Only new observer decisions have a request ID; legacy fixture-reader
        # checks are unaffected.
        task = session.scalar(
            select(Task)
            .where(Task.id == task_id, Task.owner_id == owner_id)
            .with_for_update()
        )
        checkpoint = (
            session.scalar(
                select(TaskCheckpoint)
                .where(
                    TaskCheckpoint.id == prior.checkpoint_id,
                    TaskCheckpoint.task_id == task_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task is not None and prior.checkpoint_id is not None
            else None
        )
        if (
            task is None
            or prior.input_digest is None
            or hashlib.sha256(task.objective.encode("utf-8")).hexdigest() != prior.input_digest
            or task.objective_hash != prior.input_digest
            or prior.checkpoint_id != task.last_checkpoint_id
            or prior.operation_id != "dev.add"
            or checkpoint is None
            or checkpoint.id != prior.checkpoint_id
            or checkpoint.task_id != task.id
            or checkpoint.operation_key != "dev-operation-v1"
            or checkpoint.step_name != "dev.add"
            or checkpoint.step_name != prior.operation_id
            or checkpoint.payload_digest != prior.input_digest
            or prior.expected_digest != task.evidence_digest
            or prior.result_state is None
            or prior.result_revision is None
            or prior.reason_code is None
        ):
            raise StoreError("Observer request binding changed", 409)
        conclusion = {
            "verified": "supported",
            "mismatch": "contradicted",
            "unavailable": "inconclusive",
        }.get(prior.verdict)
        if conclusion is None:
            raise StoreError("Unsupported historical observer decision", 409)
        return VerificationResult(
            prior.task_id,
            prior.verdict,
            prior.result_state,
            prior.result_revision,
            prior.id,
            prior.reason_code,
            conclusion,
        )

    @staticmethod
    def _snapshot(
        session: Any, owner_id: str, task_id: str
    ) -> evidence_observer.ObservationSnapshot:
        task = session.scalar(select(Task).where(Task.id == task_id, Task.owner_id == owner_id))
        if task is None or task.state != "awaiting_verification":
            raise StoreError("Task unavailable for observation", 404)
        if (
            not isinstance(task.evidence_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", task.evidence_digest) is None
        ):
            raise StoreError("Missing or invalid worker result claim", 409)
        input_digest = hashlib.sha256(task.objective.encode("utf-8")).hexdigest()
        if input_digest != task.objective_hash:
            raise StoreError("Task input integrity check failed", 409)
        if task.last_checkpoint_id is None:
            raise StoreError("Task execution checkpoint is missing", 409)
        checkpoint = session.get(TaskCheckpoint, task.last_checkpoint_id)
        if (
            checkpoint is None
            or checkpoint.task_id != task.id
            or checkpoint.operation_key != "dev-operation-v1"
            or checkpoint.payload_digest != input_digest
        ):
            raise StoreError("Task execution checkpoint is invalid", 409)
        return evidence_observer.ObservationSnapshot(
            owner_id=owner_id,
            task_id=task_id,
            task_revision=task.revision,
            task_state=task.state,
            objective=task.objective,
            input_digest=input_digest,
            recorded_objective_hash=task.objective_hash,
            worker_digest=task.evidence_digest,
            checkpoint_id=checkpoint.id,
            checkpoint_operation_key=checkpoint.operation_key,
            checkpoint_step_name=checkpoint.step_name,
            checkpoint_digest=checkpoint.payload_digest,
            observer_id=evidence_observer.OBSERVER_ID,
            observer_version=evidence_observer.OBSERVER_VERSION,
            source_kind=evidence_observer.SOURCE_KIND,
        )

    @staticmethod
    def _still_bound(session: Any, snapshot: evidence_observer.ObservationSnapshot) -> bool:
        task = session.scalar(
            select(Task)
            .where(Task.id == snapshot.task_id, Task.owner_id == snapshot.owner_id)
            .with_for_update()
        )
        if (
            task is None
            or task.state != snapshot.task_state
            or task.revision != snapshot.task_revision
            or task.objective != snapshot.objective
            or task.objective_hash != snapshot.recorded_objective_hash
            or task.evidence_digest != snapshot.worker_digest
            or task.last_checkpoint_id != snapshot.checkpoint_id
        ):
            return False
        checkpoint = session.get(TaskCheckpoint, snapshot.checkpoint_id)
        return bool(
            checkpoint is not None
            and checkpoint.task_id == snapshot.task_id
            and checkpoint.operation_key == snapshot.checkpoint_operation_key
            and checkpoint.step_name == snapshot.checkpoint_step_name
            and checkpoint.payload_digest == snapshot.checkpoint_digest
            and snapshot.input_digest == hashlib.sha256(task.objective.encode("utf-8")).hexdigest()
        )

    def verify_dev_add(self, owner_id: str, task_id: str, request_id: str) -> VerificationResult:
        """The sole new deterministic observer path; no public or model API.

        A request key is owner-global. Duplicate replay returns the *recorded*
        decision even if a later, distinct request changed the task state.
        """
        if re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", request_id) is None:
            raise StoreError("Invalid observer request ID", 422)
        fingerprint = self._fingerprint(owner_id, task_id)
        with self.sessions() as session:
            prior = self._prior(session, owner_id, task_id, request_id, fingerprint)
            if prior is not None:
                return prior
            try:
                snapshot = self._snapshot(session, owner_id, task_id)
            except StoreError:
                # Another transaction may have completed this same request
                # between the first idempotency lookup and the task-state read.
                # PostgreSQL READ COMMITTED sees its committed check on a new SELECT.
                prior = self._prior(session, owner_id, task_id, request_id, fingerprint)
                if prior is not None:
                    return prior
                raise

        # No task-row lock is held during independent arithmetic observation.
        # Only trusted fixed-source DevAddObserver can issue this conclusion.
        try:
            observation = evidence_observer.DevAddObserver().observe(snapshot)
        except Exception:
            observation = evidence_observer.Observation(
                "inconclusive", None, "observer_unavailable"
            )
        verdict = {
            "supported": "verified",
            "contradicted": "mismatch",
            "inconclusive": "unavailable",
        }.get(observation.conclusion)
        if verdict is None or observation.reason_code not in {
            "expected_result_matches_claim",
            "expected_result_differs_from_claim",
            "unsupported_operation",
            "invalid_input",
            "unsupported_or_invalid_operation",
            "invalid_operands",
            "oversized_evidence",
            "missing_or_invalid_worker_claim",
            "observer_unavailable",
        }:
            raise StoreError("Invalid trusted observer decision", 409)
        if observation.conclusion == "supported" and (
            observation.observed_digest is None
            or observation.observed_digest != snapshot.worker_digest
        ):
            raise StoreError("Observer evidence does not match claimed digest", 409)
        try:
            with self.sessions.begin() as session:
                # The task row is the contention lock for concurrent observers.
                task = session.scalar(
                    select(Task)
                    .where(Task.id == task_id, Task.owner_id == owner_id)
                    .with_for_update()
                )
                if task is None:
                    raise StoreError("Task unavailable for observation", 404)
                prior = self._prior(session, owner_id, task_id, request_id, fingerprint)
                if prior is not None:
                    return prior
                if not self._still_bound(session, snapshot):
                    raise StoreError("Stale observer task/attempt snapshot", 409)

                if verdict == "verified":
                    _advance(task, "completed")
                    _event(session, task, "task.verified")
                check = VerificationCheck(
                    id=str(uuid4()),
                    owner_id=owner_id,
                    task_id=task_id,
                    observed_digest=observation.observed_digest,
                    expected_digest=snapshot.worker_digest,
                    verdict=verdict,
                    request_id=request_id,
                    request_fingerprint=fingerprint,
                    task_revision=snapshot.task_revision,
                    checkpoint_id=snapshot.checkpoint_id,
                    input_digest=snapshot.input_digest,
                    operation_id="dev.add",
                    observer_id=snapshot.observer_id,
                    observer_version=snapshot.observer_version,
                    source_kind=snapshot.source_kind,
                    reason_code=observation.reason_code,
                    result_state=task.state,
                    result_revision=task.revision,
                )
                session.add(check)
                _audit(
                    session,
                    owner_id,
                    task_id,
                    None,
                    "verification",
                    "allow" if verdict == "verified" else "deny",
                    verdict,
                )
                session.flush()
                result = VerificationResult(
                    task_id,
                    verdict,
                    task.state,
                    task.revision,
                    check.id,
                    observation.reason_code,
                    observation.conclusion,
                )
            return result
        except IntegrityError:
            # Unique owner-global request index also fences cross-task races.
            with self.sessions() as session:
                prior = self._prior(session, owner_id, task_id, request_id, fingerprint)
                if prior is not None:
                    return prior
            raise StoreError("Observer request idempotency conflict", 409) from None
