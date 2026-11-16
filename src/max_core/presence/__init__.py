"""Desktop/iPhone presentation contracts. Not a remote-control system."""

from max_core.presence.notifications import (
    Delivery,
    FocusState,
    Platform,
    Presence,
    choose_voice_device,
    notification_delivery,
)
from max_core.presence.windows import windows_focus_state

__all__ = [
    "Delivery",
    "FocusState",
    "Platform",
    "Presence",
    "choose_voice_device",
    "notification_delivery",
    "windows_focus_state",
]
