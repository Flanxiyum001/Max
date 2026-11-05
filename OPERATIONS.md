# OPERATIONS — Health, Recovery, and Supervision

## Service supervision

The main MAX process cannot be its only supervisor. Use an independent supervisor appropriate to each deployment environment. Separate liveness, readiness, and dependency health.

## Recovery sequence

1. Detect failure through process state, health signals, logs, and task outcomes.
2. Capture logs, stack traces, recent changes, and relevant system state.
3. Classify the failure.
4. Select an appropriate bounded strategy: retry transient errors, use an authorized fallback, restart a component, roll back, or propose a repair.
5. Perform code repair in an isolated branch/workspace.
6. Run targeted tests, regression checks, security checks, and build verification.
7. Deploy through controlled release procedures.
8. Verify service health and the original user outcome.
9. Record root cause and learning.

## Loop prevention

Use configurable finite retries, exponential backoff, circuit breakers, repair budgets, failure fingerprints, cooldowns, dead-letter handling, safe/degraded mode, and escalation. A proposed baseline is no more than three attempts per repair cycle; specific error classes may require fewer or no retries.

## Recovery layers

- Internal recovery engine: diagnoses tasks and components.
- Independent supervisor: detects and restarts failed processes within limits.
- Infrastructure/platform recovery: Railway for hosted services, and Windows service supervision/systemd for local nodes.

## Operations rules

- Preserve task checkpoints across restarts.
- Never restart indefinitely without evidence and escalation.
- Never disable authentication or security controls to restore availability.
- Never delete user data to make tests pass.
- Do not repeat external side effects without reconciliation and idempotency.
- Notify the owner when intervention or approval is required.
