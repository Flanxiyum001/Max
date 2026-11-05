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
