# Presence P2 — holographic orb renderer and event-replay guard

**Status:** offline desktop renderer + Python view reducer. This is **not** a
Tauri installation, Win32 transparent always-on-top overlay, GTK4 layer-shell
surface, authenticated WebSocket server, iPhone PWA or deployed push service.

## Included

- `src/max_core/presence/orb.py`: content-free state enum
  (`idle`, `listening`, `thinking`, `speaking`, `awaiting_approval`),
  monotonically ordered events, freshness and device-scope validation.
  **Only authenticated trusted transport** is allowed to supply the
  `authenticated` fact. A reconnect requires a new authenticated session,
  not a counter reset from untrusted input.
- Presentation policy hides the orb when owner-disabled and makes it passive
  in background, quiet, unknown-focus, fullscreen/game or presentation modes.
  Hidden/passive views never repeat stale/sensitive events.
- `clients/orb/index.html`: standalone transparent Canvas implementation of
  MAX's cyan/blue orb, adaptive states, reduced-motion support, high-DPI scaling
  and no network/microphone/approval operations. A trusted desktop host passes
  `window.MAXOrb.setView({state, visibility})` after applying the reducer.
  No external JavaScript/CSS, telemetry or remotely loaded assets.

## Integration and acceptance gates

1. Enroll and authenticate a device independently of Tailscale; bind
   authenticated WebSocket messages to the device, session, expiry and event
   sequence. **Do not** invoke the reducer with `authenticated=true` based on
   a message field supplied by the WebSocket peer.
2. Wrap the Canvas in a minimal Tauri or platform-native window; verify
   transparent click-through, screen capture/lock-screen privacy, DPI,
   keyboard focus, startup, display changes and resource limits on Windows.
3. On Hyprland validate actual `gtk4-layer-shell` namespaces, layer rules,
   monitor routing, reduced-motion and pointer/keyboard interaction. A normal
   floating Wayland window is not interchangeable with a layer surface.
4. Bind the existing `windows_focus_state` probe and a real Hyprland focus
   adapter to passive-mode routing. The Windows Shell probe does not prove
   all Vulkan/borderless games are detected. Benchmark on the real games.
5. Separately add the owner-authenticated HTTPS iPhone PWA with
   foreground push-to-talk, pending memory suggestions, an approval *queue*
   and optional generic iOS Web Push. Approval decisions must run through
   MAX Trust step-up/single-use exact-action checks; never approve from a
   notification tap or by receiving an orb state event.

## Security and rollback

The orb displays only enum names, never objectives, personal memory contents,
secrets, approval tokens or actions. It cannot execute tools and must not
receive entire task payloads.

Rollback by reverting this PR; no runtime API, persistent data, platform
permissions, secret, database migration or deployment is modified.

## Validation

Automated Python reducer tests cover malformed, cross-device, stale/replayed
events and game-mode suppression. **Desktop rendering and performance are
unverified until tested on the owner's Windows and Hyprland hosts.**
