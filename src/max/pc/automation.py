"""Approval-gated Windows automation for MAX PC.

Requests are small, explicit plans. The model cannot silently execute them:
only a direct tap on CONFIRM in the local desktop UI can consume the pending
one-shot token. No arbitrary shell commands or file writes are accepted.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable

# Deliberately small and inspectable, not an arbitrary window selector.
WINDOWS = {
    "notepad": ("notepad",),
    "vscode": ("visual studio code",),
    "chrome": ("google chrome",),
    "edge": ("microsoft edge",),
    "explorer": ("file explorer", "explorer"),
    "spotify": ("spotify",),
    "discord": ("discord",),
    "premiere": ("adobe premiere pro",),
}
SHORTCUTS = {
    "ctrl+s": ("ctrl", "s"),
    "ctrl+f": ("ctrl", "f"),
    "ctrl+a": ("ctrl", "a"),
    "ctrl+c": ("ctrl", "c"),
    "ctrl+v": ("ctrl", "v"),
    "ctrl+z": ("ctrl", "z"),
    "ctrl+shift+s": ("ctrl", "shift", "s"),
    "escape": ("esc",),
    "ctrl+m": ("ctrl", "m"),  # Premiere Export dialog, not a completed export
    "space": ("space",),
}
_TEXT = re.compile(
    r"^(?:type|write|enter)\s+(?:in|into)\s+"
    r"(notepad|vscode|chrome|edge|explorer|spotify|discord|premiere)\s*:\s*(.{1,500})$",
    re.I | re.S,
)
_PRESS = re.compile(
    r"^(?:press|hit)\s+([a-z+\s]+)\s+(?:in|inside)\s+"
    r"(notepad|vscode|chrome|edge|explorer|spotify|discord|premiere)$",
    re.I,
)
_FOCUS = re.compile(
    r"^(?:focus|switch to|activate)\s+"
    r"(notepad|vscode|chrome|edge|explorer|spotify|discord|premiere)$",
    re.I,
)


@dataclass(frozen=True)
class WindowsAction:
    operation: str
    application: str
    value: str = ""

    @property
    def preview(self) -> str:
        if self.operation == "type":
            return f"Focus {self.application} and type exactly:\n{self.value}"
        if self.operation == "shortcut":
            return f"Focus {self.application} and send {self.value.upper()}."
        return f"Bring {self.application} to the foreground."


def parse_action(text: str) -> WindowsAction | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    match = _TEXT.fullmatch(text)
    if match:
        data = match.group(2)
        # No multiline copy/paste, control chars, path-like shell commands,
        # or secrets via this convenience workflow.
        if not data.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in data):
            return None
        return WindowsAction("type", match.group(1).lower(), data)
    match = _PRESS.fullmatch(text)
    if match:
        combo = re.sub(r"\s+", "", match.group(1)).lower()
        if combo not in SHORTCUTS:
            return None
        return WindowsAction("shortcut", match.group(2).lower(), combo)
    if re.fullmatch(r"(?:open|show)\\s+(?:the\\s+)?(?:export\\s+)?dialog\\s+in\\s+premiere", text, re.I) or re.fullmatch(
        r"(?:export|render)\\s+(?:the\\s+)?(?:video|timeline|sequence)\\s+in\\s+premiere", text, re.I
    ):
        # Ctrl+M opens Premiere's export settings; it does NOT render, pick
        # an output destination, choose a preset or publish the result.
        return WindowsAction("shortcut", "premiere", "ctrl+m")
    match = _FOCUS.fullmatch(text)
    if match:
        return WindowsAction("focus", match.group(1).lower())
    return None


class WindowsAdapter:
    """Only import GUI libraries when explicitly executing an approved plan."""

    def __init__(self, pyauto=None, windows=None):
        self._pyauto = pyauto
        self._windows = windows

    def execute(self, action: WindowsAction) -> str:
        if self._pyauto is None:
            import pyautogui
            self._pyauto = pyautogui
        if self._windows is None:
            import pygetwindow
            self._windows = pygetwindow
        self._pyauto.FAILSAFE = True
        # Only match supported applications, never arbitrary window names
        # or background processes. Show the target in the confirmation text.
        titles = WINDOWS[action.application]
        matches = [
            w for w in self._windows.getAllWindows()
            if any(label in str(w.title).lower() for label in titles)
            and bool(str(w.title).strip())
        ]
        if not matches:
            return f"{action.application} has no visible window. Open it first."
        target = matches[0]
        try:
            if bool(getattr(target, "isMinimized", False)):
                target.restore()
            target.activate()
            time.sleep(.2)
            # Windows can refuse focus changes or another window can steal
            # focus. Never inject keystrokes unless the expected HWND owns it.
            active = self._windows.getActiveWindow()
            if (
                active is None
                or getattr(target, "_hWnd", None) is None
                or getattr(active, "_hWnd", None) != target._hWnd
            ):
                return "The requested app is not the active window. Nothing was typed or pressed."
            if action.operation == "type":
                self._pyauto.write(action.value, interval=.008)
            elif action.operation == "shortcut":
                keys = SHORTCUTS[action.value]
                if len(keys) == 1:
                    self._pyauto.press(keys[0])
                else:
                    self._pyauto.hotkey(*keys)
        except Exception as exc:
            return f"Windows didn't complete that action ({type(exc).__name__})."
        return f"Completed {action.operation} in {action.application}. Check the application to confirm."


class ApprovalQueue:
    """One exact action at a time; never persistent or model-authorizable."""

    def __init__(self, adapter: WindowsAdapter | None = None,
                 now: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = 45):
        self.adapter = adapter or WindowsAdapter()
        self._now = now
        self.ttl = ttl_seconds
        self._pending: tuple[str, WindowsAction, float] | None = None

    def plan(self, action: WindowsAction) -> dict:
        token = secrets.token_urlsafe(24)
        self._pending = (token, action, self._now() + self.ttl)
        return {
            "id": token,
            "preview": action.preview,
            "expires_in": self.ttl,
            "application": action.application,
        }

    def cancel(self) -> None:
        self._pending = None

    def approve(self, token: str, *, awake: bool) -> tuple[bool, str]:
        pending = self._pending
        self._pending = None  # consume before any Windows side effect
        if not awake or pending is None or not isinstance(token, str):
            return False, "No pending action. Request the operation again."
        expected, action, expiry = pending
        if not secrets.compare_digest(token, expected) or self._now() > expiry:
            return False, "Approval expired or did not match. Nothing was executed."
        try:
            message = self.adapter.execute(action)
        except Exception as exc:
            return False, f"Desktop action unavailable ({type(exc).__name__})."
        # An unavailable target is not a completed action.
        success = message.startswith("Completed ")
        return success, message
