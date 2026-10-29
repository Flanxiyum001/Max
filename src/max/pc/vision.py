"""Explicit, ephemeral screenshot of a named foreground Windows application.

No background monitoring, file persistence, phone endpoint, or model invocation
happens here. The native PC bridge owns the separate one-shot consent gate.
"""
from __future__ import annotations

import io
import sys
from dataclasses import dataclass


# First milestone is intentionally narrow. A window title alone is not a
# security boundary; the user must also approve uploading the visible pixels.
WINDOW_TITLES = {
    "premiere": "adobe premiere pro",
    "notepad": "notepad",
    "vscode": "visual studio code",
}
MAX_IMAGE_BYTES = 1_200_000
MAX_IMAGE_WIDTH = 1280
MAX_IMAGE_HEIGHT = 900


class VisionUnavailable(RuntimeError):
    """No trusted, visible, capturable app window was found."""


@dataclass(frozen=True)
class WindowSnapshot:
    application: str
    jpeg: bytes
    width: int
    height: int


def capture_foreground_window(application: str, *, gui=None) -> WindowSnapshot:
    """Capture only the approved foreground app, and hold JPEG bytes in RAM.

    The user should switch to the app during the bridge's countdown.
    An off-screen/secondary-monitor or ambiguous capture fails closed rather
    than uploading unexpected content from another window.
    """
    if application not in WINDOW_TITLES:
        raise VisionUnavailable("Choose Premiere, Notepad or VS Code.")
    if gui is None:
        if sys.platform != "win32":
            raise VisionUnavailable("Screen observation is available only in the Windows PC app.")
        try:
            import pyautogui
        except (ImportError, OSError) as exc:
            raise VisionUnavailable("Install the Windows desktop extra: pip install -e '.[pc,desktop]'.") from None
        gui = pyautogui

    def active():
        try:
            return gui.getActiveWindow()
        except (AttributeError, OSError):
            raise VisionUnavailable("Could not identify the foreground Windows app.") from None

    window = active()
    title = str(getattr(window, "title", "") or "").casefold()
    if not title or WINDOW_TITLES[application] not in title:
        raise VisionUnavailable(
            "Wrong foreground window. Switch to the selected app and try LOOK again."
        )
    if getattr(window, "isMinimized", False):
        raise VisionUnavailable("Restore the selected app before using LOOK.")
    rect = tuple(getattr(window, name, None) for name in ("left", "top", "width", "height"))
    if any(not isinstance(v, int) or isinstance(v, bool) for v in rect):
        raise VisionUnavailable("Cannot determine a safe window capture region.")
    left, top, width, height = rect
    if min(width, height) < 120 or width > 20000 or height > 20000:
        raise VisionUnavailable("The selected app window has invalid dimensions.")
    if left < 0 or top < 0:
        raise VisionUnavailable("Move the app to the primary monitor before using LOOK.")

    try:
        screenshot = gui.screenshot()
        sw, sh = screenshot.size
        if left + width > sw or top + height > sh:
            raise VisionUnavailable("Move the entire app onto the primary monitor to capture it.")
        # Recheck before allocating/encoding. If the active app changed during
        # the snapshot, never upload pixels of that other window.
        current = active()
        same_hwnd = getattr(current, "_hWnd", None) == getattr(window, "_hWnd", None)
        if not same_hwnd or str(getattr(current, "title", "") or "").casefold() != title:
            raise VisionUnavailable("The active app changed during LOOK. No image was sent.")
        cropped = screenshot.crop((left, top, left + width, top + height))
        cropped.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT))
        out = io.BytesIO()
        cropped.convert("RGB").save(out, format="JPEG", quality=65, optimize=True)
        jpeg = out.getvalue()
    except VisionUnavailable:
        raise
    except (OSError, ValueError, AttributeError, TypeError):
        raise VisionUnavailable("Screenshot capture failed; no image was sent.") from None

    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_IMAGE_BYTES:
        raise VisionUnavailable("The image could not be safely compressed; no image was sent.")
    return WindowSnapshot(
        application=application, jpeg=jpeg,
        width=cropped.width, height=cropped.height,
    )
