# MAX local task lifecycle — Milestone 6

**Status:** local-development **command-line interaction only**. The CLI adds no model, agent, worker, browser, shell execution or external effect. It reuses Milestone 2's task API and Milestone 5's read-only timeline. Existing database schema and permission enforcement are unchanged.

## What the owner can do

Run an already configured, local-only MAX Core using [Core setup](CORE_BOOTSTRAP.md) and [existing PostgreSQL task setup](TASK_PERSISTENCE.md) (the current migration head is `0002_trust_verification`). Generate a fresh strong token locally and set `MAX_API_BEARER_TOKEN` in the terminal. Keep `MAX_DATABASE_URL` private, pointed at a disposable **development** database. The Core process still binds only to `127.0.0.1`. Do not paste credentials into ChatGPT, GitHub or shell arguments.

In the terminal running the server:

```powershell
python -m max_core
```

Use a **second terminal with the same locally configured token**, from the repository's installed development environment:

| Task | PowerShell or shell command | What happens |
| --- | --- | --- |
| Create a new task | `python -m max_core.lifecycle_cli create` | Interactively prompts for a **non-sensitive** task description with terminal echo disabled; stores it as **plaintext in your dev PostgreSQL task table**, not as a secret |
| Create with a replay key | `python -m max_core.lifecycle_cli create --idempotency-key demo-task-001` | A repeated *same key and same description* returns the same task (HTTP 200), rather than creating a duplicate |
| Inspect current status | `python -m max_core.lifecycle_cli status YOUR-TASK-UUID` | Reads and prints the **latest** task ID/state/revision; never prints the description |
| Inspect existing history | `python -m max_core.lifecycle_cli history YOUR-TASK-UUID --limit 20 --max-pages 5` | Uses the **existing** signed-cursor read-only timeline; prints metadata events, no mutation |
| Request cancellation | `python -m max_core.lifecycle_cli cancel YOUR-TASK-UUID` | Uses existing POST cancellation API, **then independently GETs task state** before stating what is recorded |

`YOUR-TASK-UUID` is the real UUID shown by the create command, not a placeholder to send literally. When no replay key is provided, the CLI generates a unique non-secret key and prints it **before attempting the request**. Keep that key if a network error makes creation outcome uncertain; replay it with **the same description** instead of automatically generating a new task. A new key represents a new task.

The CLI intentionally has **no `--objective` or `--description` command-line flag**, to avoid putting the objective into shell history, command-line process lists and scripted logs. Input echo suppression does **not** encrypt the database or make the text suitable for personal documents or credentials. Only create generic demonstration tasks; never enter API tokens, payment details, OTPs, CVVs, Aadhaar/passport data or confidential objectives.

All commands use a fixed HTTPX client with `http://127.0.0.1:8765` by default. If needed, put `--base-url http://127.0.0.1:OTHER_PORT` **before** the subcommand. Non-loopback hosts, other schemes, usernames, query parameters and URLs with paths are refused; the client follows no redirects, ignores proxy environment variables and has a short timeout. Authentication continues to be required on every Core route.

## Cancellation semantics: avoid false success

**Important:** An accepted request is not the same as a confirmed terminal state. The CLI always reads state again **after** POST and reports exactly what Core recorded, or reports the read-back failure without claiming cancellation.

| Existing state when request is processed | Existing Core behavior | Safe CLI interpretation |
| --- | --- | --- |
| `queued` | HTTP 200; transitions to `cancelled` and adds `task.cancelled` event | Fresh GET `cancelled`: **recorded cancellation confirmed** |
| `running` | HTTP 200; transitions to `cancelling` and adds `task.cancel_requested` | Fresh GET `cancelling`: **request accepted; cancellation NOT complete** |
| `cancelled` | HTTP 200, no new transition/event | Already cancelled; read-back confirms existing state |
| `cancelling` | HTTP 200, no new transition/event | Request remains pending; **NOT complete** |
| `completed`, `awaiting_verification`, `needs_reconciliation` | HTTP 409; state unchanged | Cancellation rejected; CLI performs a fresh GET and reports the current recorded state |
| Unknown or foreign UUID | HTTP 404 | Not found; no state disclosed |
| DB unavailable / no storage | HTTP 503 | No state or side effect claimed |
| Auth invalid/missing | HTTP 401 | No task access |
| Race, timeout, or post-POST read failure | HTTP result may not determine final state | CLI avoids a completion claim. Query `status` before deciding what happened |

The `running` and `cancelling` cases above are **test-fixture conditions only** in this milestone. MAX has **no running task worker or cancellation signal sender**. Ordinary newly created tasks remain `queued` until cancelled. No CLI option can mark a task `running`, `completed` or `verified`; those transitions belong to existing trusted internal store/verifier methods and future separate implementation milestones.

The CLI uses an internal read of the server response for task ID, state and revision **only**; it never prints task objective fields from JSON, raw error response bodies, headers or bearer tokens. HTTP 409 is a *request rejection*, not a transient error to retry automatically. CLI returns nonzero for rejected cancellations, validation errors and unavailable state reads.

## Validation and acceptance

- Functional tests use an in-process mocked HTTP transport with a **strict allowlist of existing task routes**; no real external model/tool endpoints.
- Tests cover create/status/history/cancel, genuine read-back after cancellation, duplicate idempotency-key replay and changed-description conflict, invalid UUID/description/key, authentication failure, wrong owner, unavailable DB, already-cancelled no-op, completed/needs-reconciliation rejection, running→cancelling semantics, read-back failure, output redaction and loopback-only behavior.
- PostgreSQL 17 CI tests serialize **claim vs cancel** and **simultaneous duplicate cancel** races with row locks; expect one cancellation event, correct revisions and no fictitious task completion.
- CI runs both Python 3.11 and 3.13, Ruff checks/formatting, strict mypy, Alembic migrations, existing full pytest suite and pip-audit. **Record the actual CI results in the PR; do not claim passing tests before evidence.**
- Owner's own PowerShell/Arch terminal acceptance on an isolated development DB remains **unverified** until performed. This PR does not deploy or contact the owner's machine.

## Rollback and exclusions

Close this PR if unmerged, or revert the isolated CLI/tests/documentation commit after owner review. No new migration, new dependency or change to existing task data formats. Existing task records remain in the owner's own development DB until managed separately.

Excluded: all execution workers and specialists, FreeLLMAPI or other model calls, new task state transitions, public network exposure, web/voice/desktop/mobile frontends, payments, email/WhatsApp/phone, personal vault, local PC control, deployments and changes to other repositories. A terminal prompt is **not** a strong step-up authentication factor. The bootstrap token is still development-only.
