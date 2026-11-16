"""Read-only device presence and focus-aware presentation decisions.

These data are supplied by independently authenticated device adapters. This module
contains no remote-execution endpoint, push credential or approval authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class Platform(StrEnum):
    WINDOWS = "windows"
    HYPRLAND = "hyprland"
    IPHONE_WEB = "iphone_web"


class FocusState(StrEnum):
    ACCEPTS_NOTIFICATIONS = "accepts_notifications"
    GAME_OR_FULLSCREEN = "game_or_fullscreen"
    PRESENTATION = "presentation"
    LOCKED_OR_ABSENT = "locked_or_absent"
    QUIET_TIME = "quiet_time"
    UNKNOWN = "unknown"


class Delivery(StrEnum):
    DISPLAY = "display"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class Presence:
    device_id: str
    platform: Platform
    seen_monotonic: float
    foreground: bool = False
    owner_enabled: bool = False
    microphone_opt_in: bool = False
    quiet_mode: bool = False
    focus: FocusState = FocusState.UNKNOWN

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", self.device_id):
            raise ValueError("invalid_device_identifier")
        if not isinstance(self.platform, Platform) or not isinstance(self.focus, FocusState):
            raise ValueError("invalid_device_state")
        if not isfinite(self.seen_monotonic) or self.seen_monotonic < 0:
            raise ValueError("invalid_presence_time")
        if any(
            type(value) is not bool
            for value in (
                self.foreground,
                self.owner_enabled,
                self.microphone_opt_in,
                self.quiet_mode,
            )
        ):
            raise ValueError("invalid_presence_flags")


def notification_delivery(device: Presence, *, now: float, ttl: float = 30) -> Delivery:
    """Conservatively defer rather than stealing focus or exposing lock-screen content."""
    if not isfinite(now) or not 0 < ttl <= 300:
        raise ValueError("invalid_clock_or_ttl")
    if now < device.seen_monotonic or now - device.seen_monotonic > ttl:
        return Delivery.DEFER
    if not device.owner_enabled or device.quiet_mode or not device.foreground:
        return Delivery.DEFER
    if device.focus != FocusState.ACCEPTS_NOTIFICATIONS:
        return Delivery.DEFER
    return Delivery.DISPLAY


def choose_voice_device(
    devices: tuple[Presence, ...], *, now: float, ttl: float = 15
) -> str | None:
    """Pick at most one active desktop device; never assume background iOS microphone."""
    if not isfinite(now) or not 0 < ttl <= 300:
        raise ValueError("invalid_clock_or_ttl")
    eligible = [
        device
        for device in devices
        if (
            device.platform in {Platform.WINDOWS, Platform.HYPRLAND}
            and device.owner_enabled
            and device.microphone_opt_in
            and device.foreground
            and not device.quiet_mode
            and device.focus == FocusState.ACCEPTS_NOTIFICATIONS
            and 0 <= now - device.seen_monotonic <= ttl
        )
    ]
    if len({device.device_id for device in eligible}) != len(eligible):
        raise ValueError("duplicate_presence_identity")
    if not eligible:
        return None
    return max(eligible, key=lambda device: (device.seen_monotonic, device.device_id)).device_id
