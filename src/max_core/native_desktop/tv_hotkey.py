"""Opt-in Windows push-to-talk shortcut; no focus changes or synthetic keys."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_ID = 0x4D54
_WM_HOTKEY = 0x0312
_CTRL_ALT_NOREPEAT = 0x4003
_F10 = 0x79


def is_tv_hotkey(pointer: int) -> bool:
    if not pointer or sys.platform != "win32":
        return False
    message = wintypes.MSG.from_address(pointer)
    return message.message == _WM_HOTKEY and message.wParam == _ID


def register(hwnd: int) -> bool:
    if sys.platform != "win32":
        return False
    register_hotkey = ctypes.WinDLL("user32").RegisterHotKey
    register_hotkey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    register_hotkey.restype = wintypes.BOOL
    return bool(register_hotkey(hwnd, _ID, _CTRL_ALT_NOREPEAT, _F10))


def unregister(hwnd: int) -> None:
    if sys.platform != "win32":
        return
    unregister_hotkey = ctypes.WinDLL("user32").UnregisterHotKey
    unregister_hotkey.argtypes = [wintypes.HWND, ctypes.c_int]
    unregister_hotkey.restype = wintypes.BOOL
    unregister_hotkey(hwnd, _ID)
