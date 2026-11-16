# P3 — iPhone Mobile Command PWA shell

**Status:** installable, standalone static interface only. No MAX Core mobile API,
authenticated private data feed, STT, upload/streaming voice, approval
submission, encrypted push subscription, backend deployment or Tailscale HTTPS
configuration has been added.

## What ships in this PR

- A responsive, accessible dark/cyan **Voice / Memory / Trust** mobile shell.
- Push-to-talk **local microphone test** started only by a direct gesture.
  All tracks are stopped on release, cancellation, background, lost focus,
  offline state and navigation. No audio is uploaded or retained.
- A swipeable/read-only memory suggestion view and read-only pending action
  feed. Both start disconnected with **no sample personal data**.
- An authenticated same-origin `GET /api/mobile/v1/snapshot` *future contract*.
  The request uses `credentials: same-origin` and `cache: no-store`. If that
  endpoint is absent, unauthorized, malformed or unreachable, both feeds stay
  empty and the screen says `Not connected`.
- A manifest and service worker that cache **only public static shell assets**.
  No private snapshot, audio, credentials, approval tokens or API response is
  cached. No push listener is installed, and no notification is sent.

## Trust and privacy architecture required before release

1. Deploy an authenticated HTTPS origin (potentially Tailscale Serve) and a
   narrow short-lived owner session. Tailscale access alone does not replace
   application authentication. Require origin validation, device enrollment,
   HTTPS, CSRF protection on mutations, rate limiting and audit.
2. Implement the mobile snapshot endpoint with a **server-side** owner/session
   check, bounded redacted summaries and `Cache-Control: no-store`. Never
   return approval bearer tokens in snapshots.
3. Implement `MAX Trust` tier-2 action challenge/decision as **separate,
   device- and action-bound one-time flows** with recent owner authentication,
   expiry, nonce replay prevention and parameter binding. This PWA deliberately
   has no approve/deny button until that mechanism is safe.
4. Connect foreground push-to-talk to a consented STT/voice service. Audio
   capture alone does not speak to MAX. A microphone must not run in the
   background or from a notification. Don't assume iOS browser background audio.
5. Implement optional generic Web Push after an install + owner tap. Apple
   supports standards-based Web Push for eligible Home Screen web apps on
   iOS/iPadOS 16.4+; consent must follow a user gesture. Notifications must
   contain no sensitive task text, memory, approval nonce or token. Tapping
   opens the app; never approves anything.
   Official references:
   https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/
   https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers
6. Validate Safari Home Screen install, touch/keyboard accessibility, loss of
   connectivity, privacy when backgrounded, memory clearing, network denial
   and real device push/microphone permissions.

## Testing and limitations

The static bundle has no third-party runtime dependencies, tracking or external
resource links. The included tests inspect the manifest, CSP, same-origin fetch,
private-cache exclusion, and optional JS parser check when Node is available.
These are **source-level safety checks**, not live browser/device testing.

The mobile API is deliberately absent; no read-only or approval integration
is claimed operational. The PWA can be opened locally to inspect layout, but
microphone capture requires a secure origin and explicit permission. The
HTML `Content-Security-Policy` meta is a defense in depth, not a replacement
for server headers.

Rollback: revert this PR. No backend API, permissions, secret, OS integration,
persistent data or production deployment is changed.
