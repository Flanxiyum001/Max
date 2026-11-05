# MAX implementation plan — reviewable assignments

**Status:** Planning only. Do not treat estimates as delivery promises.

## PR 0 — documentation and decision gates
Complete architecture, threat model, interfaces, platform proof-of-concept plan, source-of-truth index and ADRs. Exit: all required documents exist and distinguish confirmed requirements from proposals.

## PR 1 — secure project skeleton
Implement configuration validation, authenticated API skeleton, structured redacted logs, health endpoints, CI lint/type/test/security checks. Exit: reproducible clean setup, negative auth tests, no committed secrets.

## PR 2 — durable task vertical slice
Add transactional task records, migrations, cancellation, checkpoints and event streaming. Exit: restart recovery, no duplicate external side effects, integration tests.

## PR 3 — MAX Gateway
Add provider-neutral contract, mock adapter and one configured adapter; enforce privacy and budgets. Exit: outage, fallback, streaming and cancellation tests.

## PR 4 — MAX Trust and one safe tool
Add deny-by-default capability registry, permission enforcement, audit and read-only tool. Exit: denial, replay, injection and evidence tests.

## PR 5 — desktop and voice prototypes
Test orb/window behavior on Windows and Hyprland, responsive mobile UI, microphone lifecycle and interruption. Exit: measured device evidence and accessible fallback.

## Later phases
Specialist workers; isolated skill system; local execution node; memory retrieval and backup restore; research browser; personal integrations; vault; evolution and supervisor; production hardening.

## Assignment template
Objective; user outcome; scope; excluded work; files; security and privacy constraints; implementation steps; tests with exact commands; acceptance evidence; rollback; unknowns; approvals. Each PR should remain small and reviewable.

## Hard boundaries
No deployment, financial actions, vault sharing, automatic merges, local installations or modification of unrelated repositories without separate authorization.
