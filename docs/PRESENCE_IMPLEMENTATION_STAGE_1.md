# Cross-device presence milestone P1 — local focus and routing primitives

**Status:** unit-tested core contracts, **not** an installed Windows/Hyprland desktop overlay, Tailscale setup, iPhone PWA, push service or remote approval system.

## Delivered

- Strongly typed per-device presentation state, opt-in/foreground/focus/quiet flags, staleness checks and fail-closed notification delivery.
- Selection of **at most one** recent, foreground, owner-enabled desktop voice sink; a background iPhone browser cannot become a persistent always-on microphone.
- Optional Windows 11 read-only `SHQueryUserNotificationState` probe with injected test function and no dependency on a Windows platform at import. Unknown states or errors suppress alerts.
- Pure offline tests, no secrets, sockets, persistence, microphone, remote execution, approvals or permission changes.

## Platform realities / following milestones

- **Windows:** The Shell API classifies *notification appropriateness*. `QUNS_RUNNING_D3D_FULL_SCREEN` is for exclusive Direct3D. It does **not** universally detect Vulkan, borderless fullscreen, anti-cheat state or GPU load. `QUNS_BUSY`, user quiet preferences and window focus need device testing. No hook into game memory/processes. Official reference: https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shqueryusernotificationstate
- **Hyprland:** A `gtk4-layer-shell` overlay is a **layer surface**, not a regular floating window. Window rules like `float`, `pin`, `no_initial_focus` apply to normal windows, while layer surfaces use namespace-based layer rules; do not blindly combine both. Check current compositor version, overlay namespace, keyboard-interactivity mode and input region on the real host. Never draw above the lock screen by default. Reference: https://wiki.hypr.land/configuring/core/rules/layer-rules/
- **iPhone:** Web Push is supported for eligible iOS 16.4+ *Home Screen* web apps after a user gesture and permission. Background microphone capture is not guaranteed. A push may show on the lock screen, but the notification must include **no approval token or sensitive task text**, and must **not** approve a task by tapping. The user opens the authenticated HTTPS PWA and reauthenticates for sensitive decisions. References: https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers and https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/
- **Transport:** Tailscale is a private network path, **not** app authentication. Authenticate requests, check origin/CSRF, enforce short-lived task-bound one-use approvals in MAX Trust, apply per-device enrolment/revocation and audit. Browsers need a valid HTTPS origin; a Tailscale-served PWA must be available on the phone when opening a notification. Web Push still relies on an external platform push service: no public inbound Core port is required, but that is a separate, opt-in outbound integration. https://tailscale.com/docs/how-to/set-up-https-certificates

## Acceptance gates for actual presence

1. Device enrolment proof with independent application auth, revocation, and replay-resistant signed session binding.
2. Real Hyprland overlay and Windows fullscreen/Focus tests, keyboard focus, per-monitor DPI and reduced-motion checks.
3. Authenticated iPhone PWA over HTTPS and a push opt-in flow, server-to-push integration, and encrypted generic notifications without sensitive payloads.
4. MAX Trust approval challenge and verify flow: bind actor/device/task/action/nonce/expiry, fresh identity check, one-time spend and immutable audit; no client-side approval logic.
5. Disconnected/expired/revoked device tests; no queued high-risk action autoexecutes upon reconnect.

**Rollback:** revert this PR; no runtime endpoints or deployments are created.
