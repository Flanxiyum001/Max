"""Manually invoked, single-task local developer worker with two PURE allowlisted operations.

No agent, model, shell, import from task data, network operation or filesystem operation.
PostgreSQL is the *only* runtime I/O, via the existing trusted TaskStore.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from time import monotonic
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from max_core.config import ConfigurationError, Settings
from max_core.store import StoreError, TaskStore, create_store

_OWNER = "owner"
_MAX_OBJECTIVE_BYTES = 500
_MAX_OUTPUT_BYTES = 256
_OPERATION_SECONDS = 1.0
_LEASE_SECONDS = 30


class WorkerError(Exception):
    """Sanitized stable failure reason; never contains task data or DB credentials."""


@dataclass(frozen=True, slots=True)
class OperationSpec:
    operation_id: str
    permission: str
    max_seconds: float
    max_output_bytes: int


# Manifest is fixed code, not a plugin registry. No untrusted callable or path is loaded.
OPERATIONS = MappingProxyType(
    {
        "dev.health": OperationSpec("dev.health", "local_owner_development", 1.0, 256),
        "dev.add": OperationSpec("dev.add", "local_owner_development", 1.0, 256),
    }
)


@dataclass(frozen=True, slots=True)
class ValidatedOperation:
    operation_id: str
    params: dict[str, int]


@dataclass(frozen=True, slots=True)
class WorkerReport:
    task_id: str
    state: str
    outcome_digest: str | None
    result: str | None

    def display(self) -> str:
        result = f"Task {self.task_id}: recorded state={self.state}"
        if self.result is not None:
            result += f"\nUnverified local result: {self.result}"
        if self.outcome_digest is not None:
            result += f"\nWorker-reported SHA-256: {self.outcome_digest}"
        result += (
            "\nWorker output is NOT independent verification; "
            "no external action or agent was invoked."
        )
        return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise WorkerError("invalid_operation_input")
        result[name] = value
    return result


def _reject_constant(_: str) -> None:
    raise WorkerError("invalid_operation_input")


def parse_operation(objective: str) -> ValidatedOperation:
    """Deny all freeform objectives, extra keys, duplicate JSON keys and code-like input."""
    if not isinstance(objective, str) or len(objective.encode("utf-8")) > _MAX_OBJECTIVE_BYTES:
        raise WorkerError("invalid_operation_input")
    try:
        value = json.loads(
            objective, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, TypeError, RecursionError, WorkerError):
        raise WorkerError("invalid_operation_input") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"op", "params"}
        or not isinstance(value["op"], str)
        or value["op"] not in OPERATIONS
        or not isinstance(value["params"], dict)
    ):
        raise WorkerError("unknown_or_invalid_operation")
    name = value["op"]
    params = value["params"]
    if name == "dev.health":
        if params:
            raise WorkerError("invalid_operation_input")
        return ValidatedOperation(name, {})
    if name == "dev.add":
        if set(params) != {"a", "b"}:
            raise WorkerError("invalid_operation_input")
        if not all(type(params[k]) is int and -1_000_000 <= params[k] <= 1_000_000 for k in ("a", "b")):
            raise WorkerError("invalid_operation_input")
        return ValidatedOperation(name, {"a": params["a"], "b": params["b"]})
    raise WorkerError("unknown_or_invalid_operation")


def execute_operation(operation: ValidatedOperation) -> bytes:
    """Only fixed-cost, pure arithmetic/static data. No arbitrary expressions or callbacks."""
    if operation.operation_id == "dev.health":
        data: dict[str, str | int] = {"operation": "dev.health", "status": "ok"}
    elif operation.operation_id == "dev.add":
        data = {
            "operation": "dev.add",
            "result": operation.params["a"] + operation.params["b"],
        }
    else:
        raise WorkerError("unknown_or_invalid_operation")
    result = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    if len(result) > _MAX_OUTPUT_BYTES:
        raise WorkerError("operation_output_limit")
    return result


class BoundedWorker:
    """Trusted local process. Never retry an ambiguous claim or mark work verified."""

    def __init__(self, store: TaskStore, worker_id: str | None = None) -> None:
        self.store = store
        self.worker_id = worker_id if worker_id is not None else f"dev-{uuid4().hex}"

    def _stopped(self, task_id: str) -> WorkerReport:
        """Cancellation acknowledgment is allowed only *after* work has stopped."""
        saved = self.store.acknowledge_stopped_cancel(_OWNER, task_id, self.worker_id)
        return WorkerReport(task_id, saved.state, None, None)

    def _recover_or_quarantine(self, task_id: str) -> WorkerReport:
        """No blind replay; leave expired leases for explicit reconciliation."""
        saved = self.store.get(_OWNER, task_id)
        if saved.state == "cancelling":
            try:
                return self._stopped(task_id)
            except StoreError:
                pass
        if saved.state in {"running", "cancelling"}:
            try:
                quarantined = self.store.quarantine_claim(_OWNER, task_id, self.worker_id)
                return WorkerReport(task_id, quarantined.state, None, None)
            except StoreError:
                pass
        saved = self.store.get(_OWNER, task_id)
        return WorkerReport(task_id, saved.state, None, None)

    def run_one(self, task_id: str) -> WorkerReport:
        """Run only an explicitly selected queued task, never a background scheduler."""
        try:
            canonical_id = str(UUID(task_id))
        except (ValueError, TypeError, AttributeError):
            raise WorkerError("invalid_task_id") from None
        task = self.store.get(_OWNER, canonical_id)
        if task.state != "queued":
            raise WorkerError("task_not_queued")
        # Reject unknown/malformed objectives *before* claiming; no poisoned side effects.
        operation = parse_operation(task.objective)
        if OPERATIONS[operation.operation_id].permission != "local_owner_development":
            raise WorkerError("permission_denied")

        # Atomic owner-scoped lease prevents another worker from accepting this task.
        try:
            self.store.claim(_OWNER, canonical_id, self.worker_id, ttl_seconds=_LEASE_SECONDS)
        except StoreError:
            raise WorkerError("task_not_queued_or_lease_conflict") from None

        try:
            if self.store.get(_OWNER, canonical_id).state == "cancelling":
                return self._stopped(canonical_id)
            # Durable checkpoint *before* computation, keyed by this task only.
            fingerprint = hashlib.sha256(task.objective.encode("utf-8")).hexdigest()
            self.store.checkpoint(
                _OWNER,
                canonical_id,
                self.worker_id,
                "dev-operation-v1",
                operation.operation_id,
                fingerprint,
            )
            if self.store.get(_OWNER, canonical_id).state == "cancelling":
                return self._stopped(canonical_id)

            # Only a fixed-cost, no-I/O operation runs here. Clock checks are an
            # additional overrun guard, NOT a general-code hard-kill sandbox.
            start = monotonic()
            result = execute_operation(operation)
            duration = monotonic() - start
            if not isinstance(result, bytes) or len(result) > OPERATIONS[operation.operation_id].max_output_bytes:
                return self._recover_or_quarantine(canonical_id)
            if duration > min(_OPERATION_SECONDS, OPERATIONS[operation.operation_id].max_seconds):
                return self._recover_or_quarantine(canonical_id)

            if self.store.get(_OWNER, canonical_id).state == "cancelling":
                return self._stopped(canonical_id)
            digest = hashlib.sha256(result).hexdigest()
            try:
                saved = self.store.record_outcome(
                    _OWNER, canonical_id, self.worker_id, digest
                )
            except StoreError:
                # Concurrent cancel/lease expiry may have won. Do not assert success.
                return self._recover_or_quarantine(canonical_id)
            return WorkerReport(canonical_id, saved.state, digest, result.decode("ascii"))
        except (Exception, KeyboardInterrupt):
            # A fault may have happened after computation: always quarantine, never retry.
            return self._recover_or_quarantine(canonical_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one trusted harmless MAX dev task")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one already-created queued allowlisted task")
    run.add_argument("task_id", type=UUID)
    commands.add_parser("reconcile-expired", help="Quarantine stale leases; never replay work")
    args = parser.parse_args(argv)

    store: TaskStore | None = None
    try:
        settings = Settings.from_env()
        if settings.database_url is None:
            raise WorkerError("development_database_required")
        store = create_store(settings.database_url)
        if not store.ready():
            raise WorkerError("development_database_not_ready")
        if args.command == "run":
            report = BoundedWorker(store).run_one(str(args.task_id))
            print(report.display())
            return 0 if report.state == "awaiting_verification" else 3
        count = store.reconcile_expired(_OWNER, limit=50)
        print(f"Quarantined expired tasks: {count}; no operation was retried.")
        return 0
    except (ConfigurationError, StoreError, WorkerError, SQLAlchemyError, OSError):
        print(
            "MAX worker: task denied, uncertain or unavailable; inspect local status. "
            "No success is claimed.",
            file=sys.stderr,
        )
        return 2
    finally:
        if store is not None:
            store.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
