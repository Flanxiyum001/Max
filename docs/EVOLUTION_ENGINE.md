# MAX evolution and recovery engine

**Status:** First slice implemented in M40 (outcome ledger, owner-approved lessons, supervisor) — see [M40](M40_SELF_HEALING_EVOLUTION_JOBS_COMMS.md).

Learn through owner-approved memory corrections, evaluated reusable workflows, and validated capability upgrades. Distinguish candidate improvements from deployed changes.

Observe -> propose -> evaluate on regression fixtures -> review risk -> apply reversible low-risk change or request approval -> monitor -> rollback on regression. Production code, permissions, authentication, vault access and payment behavior cannot be silently rewritten.

Recovery classifies failures, checkpoints evidence, retries only within policy budgets, isolates proposed repairs, tests them, and escalates rather than looping. An independent process supervisor handles main-process failure with finite restart limits.

**Tests:** repeated failure fingerprint, retry exhaustion, non-idempotent side effect, unauthorized self-edit, rollback failure, supervisor crash and degraded mode.
