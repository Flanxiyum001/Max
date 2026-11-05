# MAX agent runtime

**Status:** Proposed behavior.

Tasks persist independently of a model session. Lifecycle: queued -> planning -> awaiting approval -> running -> verifying -> completed / partially completed / failed / cancelled. Each transition records actor, timestamp, checkpoint, reason and evidence.

MAX Core owns delegation. Specialist workers receive bounded task context, tool grants, time budgets and explicit output schemas. A worker cannot spawn unlimited sub-workers, expand its own scopes or modify authorization policy.

Every tool call has a stable ID, deadline, validated input, policy decision, idempotency classification and audit record. On restart, reconcile uncertain external effects before retry. Cancellation stops pending steps and reports already committed effects accurately.

Verification uses authoritative read-back or a trusted test result, not a model assertion. Keep logs sanitized and retain enough evidence for debugging.

**Exit gates:** restart/resume; cancellation; failed tool; duplicate delivery; conflicting subagents; rate limit; unauthorized fallback; partial success; bounded recovery; verifiable read-only vertical slice.

## Implemented-state reconciliation — post-PR #7 (2026-11-05)

The proposed `planning`, `awaiting approval` and `verifying` orchestration phases above are **future agent-planner concepts**, **not** the states in the current `max_tasks.state` column. The implemented store uses `queued -> running -> awaiting_verification -> completed` (the final transition requires trusted-reader matching evidence), plus `queued -> cancelled`, `running -> cancelling`, and stale lease `running/cancelling -> needs_reconciliation`; checkpoints increment revision while running. A separate approval-grant ledger has pending/approved/denied/consumed/expired states. **There is no live task worker, planner, external effect dispatcher or bounded specialist runtime yet.** See [TASK_PERSISTENCE.md](TASK_PERSISTENCE.md), [TRUST_IMPLEMENTATION.md](TRUST_IMPLEMENTATION.md) and [PHASE0_AUDIT.md](PHASE0_AUDIT.md).


## Milestone 7 manually invoked development worker — review branch (2026-11-05)

The **only** initial executable capability is a local operator-launched [bounded worker](BOUNDED_WORKER.md), **not** the proposed planner/agent runtime above. The worker requires a queued owner task with exact JSON operation schema and an existing PostgreSQL lease. Two constant-time pure operations are allowlisted; result digests are stored via the existing unverified outcome gate. The added worker-only cancellation acknowledgment uses an unexpired exact owner/worker lease and executes **only after local work has stopped**. Faults enter the pre-existing `needs_reconciliation` state, never an automatic retry. No automatic scheduler, trust-tool permission expansion or independent evidence source is shipped. See [Trust boundaries](TRUST_IMPLEMENTATION.md).


## Milestone 8 — internal dev.add expected-result observer (review branch)

A manually invoked, non-agent [independent deterministic observer](INDEPENDENT_EVIDENCE_OBSERVER.md) can separately evaluate a `dev.add` result against a worker's untrusted SHA-256 claim. The existing trusted verifier remains responsible for an attempt-bound, transactional `awaiting_verification → completed` transition and event; contradicted or inconclusive evidence never auto-retries a worker. No general agent planner, long-running observer scheduler, external event attestation or high-risk execution capability is implemented. Completion in this restricted prototype means **the mathematical answer was independently supported**, not worker-execution attestation.
