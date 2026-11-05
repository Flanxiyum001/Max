# MAX Development Roadmap

Order is provisional and may change after architecture and risk review.

## Phase 0 — Architecture and repository foundation
- Establish source-of-truth documentation.
- Evaluate OpenClaw and relevant components.
- Select stack and deployment topology.
- Define threat model, authorization, secrets, data classification, and acceptance standards.
- Establish CI and development workflow.

## Phase 1 — Secure MAX Core
- API, authentication, configuration, health, structured logs.
- PostgreSQL persistence and migrations.
- Model gateway abstraction; one provider adapter.
- Durable task lifecycle, cancellation, checkpoints.
- One read-only tool with authorization and audit.
- Minimal client status view.

## Phase 2 — Agent harness and tools
- Planning/execution loop.
- Tool registry and permission middleware.
- MCP client and native adapter framework.
- Dynamic bounded sub-agents.
- Verification and activity journal.

## Phase 3 — Desktop, mobile, and voice foundation
- Orb prototype based on supplied reference.
- Windows and Hyprland proof of concept.
- iPhone responsive web client.
- Voice activation, STT/TTS, streaming, interruption.
- Gaming/focus mode.

## Phase 4 — Local execution and coding
- Secure Windows/Linux node enrollment.
- Isolated project workspaces.
- GitHub workflow and test execution.
- Wake-on-LAN feasibility, safe shutdown, recovery.

## Phase 5 — Skills and research browser
- Skill registry, inspection, sandbox, validation and rollback.
- Dedicated browser.
- SearXNG/direct research evaluation.
- Source provenance and prompt-injection protections.

## Phase 6 — Memory, Drive, and Evolution
- Memory lifecycle and retrieval.
- Google Drive integration.
- Versioned backups and restore tests.
- Feedback and outcome evaluation.
- Controlled workflow improvement.

## Phase 7 — Personal integrations
- Spotify and home automation.
- Email and WhatsApp.
- Authenticated inbound/outbound calling.
- Notifications and preferences.

## Phase 8 — Personal Vault and finance
- Threat model and key management review.
- Encrypted document storage.
- Authenticated access and recipient confirmation.
- Payment-provider research and trusted approval flow.
- Independent security testing before any live financial use.

## Phase 9 — Production hardening
- Independent supervisor and bounded recovery.
- Observability, rate limits, backups, restore drills.
- Load, failure, security, and regression tests.
- Controlled rollout and operational runbooks.

Each phase must have scoped assignments, tests, acceptance criteria, owner-visible status, and rollback instructions. No phase is complete based on generated code alone.

## Milestone PR governance and Phase 0B closure

**Default branch is `max`, not `main`.** Owner merges every milestone PR manually. Each new PR starts from the last owner-merged `max` tip; no auto-merge or deployment. Existing 0A PR #2 established personal project identity, initial docs and the review process. **0B** closes the architecture research/document gap by adding source-backed evaluations, an India telephony risk gate, data and threat models, traceability and eight diagrams. Its scope is **documentation only**; it is not a completed runnable MAX feature.

### Implementation review order after 0B owner approval

| Milestone PR | Target | Observable exit and minimum negative cases |
| --- | --- | --- |
| 1 | Secure core skeleton | Config/auth/redaction, status API, CI, unauthenticated request rejected |
| 2 | Durable tasks/outbox | Migrations, restart recovery, cancel and duplicate-side-effect tests |
| 3 | Dual-format model gateway | OpenAI/Anthropic fixtures, budget/privacy routing, outage/streaming tests |
| 4 | MAX Trust + one safe tool | Exact-action approval, audit, replay denial and verified read-only result |
| 5 | Desktop/mobile/voice prototypes | Real Windows/Hyprland foreground/focus checks and iPhone foreground use |
| 6+ | Bounded agents/nodes/skills/browser/memory | Each capability isolated, tested and individually owner merged |
| Later | Communications/vault/evolution/production hardening | Consent/licensing, regional legality, independent review and restore tests |

Do not combine unrelated stages in an oversized PR or promise milestones are delivered based on documents. Milestone-specific scope, proof and risk templates: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), [VALIDATION_PLAN.md](VALIDATION_PLAN.md), [REQUIREMENT_TRACEABILITY.md](REQUIREMENT_TRACEABILITY.md).

## Phase 1 milestone-1 implementation boundary

The first runtime PR adds **local-only** MAX Core packaging, process health, minimal authenticated owner endpoints, safe access logging and automated quality checks. It intentionally does **not** satisfy the full Phase 1 line items above (persistent tasks, PostgreSQL, model gateway and tools). Those are separate, owner-reviewed PRs.

Development API setup and explicit security limitations: [CORE_BOOTSTRAP.md](CORE_BOOTSTRAP.md). Each PR is merged by the owner manually into `max`; no automated production deployment.


## Milestone 2 implementation slice

A durable PostgreSQL repository with explicit Alembic migration, idempotent create, cancel, bounded worker lease/checkpoint and internal outbox is proposed in a focused PR. It does not provide a worker, real AI, tool execution or remote deploy. Read [TASK_PERSISTENCE.md](TASK_PERSISTENCE.md). This PR must pass both Python-version CI matrices and be owner-merged before proceeding to the model gateway.

## Milestone 3 implementation slice

The owner-controlled MAX Gateway is now a **Python library**, not a publicly reachable service. Its explicit adapters handle OpenAI-compatible Chat Completions and Anthropic Messages with normalized text/stream/tool proposals, privacy routing and per-request budget admission. No selected FreeLLMAPI endpoint or real provider credentials have been configured. Model text cannot trigger tool execution. See [GATEWAY_IMPLEMENTATION.md](GATEWAY_IMPLEMENTATION.md). The next owner-reviewed milestone handles MAX Trust and independent verification.

## Milestone 4 implementation slice

MAX Trust now has an isolated approval ledger and a strictly scoped `task.status` capability, with owner-authenticated local decision endpoints. Independent completion verification relies on **host-injected evidence observations**, not self-attested model/worker claims. A new additive schema migration supports durable approval, audit and verification records. This is **not** an unrestricted tool executor or a production-grade user identity. See [TRUST_IMPLEMENTATION.md](TRUST_IMPLEMENTATION.md). All real side-effectful capabilities remain explicitly blocked pending a separate owner review.


## Current merged implementation and next gate — audit 2026-11-05

This roadmap's Phase 0 and Phase 1 PR table above is **historical planned ordering**, not the present branch state. Against `max` commit `72f67b1ec6a737c813f4ca633457a9ca39223b94`, the owner already merged the local Core, durable task persistence, dual-protocol mock Gateway and limited read-only Trust/verifier PRs (#4–#7). **No model account, worker, browser/mobile/desktop client, node or deployment exists in this tree.**

**Recommended next reviewable implementation PR (not started):** [local owner-only read-only task timeline + terminal client](PHASE0_AUDIT.md). After that: independently review provider identity/FreeLLMAPI sandbox contract, stronger authentication, source-specific read-only evidence, then bounded agent/workers; introduce desktop/voice only once focus/device constraints can be physically tested. Side-effectful integrations remain separate high-risk milestones.

**Blocking dependencies:** Do not treat a green mock CI suite as live FreeLLMAPI, India PSTN, OS compatibility, key-recovery or backup proof. Use [Phase 0 exit criteria](PHASE0_EXIT_CRITERIA.md) and [current requirement matrix](REQUIREMENT_TRACEABILITY.md) for gate-specific release decisions. The owner personally reviews and merges each PR; no auto-merge or deployment.
