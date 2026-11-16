# Milestone 14 — localhost event stream (independent review slice)

**Status:** New read-only local transport for task metadata, not a production
multi-device WebSocket system or a replacement for MAX Trust. The current
server is restricted to loopback and still uses the temporary single-owner
bootstrap bearer credential.

## Implemented

- `POST /v1/events/ticket` — authenticated local owner issues a cryptographically
  random, in-memory, one-use, 20-second WebSocket admission ticket.
- `/v1/events/ws` — requires an allowlisted exact localhost Origin, no duplicate
  Origin/protocol headers, and the `max-events-v1` plus `max-ticket.<ticket>`
  WebSocket subprotocols. The response selects `max-events-v1` only.
- The ticket is sent in a protocol header, **not in a query string**. Never
  log that protocol header. The server stores only SHA-256 ticket digests.
- A bounded 16-client / 32-event-per-client in-memory broker publishes only
  `task.created` and `task.cancel_requested` metadata after API task writes.
  An overloaded subscriber receives a close and must refetch task state.
- Every connection first gets `hello` with last process-local sequence
  and `recovery: refetch`. The sequence is not a durable cursor.
- The WebSocket rejects inbound commands (policy close 1008); no tool proposals,
  approvals, code execution, private task objectives, or device authority pass
  through this channel.

## Client contract

1. On the **same local computer**, send an authenticated `POST
   /v1/events/ticket` with the same Origin used by the WebSocket, e.g.
   `http://localhost:8765`.
2. Connect to `ws://localhost:8765/v1/events/ws`, presenting
   `max-events-v1` and `max-ticket.<ticket>` subprotocols; never put
   a bearer token or ticket in a URL.
3. On `hello`, `close 1013`, disconnection, missing sequence, or application
   restart, query the authoritative `/v1/tasks` or
   `/v1/tasks/{task_id}` HTTP API. Do not infer state from an event alone.
4. Do not automatically retry a consumed ticket; obtain another through
   authenticated HTTP. Backoff on capacity errors.

This is an opt-in developer bridge. Local JS is not given the bearer token
by this PR. A future browser session needs proper login, CSRF protections,
trusted HTTPS origins and independently enrolled device identity first.

## Security boundary and remaining Milestone 14 work

This stage does **not** provide device enrollment, hardware-backed keys,
persistent/revocable remote sessions, iPhone PWA auth, TLS/Tailscale, shared
cross-worker fanout, background task-worker events, durable catch-up, device
presence, encrypted push, remote approvals, or a remote endpoint.

Device enrollment and signed-node challenge work in the separate owner-review
M14 branch must be independently validated before any authenticated device may
publish updates. In particular:

- One-time signed challenge and per-device public key, revocation and audit.
- Persistent replay protection and concurrent connection fencing.
- No plain bootstrap bearer on a phone or remote desktop.
- No model text used as permission or provenance.
- Strict server-side validation of event source; no arbitrary client publish.
- No remote access until HTTPS, origin/CSRF, authentication, rate limits,
  cross-process backplane, and real-device tests are complete.

## Verification

CI for Python 3.11 and 3.13 must run Ruff lint and format, mypy,
PostgreSQL migrations/integration, pytest/security tests, and pip-audit.
The new tests verify ticket replay, TTL, origin rejection, inbound-command
rejection, redacted event data, fanout capacity and overflow. Tests do not
verify a real iPhone, desktop overlay, proxy, or production deployment.

## Rollback

Revert this PR. No migration or new external service is introduced, and no
device or account has been accessed. The event transport is intentionally
non-authoritative; losing an in-memory event cannot change task truth.
