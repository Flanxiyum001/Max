"""Local WebSocket safety and event-fanout tests; no remote clients/devices."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from max_core.api import create_app
from max_core.config import Settings
from max_core.event_stream import EventStreamError, LocalEventBus, OneUseTickets
from max_core.store import TaskStore
from tests.conftest import TEST_TOKEN

ORIGIN = "http://localhost:8765"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def test_single_use_expiring_origin_bound_ticket() -> None:
    tickets = OneUseTickets(ttl=20, capacity=1)
    ticket = tickets.issue(ORIGIN)
    with pytest.raises(EventStreamError, match="ticket_capacity_exceeded"):
        tickets.issue(ORIGIN)
    assert not tickets.consume(ticket, "http://127.0.0.1:8765")
    assert not tickets.consume(ticket, ORIGIN)
    second = tickets.issue(ORIGIN)
    assert tickets.consume(second, ORIGIN)
    assert not tickets.consume(second, ORIGIN)
    assert not tickets.consume("missing", ORIGIN)


def test_ticket_expiration_and_strict_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    import max_core.event_stream as stream

    monkeypatch.setattr(stream, "monotonic", lambda: 100.0)
    tickets = OneUseTickets(ttl=1)
    token = tickets.issue(ORIGIN)
    monkeypatch.setattr(stream, "monotonic", lambda: 102.0)
    assert not tickets.consume(token, ORIGIN)
    with pytest.raises(EventStreamError):
        OneUseTickets(ttl=0)


def test_invalid_status_cannot_be_published() -> None:
    bus = LocalEventBus()
    for kind, identifier, state, revision in [
        ("grant.approved", "a" * 36, "queued", 1),
        ("task.created", "not-a-uuid", "queued", 1),
        ("task.created", "a" * 36, "admin;execute", 1),
        ("task.created", "a" * 36, "queued", 0),
    ]:
        with pytest.raises(EventStreamError):
            bus.publish(kind, identifier, state, revision)
    assert bus.sequence == 0


def test_bounded_subscribers_and_overflow_resync() -> None:
    async def scenario() -> None:
        bus = LocalEventBus(max_clients=1, queue_size=1)
        subscriber, queue, _ = bus.subscribe()
        with pytest.raises(EventStreamError, match="subscriber_capacity_exceeded"):
            bus.subscribe()
        identifier = "fa5f034f-9f14-46cc-a675-d09b04b4ba6a"
        bus.publish("task.created", identifier, "queued", 1)
        bus.publish("task.cancel_requested", identifier, "cancel_requested", 2)
        await asyncio.sleep(0)
        assert await queue.get() is None
        bus.unsubscribe(subscriber)
        second, _, _ = bus.subscribe()
        bus.unsubscribe(second)

    asyncio.run(scenario())


def ticket(client: TestClient, *, origin: str = ORIGIN) -> str:
    result = client.post("/v1/events/ticket", headers={**AUTH, "Origin": origin})
    assert result.status_code == 200
    assert result.json()["expires_in_seconds"] == 20
    return str(result.json()["ticket"])


def test_ticket_api_requires_bearer_and_accepted_origin(store: TaskStore) -> None:
    with TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store),
        base_url=ORIGIN,
    ) as client:
        assert client.post("/v1/events/ticket").status_code == 401
        assert (
            client.post(
                "/v1/events/ticket", headers={**AUTH, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert ticket(client)


def test_authenticated_socket_emits_metadata_without_objective(store: TaskStore) -> None:
    with TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store),
        base_url=ORIGIN,
    ) as client:
        token = ticket(client)
        with client.websocket_connect(
            "/v1/events/ws",
            headers={"Host": "localhost:8765", "Origin": ORIGIN},
            subprotocols=["max-events-v1", f"max-ticket.{token}"],
        ) as socket:
            hello = socket.receive_json()
            assert hello == {"type": "hello", "last_sequence": 0, "recovery": "refetch"}
            created = client.post(
                "/v1/tasks",
                json={"objective": "very-private-test-objective"},
                headers={**AUTH, "Idempotency-Key": "event-test-key-001"},
            )
            assert created.status_code == 201
            event = socket.receive_json()
            assert event["type"] == "task_status"
            assert event["kind"] == "task.created"
            assert event["task_id"] == created.json()["id"]
            assert event["revision"] == created.json()["revision"]
            assert "objective" not in str(event)
            assert "very-private-test-objective" not in str(event)


def test_socket_refuses_replay_bad_origin_and_missing_protocol(store: TaskStore) -> None:
    with TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store),
        base_url=ORIGIN,
    ) as client:
        token = ticket(client)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/v1/events/ws",
                headers={"Host": "localhost:8765", "Origin": "https://evil.test"},
                subprotocols=["max-events-v1", f"max-ticket.{token}"],
            ):
                pass
        # The bad-origin handshake cannot spend a token.
        with client.websocket_connect(
            "/v1/events/ws",
            headers={"Host": "localhost:8765", "Origin": ORIGIN},
            subprotocols=["max-events-v1", f"max-ticket.{token}"],
        ) as socket:
            assert socket.receive_json()["type"] == "hello"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/v1/events/ws",
                headers={"Host": "localhost:8765", "Origin": ORIGIN},
                subprotocols=["max-events-v1", f"max-ticket.{token}"],
            ):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/events/ws", headers={"Host": "localhost:8765", "Origin": ORIGIN}):
                pass


def test_socket_cannot_be_used_as_command_channel(store: TaskStore) -> None:
    with TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store),
        base_url=ORIGIN,
    ) as client:
        token = ticket(client)
        with client.websocket_connect(
            "/v1/events/ws",
            headers={"Host": "localhost:8765", "Origin": ORIGIN},
            subprotocols=["max-events-v1", f"max-ticket.{token}"],
        ) as socket:
            assert socket.receive_json()["type"] == "hello"
            socket.send_json({"tool": "shell.execute", "command": "whoami"})
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_json()
            assert exc.value.code == 1008
