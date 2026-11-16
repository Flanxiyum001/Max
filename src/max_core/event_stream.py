"""Bounded local-only MAX event delivery; not an authorization channel.

No replay: clients must refetch authoritative state after reconnect. Tickets
are single-use and never travel in URLs or model-visible messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from uuid import UUID

_ALLOWED_KINDS = frozenset({"task.created", "task.cancel_requested"})
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


class EventStreamError(ValueError):
    """No raw event, ticket or client details in error messages."""


@dataclass(frozen=True, slots=True)
class StatusEvent:
    sequence: int
    kind: str
    task_id: str
    state: str
    revision: int

    def payload(self) -> dict[str, str | int]:
        return {
            "type": "task_status",
            "sequence": self.sequence,
            "kind": self.kind,
            "task_id": self.task_id,
            "state": self.state,
            "revision": self.revision,
        }


class OneUseTickets:
    """Bounded in-process admission for local browser WebSockets.

    Deliberately does not provide cross-process or remote-device sessions.
    """

    def __init__(self, ttl: int = 20, capacity: int = 64) -> None:
        if not 1 <= ttl <= 60 or not 1 <= capacity <= 256:
            raise EventStreamError("invalid_ticket_policy")
        self.ttl = ttl
        self.capacity = capacity
        self._lock = Lock()
        self._items: dict[bytes, tuple[float, str]] = {}

    def issue(self, origin: str) -> str:
        if not origin or len(origin) > 128:
            raise EventStreamError("invalid_ticket_origin")
        with self._lock:
            now = monotonic()
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            if len(self._items) >= self.capacity:
                raise EventStreamError("ticket_capacity_exceeded")
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode("ascii")).digest()
            self._items[digest] = (now + self.ttl, origin)
            return token

    def consume(self, token: str, origin: str) -> bool:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return False
        digest = hashlib.sha256(token.encode("ascii")).digest()
        with self._lock:
            entry = self._items.pop(digest, None)
        return entry is not None and entry[0] > monotonic() and entry[1] == origin


class LocalEventBus:
    """Non-authoritative in-memory fanout with bounded subscribers and queues.

    Publishing may occur from a sync FastAPI worker thread. Every queue write
    is scheduled on the subscribing event loop; no asyncio queue crosses threads.
    """

    def __init__(self, max_clients: int = 16, queue_size: int = 32) -> None:
        if not 1 <= max_clients <= 128 or not 1 <= queue_size <= 256:
            raise EventStreamError("invalid_bus_policy")
        self._lock = Lock()
        self._max_clients = max_clients
        self._queue_size = queue_size
        self._sequence = 0
        self._next_id = 0
        self._subscribers: dict[
            int, tuple[asyncio.AbstractEventLoop, asyncio.Queue[StatusEvent | None]]
        ] = {}

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def subscribe(self) -> tuple[int, asyncio.Queue[StatusEvent | None], int]:
        loop = asyncio.get_running_loop()
        with self._lock:
            if len(self._subscribers) >= self._max_clients:
                raise EventStreamError("subscriber_capacity_exceeded")
            self._next_id += 1
            identifier = self._next_id
            queue: asyncio.Queue[StatusEvent | None] = asyncio.Queue(self._queue_size)
            self._subscribers[identifier] = (loop, queue)
            return identifier, queue, self._sequence

    def unsubscribe(self, identifier: int) -> None:
        with self._lock:
            self._subscribers.pop(identifier, None)

    @staticmethod
    def _offer(queue: asyncio.Queue[StatusEvent | None], event: StatusEvent) -> None:
        if queue.full():
            # Once an event was dropped, the receiver must refetch state.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        else:
            # A preceding overflow sentinel stays ahead of later events.
            queue.put_nowait(event)

    def publish(self, kind: str, task_id: str, state: str, revision: int) -> int:
        if (
            kind not in _ALLOWED_KINDS
            or not isinstance(task_id, str)
            or len(task_id) != 36
            or not isinstance(state, str)
            or not re.fullmatch(r"[a-z_]{1,32}", state)
            or type(revision) is not int
            or revision < 1
        ):
            raise EventStreamError("invalid_status_event")
        try:
            if str(UUID(task_id)) != task_id:
                raise ValueError
        except ValueError:
            raise EventStreamError("invalid_status_event") from None
        with self._lock:
            self._sequence += 1
            event = StatusEvent(self._sequence, kind, task_id, state, revision)
            subscribers = tuple(self._subscribers.items())
        for identifier, (loop, queue) in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                self.unsubscribe(identifier)
        return event.sequence


def loopback_origins(port: int) -> frozenset[str]:
    return frozenset({f"http://localhost:{port}", f"http://127.0.0.1:{port}"})
