"""Windows notification-state probe. Safe to import on non-Windows hosts."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable

from max_core.presence.notifications import FocusState

# shellapi.h QUERY_USER_NOTIFICATION_STATE (these values are not bitflags).
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7


def windows_focus_state(probe: Callable[[object], int] | None = None) -> FocusState:
    """Read SHQueryUserNotificationState, defaulting to UNKNOWN on any failure.

    This is an *interruption* signal, not reliable DirectX/Vulkan process detection.
    Borderless games and some Vulkan windows may surface as BUSY or UNKNOWN.
    """
    if probe is None:
        if sys.platform != "win32":
            return FocusState.UNKNOWN
        factory = getattr(ctypes, "WinDLL", None)
        if factory is None:
            return FocusState.UNKNOWN
        try:
            native = factory("shell32").SHQueryUserNotificationState
            native.argtypes = [ctypes.POINTER(ctypes.c_int)]
            native.restype = ctypes.c_long
            probe = native
        except (OSError, AttributeError):
            return FocusState.UNKNOWN
    result = ctypes.c_int()
    try:
        hresult = probe(ctypes.byref(result))
    except (OSError, TypeError, ValueError):
        return FocusState.UNKNOWN
    if hresult != 0:
        return FocusState.UNKNOWN
    return {
        QUNS_NOT_PRESENT: FocusState.LOCKED_OR_ABSENT,
        QUNS_BUSY: FocusState.GAME_OR_FULLSCREEN,
        QUNS_RUNNING_D3D_FULL_SCREEN: FocusState.GAME_OR_FULLSCREEN,
        QUNS_PRESENTATION_MODE: FocusState.PRESENTATION,
        QUNS_ACCEPTS_NOTIFICATIONS: FocusState.ACCEPTS_NOTIFICATIONS,
        QUNS_QUIET_TIME: FocusState.QUIET_TIME,
        QUNS_APP: FocusState.UNKNOWN,
    }.get(result.value, FocusState.UNKNOWN)
