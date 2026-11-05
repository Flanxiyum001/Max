"""Independent observation gate for a worker's untrusted completion assertion.

A trusted host supplies EvidenceReader, NOT the worker/model's claimed hash.
No filesystem, network, external tool or active worker is implemented here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from max_core.models import Task, VerificationCheck
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


class IndependentVerifier:
    """Internal-only finalization; never call this from a model-supplied tool proposal."""

    def __init__(self, engine: Engine, reader: EvidenceReader) -> None:
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.reader = reader

    def verify(self, owner_id: str, task_id: str) -> VerificationResult:
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
                session, owner_id, task.id, None, "verification",
                "allow" if verdict == "verified" else "deny", verdict,
            )
            if verdict == "verified":
                _advance(task, "completed")
                _event(session, task, "task.verified")
            session.flush()
            return VerificationResult(task.id, verdict, task.state, task.revision, check.id)
