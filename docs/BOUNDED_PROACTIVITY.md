# Milestone 11 — local, bounded proactive suggestions (review PR)

Status: code proposed for owner review. No remote webhook subscriptions, unattended agents,
real provider events, sandbox workers, or production deploys are supplied here.

## Behaviour

MAX Connect accepts three strictly typed event assertions through the **owner-authenticated
loopback API**. POST /v1/connect/events accepts only source, kind, event_key, and resource_key.
Permitted source/kind pairs are github_ci/ci.failed, calendar/calendar.upcoming, and
local_build/build.finished. No arbitrary event text, URLs, prompt content, commands,
or external recipient addresses are accepted. The source name is a local bridge's
claim, not cryptographic proof from GitHub or a calendar provider.

Each accepted unique event creates a deterministic, **non-executable** private draft.
These drafts recommend a next step but do not run an LLM, query a provider, call
a tool, create a task, modify files, or open a PR. There is intentionally no
approve or execute endpoint. Future actions must pass MAX Trust's action-specific
owner approval, capability and evidence checks; simply reading a draft never
constitutes approval.

GET /v1/connect/drafts lists at most 50 pending drafts, newest first (default 20).
POST /v1/connect/drafts/{uuid}/dismiss marks the specified owner draft dismissed
idempotently; it has no external effect. The existing local bootstrap bearer token
is required on every endpoint. MAX is still loopback-only; **do not** publish it
via reverse proxies, tunnels or remote firewall rules.

## Durability and safety

Migration 0004_connect_drafts adds a separate, owner-scoped draft table, not task or
outbox events. The event key is unique per owner and source. Identical retries return
the same draft; conflicting reuse is denied. A maximum of 50 pending drafts per
owner prevents unbounded queue growth, including across PostgreSQL workers under
an owner-specific transaction advisory lock. Raw untrusted payloads are not stored.
No external source can grant itself new event types or tool authority.

The limit is **pending queue capacity**, not per-minute abuse mitigation.
Remote-ready webhooks additionally require independent provider signature
validation, timestamps/nonces, verified installation/repository allowlists,
per-source quotas, secure ingress identity, retained-event expiry, error isolation
and robust subscription lifecycle management.

## Owner-only local example

Run MAX with a fresh local token and a private migrated PostgreSQL database,
following docs/CORE_BOOTSTRAP.md. These are illustrative fields, not live evidence:

POST /v1/connect/events
Authorization: Bearer [local bootstrap token]
Content-Type: application/json

{"source":"github_ci","kind":"ci.failed","event_key":"run-12345678","resource_key":"my-repo/workflow/42"}

Then review GET /v1/connect/drafts. No real GitHub webhook or calendar subscription
is set up by running these requests.

## Acceptance and limits

Check valid types, untrusted input refusal, idempotent replay, conflicting replay,
owner isolation, restart persistence, queue capacity, dismiss behaviour, bearer
authentication, migrations, and the entire pre-existing CI suite on Python
3.11/3.13. Confirm no task execution or approval endpoint is introduced.

For actual draft-first **research**, add a separately reviewed bounded preparer
with an independent isolated sandbox, explicit read-only capability set, fixed
resource/time/token budgets, cancellation and safe artifact export. A source
bridge must authenticate and deduplicate real upstream events before calling
this local API. Calendar 15-minute timing and CI/build completion currently
depend on those future source bridges.

Rollback: close/revert this owner-review PR. After deployment, review/export
unread drafts before downgrading migration 0004 (downgrade drops drafts).
