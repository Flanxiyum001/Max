# MAX durable tasks — Milestone 2

**Status:** Implemented data layer and local-only authenticated task API. Not a worker/agent executor, event delivery service, or production deployment. The existing single-owner token remains for **local development only**.

## Purpose and contract

MAX now has a PostgreSQL-backed place to store a task before any future model or worker runs it. Tasks survive API restarts and have an auditable state, revision, idempotent creation key and optional checkpoint. State transitions and metadata-only outbox rows are **committed in the same transaction**. A work item that may have performed side effects is **never blindly retried** after a lease expires.

Tables (Alembic revision `0001_durable_tasks`):
- `max_tasks`: owner scope, objective (maximum 500 characters), hash, create idempotency key, state, revision, lease and checkpoint references.
- `max_task_checkpoints`: step name and SHA-256 digest of an external artifact; no raw tool payload is stored in the checkpoint table.
- `max_outbox`: stable event key (`task:revision:event`), state, claim token, lease, attempts and delivery timestamp. **There is no external dispatcher in this milestone.** An ack means a future consumer acknowledged internal metadata, not that an external side effect was verified.

## Local database setup — never on production

Install the project's development environment using [CORE_BOOTSTRAP.md](CORE_BOOTSTRAP.md). Create a separate **local** PostgreSQL database and restricted account in your own environment. Do not share a connection URL containing a password in chat, GitHub or logs.

Set `MAX_DATABASE_URL` as a private environment variable in the format `postgresql+psycopg://user@127.0.0.1:5432/dbname` (include credentials only through your trusted terminal/secret store). Apply the reviewed schema **before starting the API**:

```bash
python -m alembic -c alembic.ini upgrade head
python -m max_core
```

The service requires a strong `MAX_API_BEARER_TOKEN` exactly as in Phase 1. The CLI still binds only `127.0.0.1`; this PR does **not** authorize tunnels, LAN exposure, Cloudflare/Railway deployment, account linking or bot integration. If `MAX_DATABASE_URL` is absent, pre-existing bootstrap endpoints work, but authenticated task routes return **503**. If a DB is configured but unavailable, missing migration or wrong revision, `GET /health/ready` returns **503**, not a false-ready response.

## Authenticated task API

Every task endpoint needs `Authorization: Bearer <locally-generated-token>`. `POST /v1/tasks` also requires a distinct `Idempotency-Key` header of 8–100 allowed ASCII characters (letters, digits, dot, underscore, colon or hyphen).

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/v1/tasks` | JSON `{"objective":"..."}` creates queued task; **201** new, **200** exact replay, **409** same key with changed objective |
| GET | `/v1/tasks` | Most recent 50 owner tasks; task IDs, state, revision, timestamps and checkpoint ID |
| GET | `/v1/tasks/{uuid}` | One owner task or 404 |
| POST | `/v1/tasks/{uuid}/cancel` | Queued -> cancelled, running -> cancelling, terminal incompatible -> 409; replay is idempotent |
| GET | `/health/ready` | Requires DB connectivity, expected migration revision and task table to claim durable readiness |

No client-facing endpoint claims a task, writes checkpoints, acknowledges outbox delivery or marks work completed. These remain **trusted internal repository methods** awaiting later worker and verifier implementation. A model may not call them directly.

## Task lifecycle and safety properties

```text
queued -> running -> awaiting_verification -> (future independent verifier)
   |        |
   |        +-> cancelling -> needs_reconciliation (on lease expiry)
   +-> cancelled
running -> needs_reconciliation (on lease expiry)
```

- Worker lease: internal `claim()` assigns a scoped worker and expiration; PostgreSQL row locks prevent concurrent claims. Only a worker with a live lease can `checkpoint()` or `record_outcome()`.
- Checkpoint: stable operation key, step label and SHA-256 digest; exact replay doesn't duplicate the event or checkpoint, altered replay fails.
- Observed outcome: becomes **awaiting_verification**, **never completed** on a worker's assertion alone. Independent validation is a later milestone.
- Cancellation: stops future storage-side progress by denying further checkpoint/outcome requests; it cannot interrupt code in an external process yet, and should not be described as guaranteed remote cancellation.
- Stale lease: `reconcile_expired()` moves uncertain work to **needs_reconciliation**; no automatic re-execution until a future independent effect reconciliation is implemented.
- Outbox: internal events leased with bounded TTL using `FOR UPDATE SKIP LOCKED` on PostgreSQL. A stale lease can be reclaimed, but **any future external consumer must use the stable event key for destination-level deduplication and confirmation**. This is at-least-once infrastructure, **not exactly-once side effects**.

## Data minimization and trust

Task objectives are **plaintext in the configured database**, so do **not** submit API keys, passwords, Aadhaar/passport data, OTPs, UPI PINs, card CVVs or confidential vault contents. Future encryption, vault separation and retention/deletion are in [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md). Logs retain only method, route template, status and request ID; avoid database SQL echo or public telemetry.

## Verification and CI

Run `python -m pytest -q`, `python -m ruff check src tests`, `python -m ruff format --check src tests`, `python -m mypy src/max_core`, and `python -m pip_audit --progress-spinner off`.

GitHub CI provisions **ephemeral PostgreSQL 17** with **test-only credentials**, runs `alembic upgrade head`, then tests concurrent claim/outbox leasing, restart persistence, duplicate create, owner isolation, cancellation, lease expiry and checkpoint replay on Python 3.11 and 3.13. Local SQLite tests exercise state behavior but do **not** replace PostgreSQL integration evidence. Any CI failure must be fixed before owner merges.

## Rollback and next PR

No database was created or changed for the user in this assignment. To undo this **unmerged** milestone, close the PR. If the schema was manually applied to an isolated **disposable local test DB**, it may be dropped or reverted after verifying its data is unneeded; do not run `alembic downgrade` against a database with real tasks without a reviewed backup and rollback plan.

Next: explicit model gateway and independent verification (separate PRs), followed by secured tool policy and a real worker. The owner reviews and merges each PR manually; no deployment.

## Milestone 4 schema/readiness follow-up

The owner-reviewed Trust milestone adds additive Alembic revision `0002_trust_verification` (approval grants, metadata-only policy audit and independent verification records). Durable API readiness now requires **revision 0002**, not 0001. Task completion is only available through the **internal verifier with trusted observation bytes**; a worker's reported evidence hash remains untrusted. Before migrating an existing database, review [TRUST_IMPLEMENTATION.md](TRUST_IMPLEMENTATION.md) and obtain a backup/rollback plan. No database was modified when creating the PR.
