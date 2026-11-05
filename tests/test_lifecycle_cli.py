"""Milestone 6: local lifecycle CLI reuses existing auth, state and timeline APIs."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core import lifecycle_cli
from max_core.api import create_app
from max_core.config import Settings
from max_core.lifecycle_cli import (
    LifecycleClientError,
    _prompt_objective,
    cancel_task,
    create_task,
    status_task,
)
from max_core.models import OutboxEvent, Task
from max_core.store import TaskStore
from max_core.timeline_cli import TimelineClientError, render_timeline
from max_core.verification import IndependentVerifier
from tests.conftest import TEST_TOKEN


@pytest.fixture
def local_http(store: TaskStore) -> Iterator[tuple[httpx.Client, list[tuple[str, str]]]]:
    """A mock in-process loopback transport; fail on any unknown API surface."""
    api = TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store=store),
        base_url="http://127.0.0.1",
    )
    calls: list[tuple[str, str]] = []

    def bridge(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        assert method in {"GET", "POST"}
        assert path == "/v1/tasks" or path.startswith("/v1/tasks/")
        assert request.url.host == "127.0.0.1"
        calls.append((method, path))
        response = api.request(
            method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": "application/json"},
        )

    with httpx.Client(
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        transport=httpx.MockTransport(bridge),
    ) as client:
        yield client, calls


def test_create_status_history_cancel_and_fresh_read(
    store: TaskStore, local_http: tuple[httpx.Client, list[tuple[str, str]]]
) -> None:
    client, calls = local_http
    private_description = "My demo-only objective: SECRET-DONT-PRINT"
    out = create_task(client, private_description, "lifecycle-first-001")
    assert "Created:" in out and "Freshly recorded state: queued (revision 1)" in out
    assert private_description not in out
    with Session(store.engine) as session:
        task = session.scalar(select(Task).where(Task.idempotency_key == "lifecycle-first-001"))
        assert task is not None
        task_id = UUID(task.id)
    assert "state=queued" in status_task(client, task_id)
    history = render_timeline(client, task_id, limit=1)
    assert "task.queued" in history
    assert private_description not in history
    report, accepted = cancel_task(client, task_id)
    assert accepted
    assert "Cancellation is confirmed by a fresh state read." in report
    assert "Freshly recorded state: cancelled (revision 2)." in report
    assert "state=cancelled" in status_task(client, task_id)
    assert "task.cancelled" in render_timeline(client, task_id)
    with Session(store.engine) as session:
        events = session.scalars(
            select(OutboxEvent).where(OutboxEvent.task_id == str(task_id))
        ).all()
        assert len(events) == 2
        assert all(event.delivery_state == "pending" for event in events)
    assert all("/v1/tasks" in path for _, path in calls)
    assert not any("gateway" in path or "tool" in path for _, path in calls)


def test_same_key_replay_and_conflict_sanitization(
    store: TaskStore, local_http: tuple[httpx.Client, list[tuple[str, str]]]
) -> None:
    client, _ = local_http
    first = create_task(client, "A safe fixture", "lifecycle-replay-001")
    second = create_task(client, "A safe fixture", "lifecycle-replay-001")
    assert "Created:" in first
    assert "Existing task (idempotent replay):" in second
    assert first.splitlines()[0].split(": ", 1)[1] == second.splitlines()[0].split(": ", 1)[1]
    with pytest.raises(LifecycleClientError) as raised:
        create_task(client, "Different private content", "lifecycle-replay-001")
    assert "409" in str(raised.value)
    assert "Different private content" not in str(raised.value)
    with Session(store.engine) as session:
        tasks = session.scalars(
            select(Task).where(Task.idempotency_key == "lifecycle-replay-001")
        ).all()
        assert len(tasks) == 1


def test_authentication_and_unconfigured_storage_fail_closed(
    store: TaskStore,
) -> None:
    task, _ = store.create("owner", "Private objective", "lifecycle-auth-001")
    for token in ("Bearer invalid", ""):
        api = TestClient(
            create_app(Settings(bearer_token=TEST_TOKEN), store=store),
            base_url="http://127.0.0.1",
        )

        def wrong_auth(
            request: httpx.Request, *, test_api: TestClient = api, bad_token: str = token
        ) -> httpx.Response:
            result = test_api.request(
                request.method,
                request.url.path,
                headers={"Authorization": bad_token},
                content=request.content,
            )
            return httpx.Response(result.status_code, content=result.content)

        with httpx.Client(
            base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(wrong_auth)
        ) as client:
            with pytest.raises(TimelineClientError) as raised:
                status_task(client, UUID(task.id))
            assert "401" in str(raised.value)
            assert "Private objective" not in str(raised.value)
            with pytest.raises(LifecycleClientError) as raised_create:
                create_task(client, "Safe demo", "lifecycle-auth-002")
            assert "401" in str(raised_create.value)

    missing = TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN)),
        base_url="http://127.0.0.1",
    )
    assert (
        missing.post(
            "/v1/tasks",
            headers={"Authorization": f"Bearer {TEST_TOKEN}", "Idempotency-Key": "missing-db-001"},
            json={"objective": "Safe demo"},
        ).status_code
        == 503
    )
    assert (
        missing.post(
            f"/v1/tasks/{task.id}/cancel",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        ).status_code
        == 503
    )


def test_cross_owner_denied_for_status_cancel_and_history(
    store: TaskStore, local_http: tuple[httpx.Client, list[tuple[str, str]]]
) -> None:
    client, _ = local_http
    foreign, _ = store.create("other-owner", "Hidden other owner objective", "foreign-task-001")
    for operation in (
        lambda: status_task(client, UUID(foreign.id)),
        lambda: cancel_task(client, UUID(foreign.id)),
        lambda: render_timeline(client, UUID(foreign.id)),
    ):
        with pytest.raises(TimelineClientError) as error:
            operation()
        assert "404" in str(error.value)
    assert store.get("other-owner", foreign.id).state == "queued"


def test_cancelling_running_task_is_only_a_request(
    store: TaskStore, local_http: tuple[httpx.Client, list[tuple[str, str]]]
) -> None:
    client, _ = local_http
    task, _ = store.create("owner", "Safe running fixture", "running-cancel-001")
    store.claim("owner", task.id, "worker-fixture")
    report, accepted = cancel_task(client, UUID(task.id))
    assert accepted
    assert "state: cancelling" in report
    assert "NOT confirmed complete" in report
    assert store.get("owner", task.id).state == "cancelling"
    assert "task.cancel_requested" in render_timeline(client, UUID(task.id))


def test_cancel_again_is_noop_and_completed_is_rejected(
    store: TaskStore, local_http: tuple[httpx.Client, list[tuple[str, str]]]
) -> None:
    client, _ = local_http
    cancelled, _ = store.create("owner", "Cancel idempotently", "cancel-again-001")
    assert cancel_task(client, UUID(cancelled.id))[1]
    again, ok = cancel_task(client, UUID(cancelled.id))
    assert ok
    assert "already cancelled" in again
    assert store.get("owner", cancelled.id).revision == 2
    with Session(store.engine) as session:
        events = session.scalars(
            select(OutboxEvent).where(OutboxEvent.task_id == cancelled.id)
        ).all()
        assert len(events) == 2

    finished, _ = store.create("owner", "Verified fixture", "cancel-terminal-001")
    store.claim("owner", finished.id, "worker-fixture")
    evidence = b"test-only-observation"
    store.record_outcome(
        "owner", finished.id, "worker-fixture", hashlib.sha256(evidence).hexdigest()
    )

    class Reader:
        def observe(self, task_id: str) -> bytes:
            assert task_id == finished.id
            return evidence

    IndependentVerifier(store.engine, Reader()).verify("owner", finished.id)
    report, accepted = cancel_task(client, UUID(finished.id))
    assert not accepted
    assert "rejected by Core (HTTP 409)" in report
    assert "state: completed" in report
    assert "No cancellation was confirmed" in report
    assert store.get("owner", finished.id).state == "completed"


def test_reconciliation_state_rejects_cancel(
    store: TaskStore, local_http: tuple[httpx.Client, list[tuple[str, str]]]
) -> None:
    client, _ = local_http
    task, _ = store.create("owner", "Needs reconciliation", "cancel-reconcile-001")
    store.claim("owner", task.id, "worker", ttl_seconds=5)
    with Session(store.engine) as session, session.begin():
        record = session.get(Task, task.id)
        assert record is not None
        record.lease_until = datetime.now(UTC) - timedelta(seconds=5)
    assert store.reconcile_expired("owner") == 1
    result, accepted = cancel_task(client, UUID(task.id))
    assert not accepted
    assert "needs_reconciliation" in result


def test_status_readback_failure_never_claims_confirmed_cancel() -> None:
    task_id = uuid4()
    seen = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        if seen == 1:
            return httpx.Response(200, json={"id": str(task_id), "state": "queued", "revision": 1})
        if seen == 2:
            assert request.method == "POST"
            return httpx.Response(
                200, json={"id": str(task_id), "state": "cancelled", "revision": 2}
            )
        return httpx.Response(503, text="secret-server-body-do-not-show")

    with httpx.Client(
        base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(TimelineClientError) as error:
            cancel_task(client, task_id)
        assert "503" in str(error.value)
        assert "cancelled" not in str(error.value)
        assert "secret-server-body" not in str(error.value)
    assert seen == 3


@pytest.mark.parametrize("description", ["", "    ", "x" * 501, "escape\x1b[31m"])
def test_create_refuses_invalid_objective_without_http(
    local_http: tuple[httpx.Client, list[tuple[str, str]]], description: str
) -> None:
    client, calls = local_http
    with pytest.raises(LifecycleClientError):
        create_task(client, description, "lifecycle-invalid-001")
    assert not calls


@pytest.mark.parametrize("key", ["short", "spaces are bad", "unsafe;key", "x" * 101])
def test_invalid_key_refused_without_http(
    local_http: tuple[httpx.Client, list[tuple[str, str]]], key: str
) -> None:
    client, calls = local_http
    with pytest.raises(LifecycleClientError):
        create_task(client, "Safe fixture", key)
    assert not calls


def test_invalid_uuid_refused_by_parser_without_network() -> None:
    with pytest.raises(SystemExit) as raised:
        lifecycle_cli.main(["status", "NOT-A-UUID"])
    assert raised.value.code == 2


def test_noninteractive_prompt_refused_before_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    class NotATerminal:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(lifecycle_cli.sys, "stdin", NotATerminal())
    with pytest.raises(LifecycleClientError, match="interactive terminal"):
        _prompt_objective()


def test_cli_main_create_status_cancel_history_do_not_print_sensitive_content(
    store: TaskStore,
    local_http: tuple[httpx.Client, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_http, calls = local_http
    monkeypatch.setenv("MAX_API_BEARER_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(
        lifecycle_cli, "_prompt_objective", lambda: "Do not display this private title"
    )
    monkeypatch.setattr(lifecycle_cli.httpx, "Client", lambda **kwargs: nullcontext(real_http))
    assert lifecycle_cli.main(["create", "--idempotency-key", "cli-main-safe-001"]) == 0
    created = capsys.readouterr().out
    assert "cli-main-safe-001" in created
    assert "Do not display this private title" not in created
    with Session(store.engine) as session:
        task = session.scalar(select(Task).where(Task.idempotency_key == "cli-main-safe-001"))
        assert task is not None
        task_id = task.id
    assert lifecycle_cli.main(["status", task_id]) == 0
    assert "state=queued" in capsys.readouterr().out
    assert lifecycle_cli.main(["history", task_id]) == 0
    assert "task.queued" in capsys.readouterr().out
    assert lifecycle_cli.main(["cancel", task_id]) == 0
    cancelled = capsys.readouterr().out
    assert "cancelled" in cancelled
    assert "Do not display this private title" not in cancelled
    assert not any(
        method == "POST" and path != "/v1/tasks" and not path.endswith("/cancel")
        for method, path in calls
    )


def test_cli_error_body_never_echoes_auth_or_error_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "SENTINEL-API-SECRET-NOT-FOR-OUTPUT"
    monkeypatch.setenv("MAX_API_BEARER_TOKEN", TEST_TOKEN)

    def denied(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        return httpx.Response(401, text=sentinel + TEST_TOKEN)

    client = httpx.Client(base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(denied))
    monkeypatch.setattr(lifecycle_cli.httpx, "Client", lambda **kwargs: client)
    assert lifecycle_cli.main(["status", str(uuid4())]) == 2
    printed = capsys.readouterr()
    assert sentinel not in printed.err + printed.out
    assert TEST_TOKEN not in printed.err + printed.out
    assert "401" in printed.err
