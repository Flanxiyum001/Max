# MAX local task timeline — Milestone 5

**Status:** First-party **local-development** read-only task-history API and terminal client. This is not a remote dashboard, autonomous worker, connected AI model, outbox delivery service or evidence of an actual external action.

## User-visible behavior

A running, owner-authenticated MAX Core with PostgreSQL migration `0002_trust_verification` exposes:

- `GET /v1/tasks/{task_id}/events?limit=20` — at most 20 historical task **metadata** events (default; permitted 1–100), oldest revision first.
- Reply: `task_id`, `snapshot_revision`, `events` (each only `event_key`, `kind`, `task_revision`, `created_at`) and `next_cursor` (null if complete).
- The cursor is an opaque, **HMAC-SHA256 signed** bookmark bound to the authenticated owner, task ID, last (revision, event UUID) and initial task revision. It survives a restart with the *same* bootstrap token; rotating the token deliberately invalidates bookmarks. Every page still requires owner authentication.
- Subsequent task changes **do not appear in the current page sequence**, because the initial snapshot revision is fixed. Start a new timeline request with no cursor for the newest state. Pages use strict revision/ID keyset order; multiple events at the same revision are not skipped.
- Task events are metadata only. The route cannot claim a worker task, lease/ack an outbox event, call a model, approve an action or finalize a task. The **journal is not proof that an email, payment, deployment, or other external effect occurred**.

The terminal client retrieves the current task state with `GET /v1/tasks/{id}` and then fetches bounded timeline pages. It deliberately **never prints the task objective or raw HTTP error bodies**, and sanitizes event fields before printing.

## Run on your own isolated local development setup

Follow [MAX Core bootstrap](CORE_BOOTSTRAP.md) and [task persistence](TASK_PERSISTENCE.md). Use a private local PostgreSQL database and run the **already existing** migrations before serving the durable API. Do not run migrations against production or real personal data without separate owner approval.

Example on Windows PowerShell (use a new token generated locally, not a key from chat):

```powershell
$env:MAX_API_BEARER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(48))"
# Set MAX_DATABASE_URL privately to your *local development* PostgreSQL database.
python -m alembic upgrade head
python -m max_core
```

From a second terminal with the same `MAX_API_BEARER_TOKEN`, replace the UUID with one of your own **non-sensitive test tasks**:

```powershell
python -m max_core.timeline_cli 00000000-0000-4000-8000-000000000001 --limit 20 --max-pages 5
```

The illustrative UUID above is not a live task, so it will return HTTP 404 unless you created that exact task. For a real local test, use the ID returned by the owner-authenticated `POST /v1/tasks` call. No credentials are stored in the CLI, arguments, browser storage or logs; the client reads the bearer token from `MAX_API_BEARER_TOKEN` only.

The client **only** accepts `http://127.0.0.1:<port>`, never follows redirects, ignores proxy environment variables, times out and has finite page caps. It prints the event kind, revision and timestamp, plus a caution that the event is not external-effect verification. It does not modify any state. No remote URL, shell/desktop command, provider account, vault document or other repository is accessed.

## API guarantees and limitations

- **Owner scope:** task must belong to the authenticated local owner; unknown or foreign IDs return 404. Missing or malformed authorization returns 401; unconfigured persistent storage returns 503; malformed/tampered/rotated cursors or invalid limits return 422.
- **Cursor safety:** cursor signatures use a **domain-separated key derived from the local bootstrap bearer token**. This design avoids extra key-management in a local-only prototype. The cursor is **not a permission token** and does not protect remote sessions. Token rotation invalidates old cursors.
- **Consistency:** the cursor captures the task's current revision on first read and only displays outbox rows at or below that revision. A single task transition and its event are committed atomically in the existing TaskStore; later revisions are excluded. This is a *revision snapshot*, not a global PostgreSQL repeatable-read transaction across HTTP calls.
- **Data minimization:** the API does not reveal task objectives, checkpoint content/digests, worker IDs/lease tokens, approval details, evidence digests, or outbox delivery claim tokens. Historical event kinds and timestamps might still reveal activity timing; keep access owner-only.
- **Read-only:** the route uses SELECTs only. Reading does not mark an outbox message as dispatched/delivered. Future event delivery must use a separate reconciled consumer.
- **Ordering:** (task_revision, event UUID) is deterministic for each task. A single integer revision may hold multiple events, so the cursor tracks both. Existing `max_outbox.task_id` index is used; no schema migration is necessary for this bounded first slice. Revisit composite indexing with measured workload before scaling.
- **Client honesty:** the current task state is returned separately from the historical event sequence; output never says a model/agent successfully executed an action without separately recorded verification.
- **Deployment restriction:** do not expose the Phase 1 bootstrap token over LAN, internet, tunnel or mobile browsers. Production identity, key custody, TLS, cross-device sessions, audit hardening and rate limiting are separate future milestones.

## Tests and acceptance

The new unit and HTTP tests cover authenticated access, wrong owner/not found, first/next page, snapshot after independent verification, restart compatibility and token-rotation invalidation, tamper/cross-task cursor refusal, bounds, no private field leakage, stable same-revision tie-break, empty history, CLI sanitization and strict loopback origin. A real ephemeral PostgreSQL integration fixture exercises a concurrent checkpoint while a cursor-based read occurs and verifies no outbox delivery state changes.

Normal CI checks: Python 3.11/3.13, Ruff lint and formatting, strict mypy, ephemeral PostgreSQL migration, pytest and pip-audit. **Check actual CI run for this PR before claiming success**; external devices/providers are not touched by these tests.

## Owner review, rollback and exclusions

This is a discrete review-only PR based on owner-merged Phase 0 audit #8. **No merge or deployment is authorized.** No additional database migration, production or other project modifications.

Rollback: close the unmerged PR, or revert the scoped Python endpoint, read-only module/CLI, tests and docs after review. Existing PostgreSQL schema and task records remain unchanged.

Not included: event streaming/SSE, a web UI, actual task workers, tool execution, provider credentials/FreeLLMAPI hookup, model calls, phone/email/WhatsApp, payment, vault, real device control, production auth and independent real-world-effect observation.
