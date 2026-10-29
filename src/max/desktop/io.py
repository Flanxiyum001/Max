"""Local screen and input adapters (optional dependency, lazy).

Everything here is loaded on first use so that a MAX without ``pyautogui``
still starts, still chats, and reports an actionable error instead of an
ImportError at boot. Nothing is imported at module import time.

Hard limits live here rather than in the server: bounded screenshot retention,
bounded typing bursts, bounded scroll amounts, and pyautogui's own failsafe
(mouse in the screen corner aborts the batch).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ..core.tools import ToolError

MAX_TEXT_CHARS = 500
MAX_SCROLL = 20
KEEP_SHOTS = 20
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_+\-]{1,24}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# pyautogui validates key names itself; this is a cheap pre-filter so a
# malformed key never reaches the driver.
_KNOWN_KEYS = frozenset(
    {
        "enter", "tab", "esc", "escape", "space", "backspace", "delete",
        "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
        "insert", "ctrl", "alt", "shift", "cmd", "win", "super", "option",
        "capslock", "numlock", "scrolllock", "printscreen", "pause",
        "volumeup", "volumedown", "volumemute",
    }
) | frozenset(f"f{i}" for i in range(1, 25))

GUI_ACTIONS = frozenset(
    {"move", "click", "double_click", "right_click", "middle_click",
     "type", "hotkey", "press", "scroll", "drag"}
)


class DesktopUnavailable(ToolError):
    """pyautogui (or a display) is not available on this host."""


def _driver():
    try:
        import pyautogui  # type: ignore
    except Exception as exc:  # noqa: BLE001 - ImportError or display failure
        raise DesktopUnavailable(
            "desktop input/screenshot needs pyautogui — install it with "
            "`pip install 'max-assistant[desktop]'` and run MAX on a host with "
            f"a display (import failed: {exc})"
        ) from None
    pyautogui.FAILSAFE = True  # park the mouse in a corner to abort a run
    return pyautogui


class DesktopIO:
    """Screenshot + input primitives bounded to sane sizes."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.shot_dir = self.root / ".max-desktop"

    # ------------------------------------------------------------ screenshots
    def screenshot(self, monitor: int = 0) -> dict:
        if not isinstance(monitor, int) or isinstance(monitor, bool) or monitor < 0 or monitor > 8:
            raise ToolError("monitor must be an integer between 0 and 8")
        gui = _driver()
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        name = f"screen-{int(time.time())}-{monitor}.png"
        path = self.shot_dir / name
        if monitor:
            gui.screenshot(str(path), monitor=monitor)
        else:
            gui.screenshot(str(path))
        self._prune()
        try:
            width, height = _image_size(path)
        except Exception:  # noqa: BLE001
            width = height = 0
        return {
            "status": "ok",
            "path": str(path.relative_to(self.root)),
            "absolute_path": str(path),
            "width": width,
            "height": height,
            "bytes": path.stat().st_size,
            "note": (
                "saved inside the workspace — MAX is a text model, so it cannot "
                "view this image itself; read it with the file tools or open it "
                "on the desktop"
            ),
        }

    def _prune(self) -> None:
        try:
            shots = sorted(self.shot_dir.glob("screen-*.png"),
                           key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for old in shots[: max(0, len(shots) - KEEP_SHOTS)]:
            try:
                old.unlink()
            except OSError:
                pass

    # ----------------------------------------------------------------- input
    def action(self, action: str, **kwargs) -> dict:
        name = str(action or "").strip().lower()
        if name not in GUI_ACTIONS:
            raise ToolError(
                f"unknown action '{action}' — permitted: {', '.join(sorted(GUI_ACTIONS))}"
            )
        gui = _driver()

        if name in {"move", "click", "double_click", "right_click", "middle_click", "drag"}:
            x, y = _coords(kwargs)
            if name == "move":
                gui.moveTo(x, y, duration=0.1)
            elif name == "click":
                gui.click(x, y, clicks=int(kwargs.get("clicks", 1)))
            elif name == "double_click":
                gui.doubleClick(x, y)
            elif name == "right_click":
                gui.rightClick(x, y)
            elif name == "middle_click":
                gui.middleClick(x, y)
            else:
                gui.moveTo(x, y, duration=0.1)
                gui.dragTo(*_coords(kwargs, prefix="to_"), duration=0.2)
            return {"status": "ok", "action": name, "x": x, "y": y}

        if name == "type":
            text = str(kwargs.get("text") or "")
            if not text:
                raise ToolError("type requires non-empty 'text'")
            if len(text) > MAX_TEXT_CHARS:
                raise ToolError(f"text longer than {MAX_TEXT_CHARS} characters")
            if _CONTROL.search(text):
                raise ToolError("control characters are not permitted in 'text'")
            gui.write(text, interval=0.02)
            return {"status": "ok", "action": name, "chars": len(text)}

        if name == "hotkey":
            keys = _keys(kwargs.get("keys"))
            if len(keys) < 2:
                raise ToolError("hotkey requires at least two keys")
            gui.hotkey(*keys)
            return {"status": "ok", "action": name, "keys": keys}

        if name == "press":
            keys = _keys(kwargs.get("keys"))
            if not keys:
                raise ToolError("press requires 'keys'")
            for key in keys:
                gui.press(key)
            return {"status": "ok", "action": name, "keys": keys}

        if name == "scroll":
            amount = kwargs.get("amount", 1)
            if not isinstance(amount, int) or isinstance(amount, bool):
                raise ToolError("scroll 'amount' must be an integer")
            if abs(amount) > MAX_SCROLL:
                raise ToolError(f"scroll amount outside ±{MAX_SCROLL}")
            gui.scroll(amount)
            return {"status": "ok", "action": name, "amount": amount}

        raise ToolError(f"unhandled action '{name}'")


def _coords(kwargs: dict, prefix: str = "") -> tuple[int, int]:
    raw_x, raw_y = kwargs.get(prefix + "x"), kwargs.get(prefix + "y")
    if not isinstance(raw_x, int) or isinstance(raw_x, bool) or \
       not isinstance(raw_y, int) or isinstance(raw_y, bool):
        raise ToolError(f"'{prefix}x' and '{prefix}y' must be integers")
    if not (0 <= raw_x <= 100000) or not (0 <= raw_y <= 100000):
        raise ToolError("coordinates out of range")
    return raw_x, raw_y


def _keys(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ToolError("'keys' must be a string or an array of strings")
    out = []
    for item in raw:
        key = str(item).strip().lower()
        if not _SAFE_KEY.match(key) or (key not in _KNOWN_KEYS and not key.isalpha()):
            raise ToolError(f"key '{item}' is not a recognised key name")
        out.append(key)
    if len(out) > 8:
        raise ToolError("at most 8 keys per call")
    return out


def _image_size(path: Path) -> tuple[int, int]:
    """PNG dimensions without requiring Pillow."""
    with path.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    return width, height
