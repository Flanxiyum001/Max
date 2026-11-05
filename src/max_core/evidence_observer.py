"""Pure, independent `dev.add` expected-result observer and local-only CLI.

This module NEVER imports or executes the worker's calculation function.
The observed bytes are independently reconstructed from persisted task inputs.
A matched digest proves only deterministic answer correctness, not execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url

from max_core.config import ConfigurationError, Settings
from max_core.store import StoreError, create_store

OBSERVER_ID = "max.dev_add.expected_result"
OBSERVER_VERSION = "1"
SOURCE_KIND = "local_db_recomputation"
_MAX_INPUT_BYTES = 500
_MAX_OUTPUT_BYTES = 256
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True, repr=False)
class ObservationSnapshot:
    """Trusted verifier-supplied immutable task snapshot. NEVER log the objective."""

    owner_id: str
    task_id: str
    task_revision: int
    task_state: str
    objective: str
    input_digest: str
    recorded_objective_hash: str
    worker_digest: str
    checkpoint_id: str
    checkpoint_operation_key: str
    checkpoint_step_name: str
    checkpoint_digest: str
    observer_id: str
    observer_version: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class Observation:
    conclusion: str
    observed_digest: str | None
    reason_code: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _no_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")


class DevAddObserver:
    """No database mutation; no worker import; only bounded pure arithmetic."""

    def observe(self, snapshot: ObservationSnapshot) -> Observation:
        if (
            snapshot.observer_id != OBSERVER_ID
            or snapshot.observer_version != OBSERVER_VERSION
            or snapshot.source_kind != SOURCE_KIND
            or snapshot.checkpoint_step_name != "dev.add"
            or snapshot.checkpoint_operation_key != "dev-operation-v1"
        ):
            return Observation("inconclusive", None, "unsupported_operation")
        if (
            not isinstance(snapshot.objective, str)
            or len(snapshot.objective.encode("utf-8")) > _MAX_INPUT_BYTES
        ):
            return Observation("inconclusive", None, "invalid_input")
        try:
            payload = json.loads(
                snapshot.objective, object_pairs_hook=_unique_object, parse_constant=_no_constant
            )
        except (ValueError, TypeError, RecursionError):
            return Observation("inconclusive", None, "invalid_input")
        if (
            not isinstance(payload, dict)
            or set(payload) != {"op", "params"}
            or payload["op"] != "dev.add"
            or not isinstance(payload["params"], dict)
            or set(payload["params"]) != {"a", "b"}
        ):
            return Observation("inconclusive", None, "unsupported_or_invalid_operation")
        numbers = payload["params"]
        if not all(
            type(numbers[name]) is int and -1_000_000 <= numbers[name] <= 1_000_000
            for name in ("a", "b")
        ):
            return Observation("inconclusive", None, "invalid_operands")
        # Independently derived mathematical expectation; no worker code or output.
        expected = {
            "operation": "dev.add",
            "result": numbers["a"] + numbers["b"],
        }
        canonical = json.dumps(
            expected, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
        if len(canonical) > _MAX_OUTPUT_BYTES:
            return Observation("inconclusive", None, "oversized_evidence")
        digest = hashlib.sha256(canonical).hexdigest()
        if not _DIGEST.fullmatch(snapshot.worker_digest):
            return Observation("inconclusive", digest, "missing_or_invalid_worker_claim")
        if digest == snapshot.worker_digest:
            return Observation("supported", digest, "expected_result_matches_claim")
        return Observation("contradicted", digest, "expected_result_differs_from_claim")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify local dev.add mathematical correctness")
    command = parser.add_subparsers(dest="command", required=True)
    verify = command.add_parser("verify", help="Independently recompute one awaiting dev.add task")
    verify.add_argument("task_id", type=UUID)
    verify.add_argument(
        "--request-id", default=None, help="Non-secret idempotency key for retry (8-100 characters)"
    )
    args = parser.parse_args(argv)
    store = None
    try:
        config = Settings.from_env()
        if config.database_url is None:
            raise StoreError("development_database_required")
        url = make_url(config.database_url)
        if url.host not in {"localhost", "127.0.0.1"}:
            raise StoreError("local_database_required")
        store = create_store(config.database_url)
        if not store.ready():
            raise StoreError("development_database_not_ready")
        from max_core.verification import IndependentVerifier

        request_id = args.request_id or f"observe-{uuid4().hex}"
        # Print the id BEFORE the request to permit exact retry if the outcome is uncertain.
        print(f"Verification request ID for safe retry: {request_id}")
        recorded = IndependentVerifier(store.engine).verify_dev_add(
            "owner", str(args.task_id), request_id
        )
        fresh = store.get("owner", str(args.task_id))
        print(
            f"Task {args.task_id}\n"
            f"Observer conclusion: {recorded.conclusion}\n"
            f"Persisted verification verdict: {recorded.verdict}\n"
            f"Recorded state after independent read: {fresh.state}\n"
            f"Check ID: {recorded.check_id}\n"
            f"Reason: {recorded.reason_code}\n"
            "This verifies calculation correctness, NOT proof the worker ran."
        )
        return 0 if recorded.conclusion == "supported" and fresh.state == "completed" else 3
    except (ConfigurationError, StoreError, SQLAlchemyError, OSError, ValueError):
        print(
            "MAX observer: denied, conflicted, stale or unavailable; no verification success claimed.",
            file=sys.stderr,
        )
        return 2
    finally:
        if store is not None:
            store.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
