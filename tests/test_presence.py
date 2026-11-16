from __future__ import annotations

import ctypes

import pytest

from max_core.presence import (
    Delivery,
    FocusState,
    Platform,
    Presence,
    choose_voice_device,
    notification_delivery,
    windows_focus_state,
)


def device(**changes: object) -> Presence:
    fields = dict(
        device_id="desktop",
        platform=Platform.WINDOWS,
        seen_monotonic=100.0,
        foreground=True,
        owner_enabled=True,
        microphone_opt_in=True,
        focus=FocusState.ACCEPTS_NOTIFICATIONS,
    )
    fields.update(changes)
    return Presence(**fields)


def test_notification_fails_closed_for_games_and_unknown_state() -> None:
    assert notification_delivery(device(), now=105) == Delivery.DISPLAY
    for state in (
        FocusState.GAME_OR_FULLSCREEN,
        FocusState.UNKNOWN,
        FocusState.PRESENTATION,
        FocusState.LOCKED_OR_ABSENT,
    ):
        assert notification_delivery(device(focus=state), now=105) == Delivery.DEFER
    assert notification_delivery(device(quiet_mode=True), now=105) == Delivery.DEFER
    assert notification_delivery(device(owner_enabled=False), now=105) == Delivery.DEFER
    assert notification_delivery(device(foreground=False), now=105) == Delivery.DEFER
    assert notification_delivery(device(), now=200) == Delivery.DEFER
    assert notification_delivery(device(), now=50) == Delivery.DEFER


def test_voice_routes_to_exactly_one_recent_opted_in_desktop() -> None:
    devices = (
        device(device_id="one", seen_monotonic=100),
        device(device_id="two", platform=Platform.HYPRLAND, seen_monotonic=104),
        device(device_id="mobile", platform=Platform.IPHONE_WEB, seen_monotonic=108),
    )
    assert choose_voice_device(devices, now=110) == "two"
    assert choose_voice_device(devices, now=200) is None
    assert choose_voice_device((device(microphone_opt_in=False),), now=101) is None
    assert choose_voice_device((device(focus=FocusState.GAME_OR_FULLSCREEN),), now=101) is None


def test_duplicate_active_voice_identity_denied() -> None:
    with pytest.raises(ValueError, match="duplicate_presence_identity"):
        choose_voice_device((device(), device()), now=101)


def test_presence_input_validation_and_clock_fail_closed() -> None:
    with pytest.raises(ValueError):
        device(device_id="bad/device")
    with pytest.raises(ValueError):
        device(seen_monotonic=float("nan"))
    with pytest.raises(ValueError):
        device(owner_enabled=1)
    with pytest.raises(ValueError):
        notification_delivery(device(), now=float("nan"))


def test_windows_probe_mapping_and_error_fallback() -> None:
    def probe(value: int, status: int = 0):
        def run(ptr: object) -> int:
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_int))[0] = value
            return status

        return run

    assert windows_focus_state(probe(5)) == FocusState.ACCEPTS_NOTIFICATIONS
    assert windows_focus_state(probe(3)) == FocusState.GAME_OR_FULLSCREEN
    assert windows_focus_state(probe(2)) == FocusState.GAME_OR_FULLSCREEN
    assert windows_focus_state(probe(4)) == FocusState.PRESENTATION
    assert windows_focus_state(probe(1)) == FocusState.LOCKED_OR_ABSENT
    assert windows_focus_state(probe(6)) == FocusState.QUIET_TIME
    assert windows_focus_state(probe(7)) == FocusState.UNKNOWN
    assert windows_focus_state(probe(5, -1)) == FocusState.UNKNOWN
    assert windows_focus_state(lambda ptr: (_ for _ in ()).throw(OSError())) == FocusState.UNKNOWN
