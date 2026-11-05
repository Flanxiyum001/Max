# MAX evolution and recovery engine

**Status:** Proposed design.

Learn through owner-approved memory corrections, evaluated reusable workflows, and validated capability upgrades. Distinguish candidate improvements from deployed changes.

Observe -> propose -> evaluate on regression fixtures -> review risk -> apply reversible low-risk change or request approval -> monitor -> rollback on regression. Production code, permissions, authentication, vault access and payment behavior cannot be silently rewritten.

Recovery classifies failures, checkpoints evidence, retries only within policy budgets, isolates proposed repairs, tests them, and escalates rather than looping. An independent process supervisor handles main-process failure with finite restart limits.

**Tests:** repeated failure fingerprint, retry exhaustion, non-idempotent side effect, unauthorized self-edit, rollback failure, supervisor crash and degraded mode.
