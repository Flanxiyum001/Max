# MAX Trust & Independent Verification — Milestone 4

**Scope:** A fail-closed local-development trust boundary, one owner-reviewed **read-only** task-status capability, and a separately injected evidence-observer gate. No payment, deployment, shell, file editor, message sender, external tool, autonomous executor, remote identity or production service is enabled.

## What exists

- `src/max_core/trust.py` has a hardcoded allowlist containing **only `task.status`**. Its strict argument schema is exactly `{"task_id":"<same UUID as scoped task>"}`. A model's `ToolProposal` is **not** a trusted capability and cannot register, authorize or execute a new tool.
- `ActorContext` is constructed by trusted host code. It fixes **owner, task, actor/worker, session and device**. The proposal call ID, tool and canonical parameters are hashed together with those identifiers. No model-originated text can supply an owner-approval fact.
- `TrustService.request()` is an **internal method**, not a model-callable/public route. It validates the task owner and action, then writes a short-lived `pending` request and metadata-only audit entry in a DB transaction.
- A **locally authenticated owner** sees pending requests with `GET /v1/approvals` and approves or denies one via `POST /v1/approvals/{uuid}/decision` with exact JSON `{"approve":true|false}`. Both require the Phase 1 owner bearer token. The request response includes the task, actor, session, device, proposal identifier and binding hash so a future UI can present them faithfully. Only the sole read-only action is eligible.
- `TrustService.execute()` is an **internal** trusted-host method: it requires an approved, unexpired, exact-scope grant. It consumes that grant **transactionally**, appends an allow/deny audit row and returns only a task's ID, state and revision—**never task objective or private data**. There is **no generic dispatcher, arbitrary Python call, shell process, network execution or dynamic plugin loading**.
- Unknown actions—such as `shell.run`, `payment.transfer`, `git.merge`, `deploy.production` and `vault.share`—are denied even when a model claims owner authorization. Changed arguments, worker, task, session, device, call ID, consumed/rejected/expired grant or wrong owner cannot use the existing approval.

## Independent outcome gate

Existing Milestone 2 workers can claim a task and record an **unverified** `evidence_digest`, moving it to `awaiting_verification`. That assertion does **not** complete anything.

The new internal `IndependentVerifier` accepts a trusted host-injected `EvidenceReader` interface. The reader fetches the separately observed **raw bytes**, not a digest returned by the model or worker. The verifier hashes those observed bytes and compares the result to the worker's untrusted digest; it never accepts a caller-submitted 'verified' flag.

- Matching observation: append one verification record and audit, advance task to **completed**, and atomically add a `task.verified` outbox event.
- Mismatch, absent evidence, oversized evidence (over 1 MB) or observer exception: record a **mismatch/unavailable** result, **leave the task in `awaiting_verification`**, and do not create a completion event.
- Owner mismatch, queued tasks and already-completed tasks: deny. Row locking prevents concurrent double completion on PostgreSQL. The configured reader must be genuinely independent and limited to a trusted read-only evidence store before any production rollout.
- This release **does not ship an actual external evidence source**. Tests inject fixed independent fixtures. It **cannot prove** that a real email was delivered, a GitHub merge occurred, an external payment succeeded, or a node command ran. That requires authorized source-specific effect reconciliation in a future PR.

## Database and migration

A new additive Alembic migration `0002_trust_verification` adds:
- `max_approval_grants`: exact task-scoped binding hash, actor/session/device/call ID, TTL, pending/approved/denied/consumed/expired state and timestamps.
- `max_trust_audit`: metadata-only allow/deny/pending/approval records. No payloads, prompts, secrets, card data or tool arguments.
- `max_verification_checks`: independent observed and expected SHA-256 digests plus verdict and timestamp.

The existing task/outbox tables are **not rewritten**. `/health/ready` now requires migration revision `0002_trust_verification` and all three new tables. A database at revision `0001_durable_tasks` is deliberately **not ready** until the migration is reviewed and applied.

For an isolated **local development** PostgreSQL database, after a backup and explicit review, update the schema:

```sh
python -m alembic upgrade head
python -m pytest -q
```

**Do not** run Alembic on any production or existing real-data database without separate migration approval, backup and restore plan. No database or deployment was created/changed as part of opening this PR.

## Approval and verification security limits

- The existing bootstrap bearer token protects the owner-decision HTTP routes. That is **NOT strong proof of human presence, step-up MFA, a per-action challenge or a remote/mobile session**. Anyone who gets the local token can approve the one safe read-only action. Therefore all impactful actions remain **unregistered and unapprovable**, even if the local owner route is used.
- Approval ID is a random identifier, **not an exportable permission token**; possession alone is insufficient, because the trusted internal executor also checks full scope, state, digest and expiry under a DB transaction.
- Approval state and audit records are durable, but the tables are **not cryptographically append-only**, and a DB administrator can alter them. Production needs distinct minimum-privilege DB roles and tamper-evident offsite audit.
- Approval consumption occurs before returning the read-only result. This is acceptable for the single non-effectful status read. Do not reuse this 'consume then execute' design for payments or other external effects; those need transactional intent records, idempotency keys, effect reconciliation and task-specific owner confirmation.
- The independently injected observer interface is a **software boundary, not attestation**. Model/worker code must never control the observer or pass arbitrary bytes through its interface. The host, runtime and database remain assumed trusted for this milestone.
- No public ability to request arbitrary grants, mark task verification complete, execute arbitrary tool names, or bypass authorization through a model-proposed action has been added. Models do not possess the local owner bearer token.

## Test and review gates

GitHub CI runs Python 3.11 and 3.13 with ephemeral PostgreSQL 17, Alembic `upgrade head`, Ruff lint/format, strict mypy, pytest, and pip-audit. Unit and security tests cover unknown actions, parameter/actor/session/device/task binding, expiry, cross-owner isolation, rejection/replay, read-only data minimization, audit denials, unverified worker claims, mismatch/unavailable evidence, and duplicate completion. PostgreSQL integration tests race multiple grant consumers and multiple independent verifiers; only one may succeed.

**Record actual CI results on PR #7** rather than assuming success. No live worker/effect source, security penetration test, multi-factor authorization or remote device integration is part of this milestone.

## Owner-controlled merge and rollback

This is a distinct owner-review PR based on the owner-merged Milestone 3. **No automatic merge or deployment.** Closing the unmerged PR has no effect on existing systems. After manual migration, perform data-preserving rollback review: a direct `alembic downgrade` destroys approval/audit/verification history and should not be run against real data.

A later PR may add a **separately sandboxed non-sensitive read-only adapter** and strongly authenticated owner identity. High-risk tools must stay unavailable until their individual threat models, step-up authentication, idempotency and verifier are approved.


## Milestone 7 fixed local worker — no change to Trust permissions

The new [bounded worker](BOUNDED_WORKER.md) is launched manually by the owner against an isolated local development DB; it does **not** run under a model-originated `ToolProposal` or expand the Trust allowlist beyond `task.status`. Its hardcoded `dev.health` and `dev.add` are fixed pure computations, not plugins or general tools. It uses existing `TaskStore.record_outcome()` and never calls the independent verifier. The resulting `awaiting_verification` state is **not a verified success**. Only an independently injected trusted read-only evidence observer can transition to `completed`. A worker cancellation acknowledgment is separately lease-fenced and requires that local work already stopped. These statements apply only to Milestone 7's unmerged review branch and must not be read as authorization for remote or high-risk effects.


## Milestone 8 — separately recomputed dev.add correctness

The [independent local arithmetic observer](INDEPENDENT_EVIDENCE_OBSERVER.md) is a separate **internal trusted** arithmetic evaluator, not a `ToolProposal`, owner approval grant or new model-callable capability. The existing `IndependentVerifier` still owns the final completion transition; it compares a newly computed expected canonical `dev.add` digest against the worker's prior untrusted digest and revalidates the task revision/checkpoint/input under a task lock. Only supported correctness can complete; mismatch/unavailable remains `awaiting_verification`. New provenance fields and owner-global idempotency prevent cross-task request replay. The bootstrap owner token, shared host and PostgreSQL still provide only **local development** integrity assumptions. This does not upgrade the safety or provenance of the earlier fixture reader, and it does not attest actual worker execution.
