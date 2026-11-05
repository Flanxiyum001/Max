# MAX agent runtime

**Status:** Proposed behavior.

Tasks persist independently of a model session. Lifecycle: queued -> planning -> awaiting approval -> running -> verifying -> completed / partially completed / failed / cancelled. Each transition records actor, timestamp, checkpoint, reason and evidence.

MAX Core owns delegation. Specialist workers receive bounded task context, tool grants, time budgets and explicit output schemas. A worker cannot spawn unlimited sub-workers, expand its own scopes or modify authorization policy.

Every tool call has a stable ID, deadline, validated input, policy decision, idempotency classification and audit record. On restart, reconcile uncertain external effects before retry. Cancellation stops pending steps and reports already committed effects accurately.

Verification uses authoritative read-back or a trusted test result, not a model assertion. Keep logs sanitized and retain enough evidence for debugging.

**Exit gates:** restart/resume; cancellation; failed tool; duplicate delivery; conflicting subagents; rate limit; unauthorized fallback; partial success; bounded recovery; verifiable read-only vertical slice.
