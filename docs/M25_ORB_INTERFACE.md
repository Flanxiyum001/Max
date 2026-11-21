# M25 — Orb interface (default MAX window)

**Status:** Implemented and checked with offscreen Qt tests and screenshots.
**Not yet tried on a real Windows / Hyprland desktop.**

`python -m max_core.native_desktop` (and `start-max.cmd`) now opens a single
frameless black window with an animated glass orb, with nothing else on screen.
The previous dashboard is still available via `--dashboard` or right-click →
*Open dashboard*.

## Using it

- Start typing anywhere: a faint input line appears. Enter sends.
- The answer fades in under the orb and fades out after a while.
- Esc clears the input, then the answer, then closes MAX.
- Drag anywhere to move; double-click for full screen; right-click for the menu.
- Closing while a local model is responding defers shutdown until that bounded
  request finishes rather than freezing the window. A closed window releases
  its local SQLite/desktop-core resources.

## Orb states (all real)

| State | When | Look |
|---|---|---|
| idle | nothing running | slow drift, soft cyan rim |
| thinking | a model request is running | faster currents, brighter glow |
| speaking | answer just arrived (~2.5 s) | medium energy |
| error | model unavailable / not configured | red tint (~2.5 s) |

There is no microphone or speech output yet; the orb never pretends to listen.

## Model

The orb uses the existing `NativeDesktopCore.ask_local` path: **local model
only** (`MAX_TRIAL_*` with `verified_local`, see `M22_NATIVE_LOCAL_HARNESS.md`).
Without configuration the orb turns red and says no model is connected.
If the model proposes tools, the orb explicitly reports that **nothing was
executed** and requires separate owner review; it cannot carry out tool calls.
Free cloud APIs and tool calling are in `python -m max_core.chat` (PR for M23)
and are not wired into the orb yet.

## Tests

`tests/test_native_orb.py`: smoke launch, frameless + idle start, typing reveals
the input and Esc clears it, unconfigured model goes thinking → error honestly.
Dashboard smoke test still passes with `--dashboard`. Additional regressions
cover closing during an in-flight model request, local resource cleanup,
reply auto-hide timer invalidation, and return to idle. The orb redraws at
~30 fps to reduce idle CPU usage; real-device performance remains unmeasured.

Rollback: revert `orb_ui.py`, `__main__.py`, tests and this doc.
