# MAX Milestone 8 — independent deterministic `dev.add` observer

**Status:** local-development implementation on a separate owner-review branch. **Not a production observer, proof that a worker ran, external-effect attestation or model tool.**

## Scope and evidence claim

This milestone supports **only** a manually requested independent check of the mathematical answer to `dev.add`. A registered worker already records an untrusted SHA-256 digest of its canonical output and places the task in `awaiting_verification`. The new observer **does not import or call the worker's evaluator**. It obtains the original plaintext objective independently from persisted Core task data, strictly revalidates the JSON and operand bounds, performs a separate integer addition, constructs the documented `{"operation":"dev.add","result":SUM}` canonical JSON with sorted keys and compact separators, then hashes those new bytes.

A matching digest supports the proposition **"the worker's recorded answer digest matches the independently calculated deterministic answer"**. It does **not** prove that worker CPU execution happened, that the DB/host/observer is uncompromised, or that an actual outside effect occurred. `dev.health` is *not* independently verified as real system health. There are no new operation registrations, workers, shell commands, arbitrary code interpreters, filesystem reads, web calls or model calls.

The observer uses **existing PostgreSQL access** as its sole trusted data source, not an external independent service. PostgreSQL can be accessed via local loopback in the manual CLI. A shared hostile DB administrator or malicious host remains outside this prototype's assurance.

## Manual local usage

Preconditions: isolated disposable **development** PostgreSQL database at reviewed Alembic head `0003_independent_observer`; private locally supplied `MAX_DATABASE_URL` with host `127.0.0.1` or `localhost`; a strong `MAX_API_BEARER_TOKEN` in the local environment; an owner-created, **non-sensitive** `dev.add` JSON task that has already been manually executed by Milestone 7. No external DB, internet listener, tunnel, production migration or real personal data. Refer to [TASK_LIFECYCLE.md](TASK_LIFECYCLE.md) and [BOUNDED_WORKER.md](BOUNDED_WORKER.md).

Example after obtaining your real task UUID from the create command:

```powershell
python -m max_core.evidence_observer verify YOUR-TASK-UUID
python -m max_core.lifecycle_cli status YOUR-TASK-UUID
python -m max_core.lifecycle_cli history YOUR-TASK-UUID
```

For explicit replay after uncertain network/process outcome (reuse the **same request ID** that the observer printed **before** executing, and the same task UUID):

```powershell
python -m max_core.evidence_observer verify YOUR-TASK-UUID --request-id observe-EXAMPLE1234
```

A new request ID denotes a new observation attempt. The CLI prints **request ID, task UUID, conclusion, stored verdict, freshly read state, check ID and sanitized reason code** only. It never prints objectives, raw evidence, DB URL/credentials, bearer, SQL errors or Python tracebacks. Result code 0 requires **supported** and freshly recorded **completed**; contradicted/inconclusive return nonzero, as do invalid/unknown/other failure cases. An exact replay returns the **original recorded decision**, even if a different later observation changed task state; the separate fresh-state read is labelled accordingly.

The CLI requires an existing local DB connection, is invoked by the local owner, and adds **no new HTTP route, model permission or Trust capability**. The existing bootstrap token is a developer configuration safeguard, **not an authenticated remote worker or strong proof of human presence**.

## Eligibility and binding

A task must belong to the owner, be `awaiting_verification`, carry a valid 64-character lowercase hexadecimal worker outcome digest, and have a valid *applicable* latest execution checkpoint:

- `Task.last_checkpoint_id` resolves to `TaskCheckpoint.id` on the **same task**.
- `operation_key == "dev-operation-v1"`; the checkpoint payload digest matches a **fresh SHA-256** of the original task objective. The task's saved `objective_hash` must also match the fresh hash.
- `step_name == "dev.add"` for supported decisions. `dev.health` and any unsupported steps are **inconclusive**, never completed.
- The verifier captures owner, task, state, revision, original objective, input/recorded hashes, worker digest, checkpoint ID/step/key/digest, observer ID/version and `source_kind=local_db_recomputation`.
- The observer's pure calculation runs **outside a long-lived task row lock**. The existing `IndependentVerifier` then obtains a PostgreSQL task row lock and repeats the binding checks (including checkpoint identity/metadata and original objective). Any stale revision, modified objective, changed claim or substituted checkpoint is rejected **without a new check or completion**.

A checkpoint establishes an attempt binding, **not** proof of actual execution. This first-generation worker is one-shot and does not support reclaims or automatic retry; a future multi-attempt worker requires a separately durable claim/attempt ID. Missing worker claim or checkpoint is a safe rejection. Malformed/unsupported objective with an otherwise applicable `dev.add` checkpoint produces an **inconclusive** observation and cannot complete.

## Verdict and state

| Observer conclusion | Persisted `VerificationCheck.verdict` | Existing task state |
| --- | --- | --- |
| `supported` | `verified` | Atomic `awaiting_verification → completed` and exactly one `task.verified` outbox event |
| `contradicted` | `mismatch` | Remains `awaiting_verification`; no worker rerun |
| `inconclusive` | `unavailable` | Remains `awaiting_verification`; no worker rerun |
| Stale/forbidden/not eligible | No new decision | No completion; CLI exits nonzero |

**Only the existing `IndependentVerifier` creates checks/audit and finalizes a task.** The observer cannot write state, change the digest, mark tasks verified, request a grant or claim work. The timeline remains SELECT-only; only supported completion advances the task revision and emits `task.verified`. Mismatch/unavailable attempts are in `max_verification_checks` and `max_trust_audit`, not additional outbox events, because the existing outbox event key is based on task revision and event kind and would collide for repeated non-transition events.

## Request idempotency, crashes and concurrency

- The owner-global `request_id` is 8–100 safe ASCII characters, not an authorization token.
- A SHA-256 **request fingerprint** binds owner, task UUID, fixed operation `dev.add`, observer ID/version and fixed source kind. The task input and attempt bindings are stored independently in the same check row.
- Reuse of that ID and exact parameters returns the *same check ID, original verdict, state and revision*. A different task or observer version with the same ID causes **409 conflict**, never a different task's decision. An input/claim/checkpoint modification causes a binding-conflict refusal even if the request ID matches.
- The migration adds both `UNIQUE(owner_id,task_id,request_id)` (explicit task binding) and `UNIQUE(owner_id,request_id)` (cross-task collision prevention). Nullable legacy IDs do not collide.
- PostgreSQL task row locks serialize completion. A unique-index conflict in a cross-task race is translated to a sanitized idempotency conflict. A crash before DB commit leaves no partial check/completion; a crash after commit supports exact replay. Redundant concurrent *pure calculations* are possible; **one committed decision per request and at most one completion per task** are enforced.

No observer lease or scheduler is introduced: verification is a tiny, side-effect-free recomputation, and a distinct request ID is required for a fresh recheck after `mismatch` or `unavailable`.

## Additive migration and historical rows

`alembic/versions/0003_independent_observer.py` extends **existing** `max_verification_checks` with nullable `request_id`, `request_fingerprint`, `task_revision`, `checkpoint_id`, `input_digest`, `operation_id`, `observer_id`, `observer_version`, `source_kind`, `reason_code`, `result_state`, and `result_revision`. New-path records populate all required fields; existing historical checks retain **NULL** provenance and continue to be readable. Existing fixture-reader verification is preserved, but its records are not retroactively promoted to verified observation provenance.

`TaskStore.ready()` requires the newly reviewed head `0003_independent_observer`. Run the existing Alembic upgrade **only** against an isolated disposable local development database *after* reviewing the migration and backup state. A downgrade would **drop the new provenance columns and their history**. Never downgrade a DB containing real verification records without an explicit backup, retention decision and recovery validation. Reverting the unmerged PR requires no schema rollback; if already migrated locally, decide whether to preserve new provenance records before any downgrade.

## Tests and limitations

Unit tests must cover independent `dev.add`, exact schema/duplicate-key/bool/size rejection, `dev.health` denial, missing/foreign/stale checkpoints, changed objective/digest/revision, exact replay, cross-task/key/profile conflicts, observer failure and secret sanitization. PostgreSQL tests must cover identical/distinct concurrent checks, one event and one completion, cross-task uniqueness and legacy verification rows.

The existing two-version CI (Python 3.11/3.13, Ruff, mypy, ephemeral PostgreSQL 17 migrations, full pytest, pip-audit) is the merge gate. Do not claim passing results until a **current-head** CI run confirms them; no physical device, real-world-effect observer, privileged filesystem, other project or production system is used.

**Not proven:** worker execution occurrence, OS/DB integrity, a trusted independent process/role, general operations, arbitrary code safety, personal file contents, network/system health, payment/email/deployment effect, externally tamper-evident audit, production readiness, or strong per-operation MFA. These are separate future milestones.
