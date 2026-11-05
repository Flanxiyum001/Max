# MAX implementation plan — reviewable assignments

**Status:** Historical milestone plan, updated with current post-PR #7 audit below. Estimates are not delivery promises.

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

## 0B documentation milestone scope and exit gate

**0B branch**: `docs/phase0b-architecture-closure`, based on the owner-merged `max` after PR #2. This milestone adds research, regional feasibility, architectural diagrams and a requirement traceability checklist, and reconciles existing specs/decision logs. **No runtime, secret, infrastructure, device, account, database or production operations.**

**Owner review checklist:**
1. Confirm the recommendation to keep MAX-owned core, trust and gateway contracts with versioned third-party adapters; OpenClaw/Hermes/OpenHands are optional evaluated dependencies.
2. Confirm the FreeLLMAPI naming ambiguity remains an **open question**, with both API wire formats preserved.
3. Confirm India calling needs region/number/consent validation; no telephony feature is promised.
4. Confirm storage/backup and original voice licenses remain proposed until tested/approved.
5. Review the 19 required documents via [REQUIREMENT_TRACEABILITY.md](REQUIREMENT_TRACEABILITY.md) and eight graphs via [ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md); note [REPO_INVENTORY.md](REPO_INVENTORY.md) limitations.
6. Review actual GitHub checks, internal links and any test exceptions disclosed on the PR.
7. **Owner manually merges; never auto-merge or deploy.** Next runtime milestone starts from the owner-merged default branch, not this pending branch.

The full recursive source-tree inventory and comparison against any additional private owner specification files not furnished to this run are **open documentation verification items**. Do not misstate them as completed.

## Milestone 1 — secure local MAX Core bootstrap (this PR)

A new first-party Python/FastAPI package adds: strong explicit environment configuration; **loopback-only** CLI binding; a temporary bearer token for local development; request IDs and allowlisted JSON logs; liveness/readiness; two protected status routes; package metadata; negative-case tests; and PR CI for lint, formatting, typing, tests and a dependency audit. Setup: [CORE_BOOTSTRAP.md](CORE_BOOTSTRAP.md).

**Acceptance:** missing/weak token fails before serving, non-loopback bind rejected, duplicate/malformed auth denied, spoofed Host rejected, no secret in logs/repr, unexpected 500 sanitized, pytest locally passing, CI results actually verified on PR. Do not present this single-token bootstrap as secure remote authentication. No DB or provider changes. No deployment.

**Next review gate:** after owner manually merges the milestone-1 PR, milestone 2 creates *durable* PostgreSQL task state, checkpoints and transactional outbox on a new branch. It must not reuse the bootstrap token as public production auth.


## Milestone 2 — durable task state (this PR)

PostgreSQL and Alembic migration establish versioned task state, owner-scoped idempotent creation, worker leases and checkpoints, cancellable workflow transitions, restart/lease recovery quarantine and a transactional **metadata-only** outbox. Exposed HTTP routes only allow owner task create, list, get and cancel; worker claiming and event delivery remain internal, with **no external effects**. A worker's claimed result remains **awaiting_verification**, not completed. Setup and safeguards: [TASK_PERSISTENCE.md](TASK_PERSISTENCE.md).

Acceptance gates: local unit tests, GitHub Actions PostgreSQL migrations and real concurrent worker tests, owner review of backwards-compatible bootstrap API, no database provisioning or deployment. Next: model gateway and independent verifier as small owner-reviewed PRs.

## Milestone 3 — first-party MAX Model Gateway (this PR)

Internal async Python library with OpenAI Chat Completions and Anthropic Messages request/response + SSE normalization; explicit model metadata/capabilities; local-only privacy enforcement; remote opt-in with priced admission ceiling; deterministic routing; inert tool proposals; strict parsing and bounded streams; safe non-streaming fallback limited to pre-body 429/503. Mock-provider fixture tests only: no owner FreeLLMAPI account or provider secrets, deployment or new public endpoint. See [GATEWAY_IMPLEMENTATION.md](GATEWAY_IMPLEMENTATION.md) for runbook, caveats and tests.

**Acceptance:** retain all previous core/PostgreSQL tests, pass both CI Python versions, verify no credential/redirect leakage, and document unknown account quotas, tokenization, global budgets and future MAX Trust. Owner manually reviews and merges. Following PR: MAX Trust authorization and independently verified task outcomes.

## Milestone 4 — MAX Trust and independent observer verification (this PR)

An additive `0002_trust_verification` migration, deny-by-default approved action registry containing only `task.status`, scoped short-lived single-use approvals, owner-authenticated **local-only** review/decision routes, metadata-only audit, and evidence-source-injected independent result verification. Verified results alone can transition `awaiting_verification` -> `completed` with a transactional event; all uncertain outcomes remain unverified. No arbitrary tool dispatch, live external effects, model-owned approval, shell, payment, deployment or remote identity.

**Acceptance:** owner/device/session/call/parameter binding, expiry/replay denial, cross-owner isolation, mismatches never complete, concurrent PostgreSQL approval and verifier one-winner tests, zero approval action on high-risk tools, migrations/ready checks, CI Python 3.11 and 3.13 all green. Detailed boundaries: [TRUST_IMPLEMENTATION.md](TRUST_IMPLEMENTATION.md).

**Release gate:** owner manually merges this PR after CI and migration review. External read-only tools, secure identity and actual source-specific verification are later isolated milestones.

## Current-status interpretation — requirements audit 2026-11-05

The sections titled "this PR" above were written **during historical milestone development**. They are retained as implementation records, not instructions to reopen or redo merged PRs. As of `max` commit `72f67b1ec6a737c813f4ca633457a9ca39223b94`, foundation PRs #1–#3 and local Core/DB/Gateway/Trust PRs #4–#7 are **merged**. No actual connected provider, autonomous worker, device/voice/web client, Drive/vault/telephony or production server is implemented.

**Phase 0 closure:** architecture/design coverage exists, but still requires explicit [exit-criteria review](PHASE0_EXIT_CRITERIA.md) and [owner-requirement gap audit](PHASE0_AUDIT.md). Only a **local read-only owner task timeline** is recommended as the next small implementation; that work needs its own owner-reviewed PR. Never infer provider/account compatibility from OpenAI/Anthropic mock fixtures or authorization for external effects from the single approved task-status read.
