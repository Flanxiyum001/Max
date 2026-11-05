# MAX implementation plan — reviewable assignments

**Status:** Planning only. Do not treat estimates as delivery promises.

## PR 0A — first-party foundation and project ownership (current)
Personal-project README, coding-agent instructions, baseline architecture, secure module boundaries and staged plan. Exit: owner review of these documents. **This does not mean Phase 0 research and all 19 required deliverables are complete.**

## PR 0B — Phase 0 research and documentation closure (after owner merges 0A)
Complete the architecture assignment: reconcile the current repository with the proposed design; research source-backed technology alternatives, compatibility and costs; update the existing product/technical design/decisions/roadmap documents; add telephony, data and storage, threat model, and missing diagrams; validate links and document outstanding owner decisions. Exit: every assigned documentation deliverable is present and verified, with any unverified provider/platform claims marked unresolved.

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

## Owner review gate
Every PR targets `max` from its current owner-approved tip. The owner reviews and merges manually. No automatic merges, deployments, credentials, or runtime changes are implied by opening a PR. The next milestone starts only after the predecessor is merged (unless the owner explicitly approves a stacked PR). The PR body must disclose actual checks and unresolved risks.
