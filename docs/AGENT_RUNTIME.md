# MAX agent runtime

**Status:** Proposed behavior.

Tasks persist independently of a model session. Lifecycle: queued -> planning -> awaiting approval -> running -> verifying -> completed / partially completed / failed / cancelled. Each transition records actor, timestamp, checkpoint, reason and evidence.

MAX Core owns delegation. Specialist workers receive bounded task context, tool grants, time budgets and explicit output schemas. A worker cannot spawn unlimited sub-workers, expand its own scopes or modify authorization policy.

Every tool call has a stable ID, deadline, validated input, policy decision, idempotency classification and audit record. On restart, reconcile uncertain external effects before retry. Cancellation stops pending steps and reports already committed effects accurately.

Verification uses authoritative read-back or a trusted test result, not a model assertion. Keep logs sanitized and retain enough evidence for debugging.

**Exit gates:** restart/resume; cancellation; failed tool; duplicate delivery; conflicting subagents; rate limit; unauthorized fallback; partial success; bounded recovery; verifiable read-only vertical slice.

## Implemented-state reconciliation — post-PR #7 (2026-11-05)

The proposed `planning`, `awaiting approval` and `verifying` orchestration phases above are **future agent-planner concepts**, **not** the states in the current `max_tasks.state` column. The implemented store uses `queued -> running -> awaiting_verification -> completed` (the final transition requires trusted-reader matching evidence), plus `queued -> cancelled`, `running -> cancelling`, and stale lease `running/cancelling -> needs_reconciliation`; checkpoints increment revision while running. A separate approval-grant ledger has pending/approved/denied/consumed/expired states. **There is no live task worker, planner, external effect dispatcher or bounded specialist runtime yet.** See [TASK_PERSISTENCE.md](TASK_PERSISTENCE.md), [TRUST_IMPLEMENTATION.md](TRUST_IMPLEMENTATION.md) and [PHASE0_AUDIT.md](PHASE0_AUDIT.md).
