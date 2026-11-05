# MAX Technical Design — Provisional

**Status:** Initial architecture direction; not yet frozen. Technology recommendations require validation against current versions, licensing, performance, platform constraints, security, and operational cost.

## 1. Architectural principles

- Modular, provider-independent core.
- Durable task state independent of model sessions.
- Capability registry and explicit authorization middleware.
- Local and hosted execution nodes.
- MCP and native API adapters.
- Replaceable model and speech providers.
- Security and observability designed in from the start.
- Small, independently testable services; avoid premature microservice sprawl.

## 2. Proposed logical components

1. **Client applications:** Windows/Linux orb desktop and iPhone web app.
2. **API and session layer:** authenticated user sessions, streaming events, voice sessions.
3. **MAX Core:** intent handling, planning, policies, delegation, task orchestration.
4. **Model gateway:** provider adapters, capability metadata, routing, budgets, fallback.
5. **Agent runtime:** tool loop, specialist agents, context management, checkpoints, cancellation.
6. **Capability registry:** MCP clients, native API adapters, schemas, permissions, health.
7. **Skill manager:** provenance, inspection, sandboxed install/test, version management.
8. **Memory and knowledge:** PostgreSQL, retrieval/indexing, Google Drive adapter.
9. **Node manager:** authenticated Windows/Linux execution agents and jobs.
10. **Browser service:** dedicated isolated browser profiles and policy-controlled automation.
11. **Voice service:** wake word, STT, TTS, streaming, turn-taking, interruption.
12. **Communications:** email, WhatsApp, telephony adapters.
13. **Personal vault:** envelope encryption, access policy, audit, controlled document delivery.
14. **Evolution engine:** feedback extraction, offline evaluation, regression and proposed updates.
15. **Recovery and supervision:** external process supervisor, health, rollback, bounded repair.
16. **Observability:** structured logs, traces, audit records, alerts, activity journal.

## 3. Candidate technology evaluation

Potential candidates, not final selections:
- Backend: Python/FastAPI for AI and integration ecosystem; Go for local node/supervisor or high-concurrency components; TypeScript for web UI and some desktop integration.
- Desktop: Tauri with a web UI, subject to Windows and Hyprland validation.
- Mobile: responsive web application / PWA.
- Data: PostgreSQL; object storage or Google Drive for selected user documents and exports.
- Local models: Ollama initially, with runtime abstraction.
- Agent frameworks: evaluate OpenClaw as a candidate/reusable component against a custom thin MAX harness. Do not assume it replaces all product-specific requirements.
- Browser: evaluate Playwright/CDP-based isolated browser service and relevant OpenClaw browser features.
- Search: evaluate self-hosted SearXNG plus direct browsing, respecting source restrictions.
- Voice: compare licensed TTS/STT options and open-source options; wake-word processing should be local where feasible.
- Telephony and messaging: use official provider APIs and verify India-specific availability, rules, and pricing.

Choose a minimal stack based on prototypes and benchmarks rather than language preference.

## 4. Model gateway requirements

Each provider adapter must expose normalized request/response and streaming interfaces, model capabilities, context limits, tool-call support, structured-output support, latency, cost/usage accounting, rate limits, and health state. Routing policy should consider task class, quality requirement, availability, latency, cost budget, privacy, and local-only preference.

Secrets are stored in a secret manager/environment injection, never in code or logs. Provider access must be tested for each account/model; owner-reported token balances are not assumed to mean interchangeable or universally available capacity.

## 5. Agent runtime and tools

Persist task ID, objective, plan, current step, dependencies, agent assignments, checkpoint, tool call IDs, approvals, outputs, errors, and verification evidence. Every tool call passes schema validation, authorization, timeout, audit, and output-size limits.

Tool alternatives are allowed only when they carry equivalent authorization; never use fallback to evade denial.

## 6. Local execution node

The Windows/Linux node establishes an authenticated outbound connection to MAX Core, receives scoped jobs, provisions a clean workspace, executes permitted commands, streams logs, persists checkpoints, and cleans up after the task. Use device enrollment, short-lived credentials, signed job claims, command allowlists or policy checks, resource limits, and explicit shutdown conditions.

Do not expose an unrestricted remote shell to the model. Destructive actions, merges to protected branches, production deploys, and shutdown with unsaved work require policy checks/approval.

## 7. Browser and research

Dedicated isolated browser context; no direct access to personal Chrome profile by default. Separate download directory, cookie store, and secrets. Treat page contents as untrusted. Search via SearXNG/direct browsing where permitted; honor terms, robots, rate limits, and access controls. Store source URL, retrieval time, relevant excerpt, and confidence with research notes.

## 8. Data and security model

PostgreSQL is the proposed operational source of truth. Google Drive stores user-selected documents, knowledge exports, and versioned backups. Use encryption, secret management, RBAC/ABAC, audit, retention, data classification, and tested restore. Personal Vault keys should be protected by envelope encryption and hardware-backed/OS credential storage where available. Do not expose plaintext sensitive documents to general-purpose agents unnecessarily.

## 9. Voice and calling

Local wake-word detection; stream audio to STT/voice session; orchestrate task; stream TTS response. Support interruption, barge-in, echo cancellation, device selection, and network-loss fallback. Calls require caller authentication, call preferences, quiet hours, clear AI identity, and retention controls. Verify India telecom and provider constraints before provider choice.

## 10. Reliability

Use distinct liveness, readiness, and dependency health. Recovery attempts are finite and policy-specific; payment and other external side effects require reconciliation before retry. External supervisor monitors the main process. Repair occurs in branch/workspace, with tests and security review before release.

## 11. Initial vertical slice proposal

A minimal secure local development slice should prove:
- Repository standards and CI.
- Authenticated API and user session.
- Persistent task record in PostgreSQL.
- Model gateway interface with one configured provider (secrets local only).
- One safe read-only tool with permission checks and audit.
- Task status streaming to a minimal client.
- Unit/integration tests and reproducible local setup.

Do not begin telephony, payments, vault sharing, autonomous deployment, or unrestricted PC control in the first slice.

## 12. Open architecture decisions

See [DECISIONS.md](DECISIONS.md). No candidate framework, provider, or integration is accepted solely because it appears in this document.

## 13. Phase 0B decision refinement — proposed stack (2026-11-05)

This section is the working candidate baseline; previous sections remain provisional, not proof of implementation.

- **MAX Core:** Python/FastAPI as a modular first-party API, background worker with bounded queue and outbox persisted in PostgreSQL. A Go/TypeScript core was considered but would increase initial ecosystem integration or developer workload without proven benefit. Avoid in-memory authoritative tasks and premature microservices. Core handles task IDs, states, authz context, checkpoints, retry budget, side-effect record and verifier.
- **Database:** PostgreSQL as source of truth, with schema migrations, separate narrowly-scoped DB roles, external encrypted backups and restore drills. Evaluate managed DB price/storage/retention in the first deploy milestone. SQLite remains a viable isolated experiment alternative but is not the proposed hosted durability store.
- **UI:** shared TypeScript components + Tauri 2 desktop prototype (Windows 11 and Hyprland explicitly tested), private iPhone responsive web UI. Electron offers mature desktop APIs at higher memory footprint; native Qt affords deeper OS behavior at greater UI duplication. Wayland floating/pinned/click-through capabilities remain **unverified until a real Hyprland compositor test**. Do not promise background iOS voice on a PWA.
- **Model transport:** MAX-owned request and response schema, separate OpenAI Chat Completions-style and Anthropic Messages-style adapters, optional vetted FreeLLMAPI and direct provider backends, local Ollama adapter. Capability and billing metadata must be probed per provider, not inferred from a compatible URL. No cloud fallback from local-only data. See [MODEL_GATEWAY.md](MODEL_GATEWAY.md).
- **Tools and agents:** typed allowlisted capability registry with trusted MAX Trust authorization on every call. MCP transport authorization is not MAX's own permission model; malicious tool output cannot alter scope. Use Playwright browser contexts **plus separate process/network isolation** for hostile research, and sandbox acquired skills. [Official MCP spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization), [Playwright isolation](https://playwright.dev/docs/browser-contexts).
- **Voice:** local opt-in wake/VAD, switchable STT/TTS adapters, interruption and privacy controls. Licensing varies between openWakeWord code and bundled weights; a licensed original voice is required. Measure real device-to-first-audio latency rather than accepting model benchmarks.
- **Telephony:** no provider chosen; published Twilio India domestic voice restrictions block treating the service as a guaranteed Indian two-way solution. Prefer authenticated in-app voice first, and run a real provider/regional feasibility gate later.
- **OpenClaw:** reference implementation/patterns only, not foundation. Full tradeoffs in [OPENCLAW_EVALUATION.md](OPENCLAW_EVALUATION.md). OpenHands SDK may later support **isolated coding jobs**, not MAX Core ownership. Hermes Agent is useful for learning/skill lifecycle comparisons, not permission delegation.
- **Security:** central deny-by-default policy, exact-action approval, separate device node enrollment, keyed secrets, data classification, owner-controlled PRs, independent verification and supervised recovery. See [THREAT_MODEL.md](THREAT_MODEL.md).

**Cost/risk note:** Python+Postgres+worker+browser+speech+desktop may introduce multiple operational dependencies. Add them progressively. Actual hosting, storage, GPU, token, telecom and licensing costs are **unknown until quoted/measured**; no 'free forever' claim. Prototype resource limits and backup restoration before deployment.

Eight explicit diagrams: [ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md). Primary-source evidence and unresolved claims: [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md). Test cases: [VALIDATION_PLAN.md](VALIDATION_PLAN.md). No runtime or infrastructure changes were made in Phase 0B.
