"""Windows keyboard, mouse and scroll control for the local MAX orb.

Uses only the Windows API through ctypes (no new dependencies). These tools are
offered only to local (non-cloud) host sessions on Windows. With
``MAX_PC_CONTROL=full`` the owner has chosen to let MAX run them, plus
``apps.open`` and the ``desktop.*`` window tools, without asking each time; see
``auto_approved``. Emergency stop: while the mouse pointer rests in the top-left
corner of the primary screen, every PC-control action is refused.
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Mapping
from typing import Any

from max_core.tools import Tool, ToolError, ToolParam, ToolRisk

MAX_TYPE_CHARS = 2_000
_FAILSAFE_PX = 4

# Tools MAX may run without asking when the owner sets MAX_PC_CONTROL=full.
# shell.run is deliberately not here: it still needs approval for each command.
AUTO_APPROVED = frozenset(
    {
        "apps.open",
        "desktop.active_window",
        "desktop.list_windows",
        "desktop.focus_window",
        "pc.screen_size",
        "pc.type_text",
        "pc.press_keys",
        "pc.click",
        "pc.move_mouse",
        "pc.scroll",
        "pc.screenshot",
        "files.save_document",
    }
)


def full_control_enabled(env: Mapping[str, str]) -> bool:
    return env.get("MAX_PC_CONTROL", "").strip().lower() == "full"


def auto_approved(name: str, env: Mapping[str, str]) -> bool:
    return full_control_enabled(env) and name in AUTO_APPROVED


# ---------------------------------------------------------------------------
# Win32 plumbing

_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE, _KEYEVENTF_EXTENDED = 0x0002, 0x0004, 0x0001
_MOUSE = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
_MOUSEEVENTF_WHEEL = 0x0800

# Virtual-key names accepted by pc.press_keys.
VK: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "printscreen": 0x2C,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "mute": 0xAD,
    "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2,
}  # fmt: skip
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B}


def _api() -> Any:
    if sys.platform != "win32":
        raise ToolError("windows_desktop_required")
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr),
        ]  # fmt: skip

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr),
        ]  # fmt: skip

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    user32 = getattr(ctypes, "WinDLL")("user32", use_last_error=True)  # noqa: B009
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    return user32, INPUT, wintypes


def _check_failsafe(user32: Any, wintypes: Any) -> None:
    pt = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(pt)) and pt.x <= _FAILSAFE_PX and pt.y <= _FAILSAFE_PX:
        raise ToolError("stopped_by_owner_failsafe")


def _send(user32: Any, inputs: list[Any], input_type: Any) -> None:
    array = (input_type * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(input_type))
    if sent != len(inputs):
        raise ToolError("input_blocked_by_windows")


def _key(input_type: Any, vk: int = 0, scan: int = 0, flags: int = 0) -> Any:
    item = input_type()
    item.type = _INPUT_KEYBOARD
    item.u.ki.wVk, item.u.ki.wScan = vk, scan
    item.u.ki.dwFlags = flags | (_KEYEVENTF_EXTENDED if vk in _EXTENDED else 0)
    return item


def _mouse(input_type: Any, flags: int, data: int = 0) -> Any:
    item = input_type()
    item.type = _INPUT_MOUSE
    item.u.mi.dwFlags, item.u.mi.mouseData = flags, data & 0xFFFFFFFF
    return item


def _screen(user32: Any) -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _point(user32: Any, args: Mapping[str, Any]) -> tuple[int, int]:
    w, h = _screen(user32)
    x, y = args["x"], args["y"]
    if not (0 <= x < w and 0 <= y < h):
        raise ToolError("point_off_screen")
    return x, y


# ---------------------------------------------------------------------------
# Tool handlers


def screen_size(_: Mapping[str, Any]) -> str:
    user32, _t, wintypes = _api()
    w, h = _screen(user32)
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return f"primary screen {w}x{h} pixels; mouse at ({pt.x}, {pt.y})"


def type_text(args: Mapping[str, Any]) -> str:
    user32, input_type, wintypes = _api()
    _check_failsafe(user32, wintypes)
    text = args["text"].replace("\r\n", "\n")
    inputs = []
    for ch in text:
        if ch == "\n":
            inputs += [
                _key(input_type, VK["enter"]),
                _key(input_type, VK["enter"], 0, _KEYEVENTF_KEYUP),
            ]
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i : i + 2], "little")
            inputs.append(_key(input_type, 0, code, _KEYEVENTF_UNICODE))
            inputs.append(_key(input_type, 0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
    for start in range(0, len(inputs), 200):
        _check_failsafe(user32, wintypes)
        _send(user32, inputs[start : start + 200], input_type)
    return f"typed {len(text)} characters"


def parse_keys(combo: str) -> list[int]:
    names = [part.strip().lower() for part in combo.replace(" ", "").split("+")]
    if not names or len(names) > 5 or any(not n for n in names):
        raise ToolError("invalid_key_combo")
    try:
        return [VK[n] for n in names]
    except KeyError:
        raise ToolError("unknown_key") from None


def press_keys(args: Mapping[str, Any]) -> str:
    codes = parse_keys(args["keys"])
    times = args.get("times", 1)
    if not 1 <= times <= 20:
        raise ToolError("invalid_repeat_count")
    user32, input_type, wintypes = _api()
    for _ in range(times):
        _check_failsafe(user32, wintypes)
        down = [_key(input_type, vk) for vk in codes]
        up = [_key(input_type, vk, 0, _KEYEVENTF_KEYUP) for vk in reversed(codes)]
        _send(user32, down + up, input_type)
        time.sleep(0.03)
    return f"pressed {args['keys']}" + (f" x{times}" if times > 1 else "")


def move_mouse(args: Mapping[str, Any]) -> str:
    user32, _t, wintypes = _api()
    _check_failsafe(user32, wintypes)
    x, y = _point(user32, args)
    if not user32.SetCursorPos(x, y):
        raise ToolError("input_blocked_by_windows")
    return f"mouse moved to ({x}, {y})"


def click(args: Mapping[str, Any]) -> str:
    button = args.get("button", "left").lower()
    if button not in _MOUSE:
        raise ToolError("invalid_mouse_button")
    clicks = args.get("clicks", 1)
    if not 1 <= clicks <= 3:
        raise ToolError("invalid_click_count")
    user32, input_type, wintypes = _api()
    _check_failsafe(user32, wintypes)
    if "x" in args or "y" in args:
        if "x" not in args or "y" not in args:
            raise ToolError("need_both_x_and_y")
        x, y = _point(user32, args)
        if not user32.SetCursorPos(x, y):
            raise ToolError("input_blocked_by_windows")
    down, up = _MOUSE[button]
    for _ in range(clicks):
        _send(user32, [_mouse(input_type, down), _mouse(input_type, up)], input_type)
        time.sleep(0.05)
    return f"{button} click x{clicks}"


def scroll(args: Mapping[str, Any]) -> str:
    amount = args["amount"]
    if not -50 <= amount <= 50 or amount == 0:
        raise ToolError("invalid_scroll_amount")
    user32, input_type, wintypes = _api()
    _check_failsafe(user32, wintypes)
    _send(user32, [_mouse(input_type, _MOUSEEVENTF_WHEEL, 120 * amount)], input_type)
    return f"scrolled {'up' if amount > 0 else 'down'} {abs(amount)}"


def pc_control_tools() -> tuple[Tool, ...]:
    keys = "e.g. 'ctrl+l', 'alt+tab', 'win+d', 'enter', 'ctrl+shift+t', 'volumeup', 'playpause'"
    return (
        Tool(
            "pc.screen_size",
            "Primary screen size in pixels and the current mouse position.",
            ToolRisk.READ_ONLY,
            (),
            screen_size,
        ),
        Tool(
            "pc.type_text",
            "Type text into the focused window as if from the keyboard. Newlines press Enter.",
            ToolRisk.EFFECTFUL,
            (ToolParam("text", str, "text to type", max_length=MAX_TYPE_CHARS),),
            type_text,
        ),
        Tool(
            "pc.press_keys",
            f"Press a key or shortcut in the focused window, {keys}.",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("keys", str, "keys joined with +", max_length=60),
                ToolParam("times", int, "repeat count, 1-20", required=False),
            ),
            press_keys,
        ),
        Tool(
            "pc.click",
            "Click the mouse, at screen pixel (x, y) if given, else where it is now.",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("x", int, "screen x in pixels", required=False),
                ToolParam("y", int, "screen y in pixels", required=False),
                ToolParam("button", str, "left, right or middle", required=False, max_length=6),
                ToolParam("clicks", int, "1, 2 (double) or 3", required=False),
            ),
            click,
        ),
        Tool(
            "pc.move_mouse",
            "Move the mouse pointer to screen pixel (x, y).",
            ToolRisk.EFFECTFUL,
            (ToolParam("x", int, "screen x"), ToolParam("y", int, "screen y")),
            move_mouse,
        ),
        Tool(
            "pc.scroll",
            "Scroll the mouse wheel; positive scrolls up, negative down (notches, -50..50).",
            ToolRisk.EFFECTFUL,
            (ToolParam("amount", int, "notches"),),
            scroll,
        ),
    )


__all__ = [
    "AUTO_APPROVED",
    "auto_approved",
    "full_control_enabled",
    "parse_keys",
    "pc_control_tools",
]
