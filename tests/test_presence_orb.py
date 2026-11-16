"""Authenticated orb status reducer tests; no socket or UI required."""

from __future__ import annotations

import pytest

from max_core.presence.notifications import FocusState
from max_core.presence.orb import (
    OrbError,
    OrbEvent,
    OrbReducer,
    OrbState,
    OrbVisibility,
)


def test_orb_accepts_only_fresh_authenticated_ordered_event() -> None:
    orb = OrbReducer("desktop")
    orb.receive(
        OrbEvent("desktop", 1, 1000, OrbState.LISTENING),
        authenticated=True,
        now_ms=1001,
    )
    view = orb.view(
        now_ms=1002,
        owner_enabled=True,
        foreground=True,
        quiet_mode=False,
        focus=FocusState.ACCEPTS_NOTIFICATIONS,
    )
    assert view.state == OrbState.LISTENING
    assert view.visibility == OrbVisibility.ACTIVE
    with pytest.raises(OrbError, match="stale_or_replayed_orb_event"):
        orb.receive(
            OrbEvent("desktop", 1, 1001, OrbState.SPEAKING),
            authenticated=True,
            now_ms=1002,
        )


@pytest.mark.parametrize(
    "authenticated,device_id",
    [(False, "desktop"), (True, "other")],
)
def test_unauthorized_status_cannot_drive_visuals(
    authenticated: bool, device_id: str
) -> None:
    orb = OrbReducer("desktop")
    with pytest.raises(OrbError, match="unauthorized_orb_event"):
        orb.receive(
            OrbEvent(device_id, 1, 100, OrbState.AWAITING_APPROVAL),
            authenticated=authenticated,
            now_ms=101,
        )


@pytest.mark.parametrize(
    "focus,expected",
    [
        (FocusState.ACCEPTS_NOTIFICATIONS, OrbVisibility.ACTIVE),
        (FocusState.GAME_OR_FULLSCREEN, OrbVisibility.PASSIVE),
        (FocusState.UNKNOWN, OrbVisibility.PASSIVE),
        (FocusState.PRESENTATION, OrbVisibility.PASSIVE),
        (FocusState.LOCKED_OR_ABSENT, OrbVisibility.PASSIVE),
    ],
)
def test_focus_suppresses_game_alerts(
    focus: FocusState, expected: OrbVisibility
) -> None:
    orb = OrbReducer("desktop")
    orb.receive(
        OrbEvent("desktop", 2, 100, OrbState.AWAITING_APPROVAL),
        authenticated=True,
        now_ms=100,
    )
    view = orb.view(
        now_ms=101,
        owner_enabled=True,
        foreground=True,
        quiet_mode=False,
        focus=focus,
    )
    assert view.visibility == expected
    if expected == OrbVisibility.PASSIVE:
        assert view.state == OrbState.IDLE


def test_expired_or_hidden_events_are_never_visibly_stale() -> None:
    orb = OrbReducer("desktop", ttl_ms=1000)
    orb.receive(OrbEvent("desktop", 1, 100, OrbState.SPEAKING), authenticated=True, now_ms=100)
    def view(**kwargs: object):
        values = dict(
            now_ms=110,
            owner_enabled=True,
            foreground=True,
            quiet_mode=False,
            focus=FocusState.ACCEPTS_NOTIFICATIONS,
        )
        values.update(kwargs)
        return orb.view(**values)

    assert view(quiet_mode=True).visibility == OrbVisibility.PASSIVE
    assert view(foreground=False).visibility == OrbVisibility.PASSIVE
    assert view(owner_enabled=False).visibility == OrbVisibility.HIDDEN
    assert view(now_ms=1101).visibility == OrbVisibility.PASSIVE


def test_stale_future_and_invalid_inputs_denied() -> None:
    orb = OrbReducer("desktop", ttl_ms=1000)
    for event, now in (
        (OrbEvent("desktop", 1, 1, OrbState.IDLE), 1002),
        (OrbEvent("desktop", 1, 1100, OrbState.IDLE), 1000),
    ):
        with pytest.raises(OrbError, match="stale_or_replayed_orb_event"):
            orb.receive(event, authenticated=True, now_ms=now)
    with pytest.raises(OrbError, match="invalid_orb_configuration"):
        OrbReducer("device/../../bad")
    with pytest.raises(OrbError, match="invalid_orb_event"):
        OrbEvent("desktop", 0, 100, OrbState.IDLE)
    with pytest.raises(OrbError, match="invalid_orb_visibility_flags"):
        orb.view(
            now_ms=1000,
            owner_enabled=True,
            foreground=1,
            quiet_mode=False,
            focus=FocusState.ACCEPTS_NOTIFICATIONS,
        )
