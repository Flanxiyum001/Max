# M24 — Native desktop title bar with a status island

**Status:** Implemented in the native Qt app and checked in offscreen Qt tests
and screenshots. **Not yet checked on a real Windows or Hyprland desktop.**

## What changed

- The window is frameless with a custom title bar (`native_desktop/title_bar.py`):
  dark red-to-black gradient, back/forward arrows, a black notch-style
  **status island** in the centre (state label + purple waveform), inbox (Trust)
  and help buttons, and minimise/maximise/close. A purple accent strip sits
  below it. Drag the bar to move (uses `startSystemMove`, which also works on
  Wayland), double-click to maximise, resize from the bottom-right grip.
- Back/forward step through page history (also Alt+Left / Alt+Right).
- The scrollable pages now have an explicit dark background; before this they
  showed the OS theme's light colour, which hid the main heading.
- `MAX_NATIVE_SYSTEM_FRAME=yes` keeps the normal OS title bar (useful for
  tiling window managers); the island and arrows are still shown.

## Honest island states

| State | Shown when |
|---|---|
| IDLE | nothing running (flat waveform) |
| THINKING | a real local model request is running (animated) |
| QUIET | quiet mode on |
| LISTENING / SPEAKING | **never set yet** — reserved for a real microphone/voice pipeline |

MAX has no live microphone or text-to-speech in the desktop app, so it never
displays LISTENING on its own.

## Tests

`tests/test_native_desktop_qt.py`: frameless flag, IDLE ↔ QUIET ↔ IDLE,
LISTENING only via explicit `set_state`, back/forward history incl. dropping
forward history, inbox navigation, system-frame opt-out.

Rollback: revert `title_bar.py`, the `qt_ui.py` changes, tests and this doc.
No migration, listener, secret or deployment.
