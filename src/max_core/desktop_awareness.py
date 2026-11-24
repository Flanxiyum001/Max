"""Bounded Windows desktop metadata and window focus; no capture or input injection."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from max_core.tools import Tool, ToolError, ToolParam, ToolRisk

MAX_WINDOWS = 30
MAX_TITLE = 120


def _user32() -> Any:
    if sys.platform != "win32":
        raise ToolError("windows_desktop_required")
    from ctypes import wintypes

    api = getattr(ctypes, "WinDLL")("user32", use_last_error=True)
    api.GetForegroundWindow.restype = wintypes.HWND
    api.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.IsWindow.argtypes = [wintypes.HWND]
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.SetForegroundWindow.argtypes = [wintypes.HWND]
    return api


def _title(api: Any, hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(MAX_TITLE + 1)
    api.GetWindowTextW(hwnd, buffer, len(buffer))
    # Titles are untrusted external text. Remove control characters.
    return "".join(ch if ch.isprintable() else " " for ch in buffer.value).strip()


def _window(api: Any, hwnd: int) -> dict[str, Any]:
    return {"handle": hwnd, "title": _title(api, hwnd)}


def active_window(_: Mapping[str, Any]) -> str:
    api = _user32()
    hwnd = api.GetForegroundWindow()
    if not hwnd:
        raise ToolError("foreground_window_unavailable")
    return json.dumps(_window(api, hwnd), ensure_ascii=True)


def list_windows(_: Mapping[str, Any]) -> str:
    api = _user32()
    from ctypes import wintypes

    windows: list[dict[str, Any]] = []
    callback_type = getattr(ctypes, "WINFUNCTYPE")(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _: int) -> bool:
        if api.IsWindowVisible(hwnd) and api.GetWindowTextLengthW(hwnd):
            item = _window(api, hwnd)
            if item["title"]:
                windows.append(item)
        return len(windows) < MAX_WINDOWS

    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    completed = api.EnumWindows(callback_type(visit), 0)
    if not completed and len(windows) < MAX_WINDOWS:
        raise ToolError("window_enumeration_failed")
    # Bound ASCII JSON output even for Unicode titles.
    while len(json.dumps(windows, ensure_ascii=True)) > 7500:
        windows.pop()
    return json.dumps(windows, ensure_ascii=True)


def focus_window(args: Mapping[str, Any]) -> str:
    api = _user32()
    hwnd, title = args["handle"], args["title"]
    if (
        type(hwnd) is not int
        or not 0 < hwnd <= (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
        or not isinstance(title, str)
        or not title
        or len(title) > MAX_TITLE
    ):
        raise ToolError("invalid_window")
    if not api.IsWindow(hwnd) or not api.IsWindowVisible(hwnd) or _title(api, hwnd) != title:
        raise ToolError("window_changed")
    if not api.SetForegroundWindow(hwnd) or api.GetForegroundWindow() != hwnd:
        raise ToolError("window_focus_denied")
    return "Window is now in the foreground."


def gpu_status(_: Mapping[str, Any]) -> str:
    if sys.platform != "win32":
        raise ToolError("windows_desktop_required")
    # No arbitrary command, PATH search, shell, or model-supplied arguments.
    candidates = [
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "NVIDIA Corporation"
        / "NVSMI"
        / "nvidia-smi.exe",
    ]
    binary = next((path for path in candidates if path.is_file()), None)
    if binary is None:
        raise ToolError("nvidia_telemetry_unavailable")
    try:
        result = subprocess.run(
            [
                str(binary),
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.SubprocessError):
        raise ToolError("nvidia_telemetry_failed") from None
    return result.stdout[:2000].strip() or "No GPU data returned."


def desktop_tools() -> tuple[Tool, ...]:
    # Window titles can reveal confidential information even though observation
    # changes no OS state. Require an explicit approval for each observation.
    return (
        Tool(
            "desktop.active_window",
            "Inspect the active window title.",
            ToolRisk.EFFECTFUL,
            (),
            active_window,
        ),
        Tool(
            "desktop.list_windows",
            "List visible window titles and handles.",
            ToolRisk.EFFECTFUL,
            (),
            list_windows,
        ),
        Tool(
            "desktop.focus_window",
            "Focus one window using its exact handle and title from desktop.list_windows.",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("handle", int, "window handle"),
                ToolParam("title", str, "exact current window title", max_length=MAX_TITLE),
            ),
            focus_window,
        ),
        Tool(
            "system.gpu",
            "Local NVIDIA GPU usage, VRAM and temperature.",
            ToolRisk.READ_ONLY,
            (),
            gpu_status,
        ),
    )
