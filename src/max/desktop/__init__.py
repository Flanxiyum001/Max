"""Optional local desktop control (screen, input, terminal) for MAX.

A stdio MCP server (``max.desktop.server``) is the execution boundary: it owns
the command policy, the approval book and the child environment. MAX itself
only registers three native tools that forward through the MCP manager, so the
capability is off by default, individually denyable, and absent from a build
without ``pyautogui``.

Nothing here is imported at boot unless desktop control is enabled — the core
assistant starts and chats with this package entirely untouched.
"""

from __future__ import annotations

from .approvals import ApprovalBook, ApprovalError, fingerprint
from .policy import Verdict, classify
from .provider import (
    DESKTOP_SERVER_ID,
    DESKTOP_TOOLS,
    DesktopProvider,
    desktop_server_config,
    register_desktop_tools,
)

__all__ = [
    "ApprovalBook",
    "ApprovalError",
    "DESKTOP_SERVER_ID",
    "DESKTOP_TOOLS",
    "DesktopProvider",
    "Verdict",
    "classify",
    "desktop_server_config",
    "fingerprint",
    "register_desktop_tools",
]
