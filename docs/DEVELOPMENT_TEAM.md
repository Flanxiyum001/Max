# Bounded software development team — first runtime slice

This milestone implements a host-injected orchestration library inside MAX. It is
not a running autonomous coding service. Model-backed workers, an isolated coding
executor, persistent team jobs, the Windows agent, phone UI and release/deployment
adapters remain separate integrations. No new model, shell or MCP permissions are
granted, and the existing MAX Trust capability registry is unchanged.

## Intended workflow

A trusted host defines the task, revision, specialist identities, evidence for the fixed required checks,
budgets and whether UI work needs review. An implementation worker produces a
candidate; independent code-review, QA and security specialists evaluate that
candidate. UI/UX review is required when declared by the host. Agent opinions do
not prove that tests executed: a separate host-owned check observer supplies
revision-bound test evidence. Failed/missing checks and unresolved blocking issues
must prevent readiness. The orchestrator may return a review outcome; it cannot
merge, deploy, mark a Core task completed or grant itself execution rights.

Specialists receive bounded assignments and no executable tool grants from this
library. Independent identities are a host-enforced assignment boundary, not
proof that different people, model vendors or isolated processes performed the
work. Trusted adapters and process isolation are still required before real work.
All implementation and reviewer output is untrusted data. Never derive host
identity bindings, required check sets or permission policy from model output.

## Acceptance and release boundaries

Fixture tests exercise controller decisions and failure handling. Passing those
tests proves library behavior only. A live development workflow additionally needs
an approved model connection, isolated worker execution, independently collected
check evidence, durable recovery, scoped GitHub access and exact-commit CI checks.
Real provider and Windows acceptance remain unverified until run on the owner's
machine. Router wake-up remains paused.

Use the full repository lint, formatting, strict typing, unit/security suite,
PostgreSQL migration/integration suite and dependency audit. Use independent code
review of the final change and regression tests for actionable findings. Never
replace a failed or missing check with an agent's statement that everything works.

Rollback: revert this milestone's new library/tests/docs. There are no migrations,
new API routes, live credentials, account changes or deployed services to undo.

## Controller contract

`DevelopmentTeam.run(task_id, revision, objective, has_ui=...)` invokes explicitly
registered asynchronous adapters. Actor IDs must be distinct across roles. Code
review, QA and security are mandatory; UI/UX is conditional. The task starts from
a full Git commit SHA and each implementation round returns a candidate SHA.
Reviewers and check evidence must refer to that candidate. A new candidate causes
all required reviewers and checks to run again.

The default budget is two rounds (configurable from one to three) under one
cooperative deadline, capped at 300 seconds. Cancellation propagates rather than
being translated into success. An adapter that blocks the event loop or ignores
cancellation cannot be forcibly stopped by this library; use host process isolation
and external supervision for real workers. No background task or recursive agent
creation occurs here.

Required check categories are lint, formatting, typing, unit/security, PostgreSQL
and dependency audit. Skipped/unavailable, failed, missing, duplicate or stale
results cannot yield readiness. All returned issues block readiness in this first
slice; severity-based exceptions and waiver flows are deliberately not implemented.
A clean report returns `ready_for_owner_review`, never `released` or `completed`.
It is advisory outside the library and is not a gate wired into GitHub deployment
or the Core task state machine. An integration must enforce its own release policy.

The host determines `has_ui`; the controller does not inspect a diff to discover UI
changes. Any additional supplied checks must pass as well as the six fixed
required categories. A host adapter must collect the full affected workflow checks.
