"""Read-only task timeline: owner isolation, bounded keyset cursors and sanitized CLI."""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.api import create_app
from max_core.config import Settings
from max_core.models import OutboxEvent
from max_core.store import StoreError, TaskStore
from max_core.timeline import TaskTimeline
from max_core.timeline_cli import TimelineClientError, _json, _local_url, render_timeline
from max_core.verification import IndependentVerifier
from tests.conftest import TEST_TOKEN


def api(store: TaskStore, token: str = TEST_TOKEN) -> TestClient:
    return TestClient(
        create_app(Settings(bearer_token=token), store=store),
        base_url="http://127.0.0.1",
    )


def authorized() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def make_events(store: TaskStore, key: str) -> tuple[str, bytes]:
    task, _ = store.create("owner", "Private objective: never include in timeline", key)
    store.claim("owner", task.id, "worker-1")
    store.checkpoint(
        "owner", task.id, "worker-1", "checkpoint-001", "private-step", _digest(b"secret")
    )
    evidence = b"independent observation fixture"
    store.record_outcome("owner", task.id, "worker-1", _digest(evidence))
    return task.id, evidence


def test_owner_route_requires_auth_and_storage(client: TestClient, store: TaskStore) -> None:
    task, _ = store.create("owner", "Sensitive", "timeline-auth-001")
    live = api(store)
    assert live.get(f"/v1/tasks/{task.id}/events").status_code == 401
    assert (
        live.get(
            f"/v1/tasks/{task.id}/events", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    assert live.get(f"/v1/tasks/{uuid4()}/events", headers=authorized()).status_code == 404
    assert client.get(f"/v1/tasks/{task.id}/events", headers=authorized()).status_code == 503
    assert client.get(f"/v1/tasks/{task.id}/events").status_code == 401


def test_snapshot_pagination_does_not_include_later_commits(store: TaskStore) -> None:
    task_id, evidence = make_events(store, "timeline-snapshot-001")
    client = api(store)
    path = f"/v1/tasks/{task_id}/events"
    first = client.get(path, headers=authorized(), params={"limit": 2})
    assert first.status_code == 200
    page1 = first.json()
    assert page1["snapshot_revision"] == 4
    assert [e["task_revision"] for e in page1["events"]] == [1, 2]
    cursor = page1["next_cursor"]
    assert isinstance(cursor, str) and 20 < len(cursor) <= 512

    class Reader:
        def observe(self, task_id_: str) -> bytes:
            assert task_id_ == task_id
            return evidence

    IndependentVerifier(store.engine, Reader()).verify("owner", task_id)
    second = client.get(path, headers=authorized(), params={"limit": 2, "cursor": cursor})
    assert second.status_code == 200
    page2 = second.json()
    assert page2["snapshot_revision"] == 4
    assert [e["task_revision"] for e in page2["events"]] == [3, 4]
    assert page2["next_cursor"] is None
    fresh = client.get(path, headers=authorized(), params={"limit": 100}).json()
    assert [e["task_revision"] for e in fresh["events"]] == [1, 2, 3, 4, 5]
    assert fresh["events"][-1]["kind"] == "task.verified"
    assert fresh["snapshot_revision"] == 5


def test_cursor_survives_restart_but_not_token_rotation(store: TaskStore) -> None:
    task_id, _ = make_events(store, "timeline-restart-001")
    path = f"/v1/tasks/{task_id}/events"
    first = api(store).get(path, headers=authorized(), params={"limit": 1}).json()
    cursor = first["next_cursor"]
    assert api(store).get(path, headers=authorized(), params={"cursor": cursor}).status_code == 200
    different_token = "another-test-token-" + "abcdef1234567890" * 3
    rotated = api(store, different_token)
    changed_auth = {"Authorization": f"Bearer {different_token}"}
    res = rotated.get(path, headers=changed_auth, params={"cursor": cursor})
    assert res.status_code == 422
    assert res.json()["detail"] == "Invalid timeline cursor"


@pytest.mark.parametrize("bad_cursor", ["invalid", ".bad", "A" * 513, "!bad.", "1.2.3"])
def test_bad_cursor_fails_closed(store: TaskStore, bad_cursor: str) -> None:
    task, _ = store.create("owner", "Owner only", "timeline-bad-001")
    res = api(store).get(
        f"/v1/tasks/{task.id}/events", headers=authorized(), params={"cursor": bad_cursor}
    )
    assert res.status_code == 422


def test_signature_tamper_wrong_task_and_cross_owner(store: TaskStore) -> None:
    first, _ = make_events(store, "timeline-scope-001")
    second, _ = make_events(store, "timeline-scope-002")
    service = TaskTimeline(store, TEST_TOKEN)
    cursor = service.page("owner", first, limit=1).next_cursor
    assert cursor
    body, sig = cursor.rsplit(".", 1)
    altered = f"{body}.{('0' if sig[0] != '0' else '1') + sig[1:]}"
    with pytest.raises(StoreError, match="Invalid timeline cursor"):
        service.page("owner", first, cursor=altered)
    with pytest.raises(StoreError, match="Invalid timeline cursor"):
        service.page("owner", second, cursor=cursor)
    with pytest.raises(StoreError) as exc:
        service.page("other-owner", first, cursor=cursor)
    assert exc.value.status_code == 404
    res = api(store).get(
        f"/v1/tasks/{second}/events",
        headers=authorized(),
        params={"cursor": cursor},
    )
    assert res.status_code == 422


@pytest.mark.parametrize("limit", ["0", "-1", "101", "nan"])
def test_api_limit_is_bounded(store: TaskStore, limit: str) -> None:
    task, _ = store.create("owner", "Bounded", "timeline-limit-001")
    assert (
        api(store)
        .get(f"/v1/tasks/{task.id}/events", headers=authorized(), params={"limit": limit})
        .status_code
        == 422
    )


def test_no_objective_secrets_lease_or_claim_fields_leak(store: TaskStore) -> None:
    task_id, _ = make_events(store, "timeline-secret-001")
    response = api(store).get(f"/v1/tasks/{task_id}/events", headers=authorized())
    assert response.status_code == 200
    text = response.text
    for private in (
        "Private objective",
        "private-step",
        "secret",
        "lease_owner",
        "claim_token",
        "payload_digest",
        "evidence_digest",
        "idempotency_key",
        "objective",
    ):
        assert private not in text
    event = response.json()["events"][0]
    assert set(event) == {"event_key", "kind", "task_revision", "created_at"}
    with Session(store.engine) as session:
        states = session.scalars(
            select(OutboxEvent.delivery_state).where(OutboxEvent.task_id == task_id)
        ).all()
        assert states == ["pending"] * 4


def test_same_revision_tie_break_is_stable(store: TaskStore) -> None:
    task, _ = store.create("owner", "multiple event kinds", "timeline-tie-001")
    with Session(store.engine) as session, session.begin():
        session.add(
            OutboxEvent(
                id=str(uuid4()),
                task_id=task.id,
                event_key=f"{task.id}:1:task.extra",
                kind="task.extra",
                task_revision=1,
            )
        )
    service = TaskTimeline(store, TEST_TOKEN)
    first = service.page("owner", task.id, limit=1)
    assert first.next_cursor
    second = service.page("owner", task.id, limit=1, cursor=first.next_cursor)
    with Session(store.engine) as session:
        ordered = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.task_id == task.id)
            .order_by(OutboxEvent.task_revision, OutboxEvent.id)
        ).all()
    assert [first.events[0].kind, second.events[0].kind] == [event.kind for event in ordered]
    assert second.next_cursor is None


def test_empty_outbox_has_empty_page(store: TaskStore) -> None:
    task, _ = store.create("owner", "No events", "timeline-empty-001")
    with Session(store.engine) as session, session.begin():
        row = session.scalar(select(OutboxEvent).where(OutboxEvent.task_id == task.id))
        assert row
        session.delete(row)
    page = TaskTimeline(store, TEST_TOKEN).page("owner", task.id)
    assert page.events == ()
    assert page.next_cursor is None
    assert page.snapshot_revision == 1


def test_cli_renders_metadata_without_objective(store: TaskStore) -> None:
    task_id, _ = make_events(store, "timeline-cli-001")
    api_client = api(store)

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = url.replace("http://127.0.0.1:8765", "")
        result = api_client.get(path, headers={"Authorization": request.headers["Authorization"]})
        return httpx.Response(result.status_code, json=result.json())

    with httpx.Client(
        base_url="http://127.0.0.1:8765",
        headers=authorized(),
        transport=httpx.MockTransport(handle),
    ) as client:
        rendered = render_timeline(client, UUID(task_id), limit=1, max_pages=10)
    assert "task.queued" in rendered
    assert "task.checkpointed" in rendered
    assert "task.awaiting_verification" in rendered
    assert "Private objective" not in rendered
    assert "not evidence that an external action happened" in rendered


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://169.254.169.254:8765",
        "http://example.com:8765",
        "http://127.0.0.1:80",
        "http://user:password@127.0.0.1:8765",
        "http://127.0.0.1:8765/private",
    ],
)
def test_cli_refuses_nonloopback_or_unexpected_urls(url: str) -> None:
    with pytest.raises(TimelineClientError):
        _local_url(url)
    assert _local_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"


def test_cli_never_repeats_server_body_on_error() -> None:
    bad = httpx.Response(401, text="SECRET: refused bearer token=very-private")
    with pytest.raises(TimelineClientError) as exc:
        _json(bad)
    assert "SECRET" not in str(exc.value)
    assert "401" in str(exc.value)
