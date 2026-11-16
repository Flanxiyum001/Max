"""Replay-resistant, content-free orb state reducer; never an approval channel."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from max_core.presence.notifications import FocusState


class OrbError(ValueError):
    """No sensitive event payload in errors."""


class OrbState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    AWAITING_APPROVAL = "awaiting_approval"


class OrbVisibility(StrEnum):
    ACTIVE = "active"
    PASSIVE = "passive"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class OrbEvent:
    device_id: str
    sequence: int
    issued_at_ms: int
    state: OrbState

    def __post_init__(self) -> None:
        if (
            not isinstance(self.device_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", self.device_id)
            or type(self.sequence) is not int
            or not 0 < self.sequence < 2**63
            or type(self.issued_at_ms) is not int
            or self.issued_at_ms < 0
            or not isinstance(self.state, OrbState)
        ):
            raise OrbError("invalid_orb_event")


@dataclass(frozen=True, slots=True)
class OrbView:
    state: OrbState
    visibility: OrbVisibility


@dataclass(slots=True)
class OrbReducer:
    """Local display model; trusted transport must authenticate the sender.

    Sequence counters are per enrolled authenticated session. A reconnect must
    start a new trusted reducer, never trust an event-driven counter reset.
    """

    device_id: str
    ttl_ms: int = 15_000
    _state: OrbState = field(default=OrbState.IDLE, init=False)
    _last_sequence: int = field(default=0, init=False)
    _last_event_ms: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", self.device_id)
            or type(self.ttl_ms) is not int
            or not 1000 <= self.ttl_ms <= 60_000
        ):
            raise OrbError("invalid_orb_configuration")

    def receive(self, event: OrbEvent, *, authenticated: bool, now_ms: int) -> None:
        if type(now_ms) is not int or now_ms < 0:
            raise OrbError("invalid_orb_clock")
        if not isinstance(event, OrbEvent):
            raise OrbError("invalid_orb_event")
        if authenticated is not True or event.device_id != self.device_id:
            raise OrbError("unauthorized_orb_event")
        if (
            event.sequence <= self._last_sequence
            or event.issued_at_ms > now_ms
            or now_ms - event.issued_at_ms > self.ttl_ms
            or (self._last_event_ms is not None and now_ms < self._last_event_ms)
        ):
            raise OrbError("stale_or_replayed_orb_event")
        self._last_sequence = event.sequence
        self._last_event_ms = now_ms
        self._state = event.state

    def view(
        self,
        *,
        now_ms: int,
        owner_enabled: bool,
        foreground: bool,
        quiet_mode: bool,
        focus: FocusState,
    ) -> OrbView:
        if type(now_ms) is not int or now_ms < 0 or not isinstance(focus, FocusState):
            raise OrbError("invalid_orb_clock_or_focus")
        if any(type(flag) is not bool for flag in (owner_enabled, foreground, quiet_mode)):
            raise OrbError("invalid_orb_visibility_flags")
        if not owner_enabled:
            return OrbView(OrbState.IDLE, OrbVisibility.HIDDEN)
        if not foreground or quiet_mode or focus != FocusState.ACCEPTS_NOTIFICATIONS:
            return OrbView(OrbState.IDLE, OrbVisibility.PASSIVE)
        if self._last_event_ms is None or not 0 <= now_ms - self._last_event_ms <= self.ttl_ms:
            return OrbView(OrbState.IDLE, OrbVisibility.PASSIVE)
        return OrbView(self._state, OrbVisibility.ACTIVE)
