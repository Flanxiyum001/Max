"""Interactive local-development task lifecycle CLI.

Reuses the existing Core HTTP routes and Milestone 5 timeline client.
No model, worker, shell, external tool or task-state transitions live here.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import httpx

from max_core.config import ConfigurationError, Settings
from max_core.timeline_cli import (
    _KNOWN_STATES,
    _json,
    _local_url,
    TimelineClientError,
    render_timeline,
)

_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")
_MAX_RESPONSE_BYTES = 16_384


class LifecycleClientError(Exception):
    """Never echo objectives, authentication headers or remote error bodies."""


def _task_view(payload: dict[str, Any], expected_id: UUID | None = None) -> tuple[UUID, str, int]:
    """Extract only the three non-sensitive task fields used in output."""
    value = payload.get("id")
    state = payload.get("state")
    revision = payload.get("revision")
    try:
        task_id = UUID(value) if isinstance(value, str) else None
    except ValueError:
        task_id = None
    if (
        task_id is None
        or str(task_id) != value
        or (expected_id is not None and task_id != expected_id)
        or not isinstance(state, str)
        or state not in _KNOWN_STATES
        or type(revision) is not int
        or revision < 1
    ):
        raise LifecycleClientError("Invalid task state returned by local MAX")
    return task_id, state, revision


def _created_payload(response: httpx.Response) -> dict[str, Any]:
    if response.status_code not in {200, 201}:
        raise LifecycleClientError(
            f"Task creation was not confirmed (HTTP {response.status_code})"
        )
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise LifecycleClientError("Invalid task response from local MAX")
    try:
        data = response.json()
    except ValueError as exc:
        raise LifecycleClientError("Invalid task response from local MAX") from exc
    if not isinstance(data, dict):
        raise LifecycleClientError("Invalid task response from local MAX")
    return data


def _read_status(client: httpx.Client, task_id: UUID) -> tuple[UUID, str, int]:
    payload = _json(client.get(f"/v1/tasks/{task_id}"))
    return _task_view(payload, task_id)


def _validate_key(key: str) -> None:
    if _KEY.fullmatch(key) is None:
        raise LifecycleClientError("Invalid idempotency key (8-100 safe ASCII characters)")


def create_task(client: httpx.Client, objective: str, idempotency_key: str) -> str:
    """Never include the plaintext objective in a CLI result or exception."""
    _validate_key(idempotency_key)
    normalized = objective.strip()
    if not 1 <= len(normalized) <= 500 or not normalized.isprintable():
        raise LifecycleClientError("Task description must be 1-500 printable characters")
    response = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": idempotency_key},
        json={"objective": normalized},
    )
    created_id, _, _ = _task_view(_created_payload(response))
    _, state, revision = _read_status(client, created_id)
    label = "Created" if response.status_code == 201 else "Existing task (idempotent replay)"
    return (
        f"{label}: {created_id}\n"
        f"Freshly recorded state: {state} (revision {revision})\n"
        "No agent or external action was started."
    )


def status_task(client: httpx.Client, task_id: UUID) -> str:
    _, state, revision = _read_status(client, task_id)
    return (
        f"Task {task_id}: recorded state={state}, revision={revision}\n"
        "This is database state, not proof of an external action."
    )


def cancel_task(client: httpx.Client, task_id: UUID) -> tuple[str, bool]:
    """Return (report, request_accepted); always GET again after the POST."""
    _, previous_state, _ = _read_status(client, task_id)
    response = client.post(f"/v1/tasks/{task_id}/cancel")
    if response.status_code not in {200, 409}:
        raise LifecycleClientError(
            f"Cancellation could not be confirmed (HTTP {response.status_code})"
        )

    # The POST response is *not* independent evidence of the resulting state.
    # A failed GET must never be presented as confirmed cancellation.
    _, actual_state, revision = _read_status(client, task_id)
    latest = f"Freshly recorded state: {actual_state} (revision {revision})."
    if response.status_code == 409:
        return (
            f"Cancellation rejected by Core (HTTP 409).\n{latest}\n"
            "No cancellation was confirmed.",
            False,
        )
    if actual_state == "cancelled":
        if previous_state == "cancelled":
            conclusion = "Task was already cancelled; no new cancellation is claimed."
        else:
            conclusion = "Cancellation is confirmed by a fresh state read."
    elif actual_state == "cancelling":
        conclusion = "Cancellation was requested, but is NOT confirmed complete."
    else:
        conclusion = "Core accepted the request, but cancellation is NOT confirmed."
    return f"Core accepted cancellation request (HTTP 200).\n{latest}\n{conclusion}", True


def _prompt_objective() -> str:
    """Avoid shell history, command-line arguments and terminal echo."""
    if not sys.stdin.isatty():
        raise LifecycleClientError("An interactive terminal is required for task creation")
    try:
        return getpass.getpass(
            "Non-sensitive task description (stored as plaintext, input hidden): "
        )
    except (EOFError, KeyboardInterrupt) as exc:
        raise LifecycleClientError("Task entry cancelled") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage local MAX development tasks only")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="Local MAX URL")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Prompt privately for a non-sensitive task")
    create.add_argument(
        "--idempotency-key",
        help="Safe replay key, not a secret; default generates a new one",
    )
    for name in ("status", "cancel", "history"):
        cmd = commands.add_parser(name, help=f"Read task status or {name} it")
        cmd.add_argument("task_id", type=UUID, help="Existing task UUID")
        if name == "history":
            cmd.add_argument("--limit", type=int, default=20)
            cmd.add_argument("--max-pages", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        url = _local_url(args.base_url)
        token = Settings.from_env().bearer_token
        if args.command == "create":
            key = args.idempotency_key or f"cli-{uuid4().hex}"
            _validate_key(key)
            objective = _prompt_objective()
        with httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False,
            trust_env=False,
            timeout=5.0,
        ) as client:
            if args.command == "create":
                # The key is not a credential. Print it for safe retries after a timeout.
                print(f"Idempotency key for retry: {key}")
                report = create_task(client, objective, key)
            elif args.command == "status":
                report = status_task(client, args.task_id)
            elif args.command == "history":
                report = render_timeline(
                    client, args.task_id, limit=args.limit, max_pages=args.max_pages
                )
            else:
                report, accepted = cancel_task(client, args.task_id)
                print(report)
                return 0 if accepted else 3
    except (LifecycleClientError, TimelineClientError, ConfigurationError) as exc:
        print(f"MAX lifecycle: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPError:
        print(
            "MAX lifecycle: local MAX is unavailable; a request may have been committed. "
            "Read status before retrying; reuse the creation idempotency key.",
            file=sys.stderr,
        )
        return 2
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
