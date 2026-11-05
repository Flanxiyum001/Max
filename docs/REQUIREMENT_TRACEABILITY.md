# MAX Phase 0 deliverable and acceptance traceability

**Historical Phase 0B document inventory (2026-11-05):** 'Present' below meant the architecture document existed at that review, not that the capability ran. Current implementation and test evidence are audited separately below against merged `max` at `72f67b1ec6a737c813f4ca633457a9ca39223b94`.

## Required 19 documents

| # | Required document | Status after PR 0B | Primary coverage |
| --- | --- | --- | --- |
| 1 | [AGENTS.md](../AGENTS.md) | Present | Development-worker instructions distinct from MAX identity |
| 2 | [ARCHITECTURE.md](../ARCHITECTURE.md) | Present | Logical topology, trust boundaries |
| 3 | [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md) | Present, reconciled | Proposed stack and contracts |
| 4 | [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md) | Present, reconciled | Owner requirements, scope |
| 5 | [DECISIONS.md](DECISIONS.md) | Present, reconciled | Confirmed vs proposed vs blocked |
| 6 | [ROADMAP.md](ROADMAP.md) | Present, reconciled | Owner-gated milestones |
| 7 | [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md) | Present | Trust and policy |
| 8 | [MODEL_GATEWAY.md](MODEL_GATEWAY.md) | Present, expanded | OpenAI/Anthropic compatible adapter boundaries |
| 9 | [AGENT_RUNTIME.md](AGENT_RUNTIME.md) | Present | Task transitions and delegation |
| 10 | [SKILL_SYSTEM.md](SKILL_SYSTEM.md) | Present | Dynamic skill checks |
| 11 | [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md) | Present | Memory governance |
| 12 | [EVOLUTION_ENGINE.md](EVOLUTION_ENGINE.md) | Present | Controlled changes |
| 13 | [VOICE_ARCHITECTURE.md](VOICE_ARCHITECTURE.md) | Present | Original licensed voice plan |
| 14 | [DESKTOP_ARCHITECTURE.md](DESKTOP_ARCHITECTURE.md) | Present | Windows/Hyprland/mobile |
| 15 | [DEVICE_EXECUTION.md](DEVICE_EXECUTION.md) | Present | Scoped nodes |
| 16 | [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md) | Added 0B | Provider/regional gates |
| 17 | [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md) | Added 0B | Encryption, backup and restore |
| 18 | [THREAT_MODEL.md](THREAT_MODEL.md) | Added 0B | Threats and negative tests |
| 19 | [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Present, owner-gated | Sequence and PR scopes |

Supplementary: [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md), [DEPENDENCY_EVALUATION.md](DEPENDENCY_EVALUATION.md), [OPENCLAW_EVALUATION.md](OPENCLAW_EVALUATION.md), [ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md), [REPO_INVENTORY.md](REPO_INVENTORY.md), [VALIDATION_PLAN.md](VALIDATION_PLAN.md).

## Requirements to evidence

| Owner requirement | Design references | Implementation acceptance proof (future) |
| --- | --- | --- |
| MAX-owned personal assistant identity | MAX.md, README, AGENTS, ARCHITECTURE | Identity loaded by runtime, no dependency impersonation |
| Persistent tasks and workers | AGENT_RUNTIME, DATA_AND_STORAGE | Restart, cancellation, recovery, side effect idempotence |
| FreeLLMAPI/dual formats and model budgets | MODEL_GATEWAY, RESEARCH_FINDINGS, DECISIONS | Two protocol fixtures, real vetted account smoke test, privacy and fallback denial |
| OpenClaw evaluation | OPENCLAW_EVALUATION, RESEARCH_FINDINGS | Explicit go/no-go ADR with version/license if adopted |
| Permissions and financial safety | THREAT_MODEL, SECURITY_ARCHITECTURE | Exact-action approval, replay refusal, no secrets |
| Skill discovery and self-improvement | SKILL_SYSTEM, EVOLUTION_ENGINE | Isolated malicious-skill tests, rollback |
| Desktop, iPhone and voice | DESKTOP_ARCHITECTURE, VOICE_ARCHITECTURE | Physical Windows/Hyprland/iPhone test matrix, consent and interruption |
| Browser and device nodes | DEVICE_EXECUTION, RESEARCH_FINDINGS | SSRF, no personal Chrome, node isolation, safe shutdown |
| Data/Drive and memory | DATA_AND_STORAGE, MEMORY_ARCHITECTURE | Scoped OAuth, restore, deletion and provenance |
| India calling, WhatsApp and email | TELEPHONY_ARCHITECTURE | Provider route+regulatory verification, call authentication |
| Independent recovery | OPERATIONS.md, IMPLEMENTATION_PLAN | Crash loop limits and independent supervisor |

## Phase 0B acceptance

- [x] All 19 required documents fetched on the milestone branch and non-empty; see [PHASE0B_VALIDATION_REPORT.md](PHASE0B_VALIDATION_REPORT.md).
- [x] All 22 changed files' local link destinations checked against known document paths; no unknown targets found.
- [ ] Eight Mermaid graphs rendered and visually validated; presence verified, rendering not run.
- [ ] All user-supplied owner specification documents checked against this register. If those private source documents were not available, record the gap rather than inventing requirements.
- [ ] Complete recursive tracked-file inventory, not just known-path reads; see REPO_INVENTORY limitations.
- [ ] Any provider account capability and local device test completed (not in Phase 0; carry forward as explicit gates).
- [ ] Owner manually reviews and merges; do not auto-merge or deploy.

**Do not mark an incomplete check as passed.** A document list is not a security audit or technical proof.


## Current owner-requirement-to-evidence matrix — post-PR #7 audit (2026-11-05)

**Status semantics:** **complete** = this narrowly defined obligation has concrete repository evidence and, when it is runtime functionality, relevant automated tests; **partial** = documented and/or a prototype exists but the complete owner outcome does not; **missing** = no corresponding design/control; **contradictory** = current claims or descriptions disagree; **unverified** = real account, device, security or service evidence is required. Completion here never implies production readiness. **Next block** means the gap must be resolved before the recommended *local read-only task timeline* milestone; **later block** means it blocks a specific future integration or release, not all development.

| ID | Original owner requirement | Current location and concrete evidence | Status | Gap / correction | Blocks |
| --- | --- | --- | --- | --- | --- |
| ID-01 | First-party MAX identity and handbook | [MAX.md](../MAX.md), [SOUL.md](../SOUL.md), [CORE.md](../CORE.md), [README](../README.md): separate named project principles | complete | Handbook is product intent, not a live assistant persona | No |
| ID-02 | MAX identity separated from Codex instructions | [AGENTS.md](../AGENTS.md) explicitly worker-only, distinct from MAX.md; PR #2 diff | complete | No runtime enforcement of handbook yet | No |
| ID-03 | Personal, owner-controlled product, not Discord-bot replacement | [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md) §13; [AGENTS.md](../AGENTS.md) scope rules | complete | Operational service isolation has not been tested | Later |
| ID-04 | Clear communication and no fabricated successes | MAX.md, [TRUST_IMPLEMENTATION.md](TRUST_IMPLEMENTATION.md); verifier code gates task completed by observed bytes | partial | No owner-facing result renderer or independently attested external effects | Yes (honest read-only status only) |
| OR-01 | Central MAX Core orchestrator delegates bounded specialists | [AGENT_RUNTIME.md](AGENT_RUNTIME.md) design; src/max_core/api.py only owner task endpoints | partial | No planner, orchestration loop or specialist lifecycle | Later |
| OR-02 | Durable task IDs/state and owner isolation | src/max_core/store.py, models.py, migration 0001; test_durable_tasks.py and real Postgres CI tests | complete | No real-data deployment/restore proof; operational storage is plaintext | Later |
| OR-03 | Checkpoint, cancel, lease expiry and crash reconciliation | TaskStore methods; test_durable_tasks.py, test_postgres_integration.py | partial | No actual worker termination, verified external effect reconciliation or checkpoint artifacts | Later |
| OR-04 | Transactional outbox and idempotent task creation | TaskStore.create/lease_outbox/ack_outbox, 0001 migration, unit/Postgres tests | partial | Outbox has **no dispatcher**, consumer-side dedup or external idempotency guarantee | Later |
| OR-05 | Independent task verification before completion | src/max_core/verification.py, migration 0002; tests/test_verification.py and test_postgres_trust.py | partial | Only injected fixture evidence readers; no authoritative service-specific observer | Later |
| OR-06 | Durable task events visible across clients | Outbox schema exists but src/max_core/api.py exposes no event-stream/timeline route; README proposes streaming | missing | Add scoped read-only task timeline with deterministic cursor and nonsecret event metadata | **Yes** |
| OR-07 | Independent bounded supervisor/restart recovery | [OPERATIONS.md](../OPERATIONS.md), [EVOLUTION_ENGINE.md](EVOLUTION_ENGINE.md) | partial | No external supervisor, alerting, fault injection or tested runbook | Later |
| GW-01 | FreeLLMAPI is intended gateway; preserve owner choice | [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md) §15, [MODEL_GATEWAY.md](MODEL_GATEWAY.md), ADR-005 | partial | Actual owner-selected repo/deployment and permitted usage unconfirmed | Later (provider milestone) |
| GW-02 | Two format contracts, OpenAI-compatible and Anthropic Messages | src/max_core/gateway/{adapters,normalize,streaming}.py; test_gateway.py and test_gateway_streams.py | complete | **Mock text-subset contract only**; no live third-party contract verification | Later |
| GW-03 | Actual FreeLLMAPI dialects/auth and streaming | Public upstream API docs describe both protocols; [GATEWAY_IMPLEMENTATION.md](GATEWAY_IMPLEMENTATION.md) states no live smoke tests | unverified | Verify owner's exact upstream, pinned version, auth, model ID, routing, tools/stream/usage with permission | Later |
| GW-04 | Structured outputs, vision/audio and tool round-trip capabilities | [MODEL_GATEWAY.md](MODEL_GATEWAY.md) capability matrix; gateway code allows text and inert tool proposals only | partial | JSON schema, vision/audio, tool results and unsupported-mode probing absent | Later |
| GW-05 | Privacy gates and local-only routing/fallback | src/max_core/gateway/router.py; test_gateway.py negative cloud tests | partial | Caller-supplied classification needs trusted ingress; DNS/egress hardening unimplemented | Later, before live cloud |
| GW-06 | Budgets, quotas, model discovery, real account allocations | src/max_core/gateway/contracts.py/router.py price/ceiling checks; product spec marks ~1.2B tokens owner-reported | partial | Exact token counting, ledger, authoritative quotas, health/capability probes absent; cannot equate account pools | Later |
| GW-07 | Optional Ollama on reported PC | [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md), [DECISIONS.md](DECISIONS.md) T-005 | unverified | Owner device runtime/model/VRAM not checked and no Ollama adapter registered | Later |
| SK-01 | Discover relevant skills when needed | [SKILL_SYSTEM.md](SKILL_SYSTEM.md), [SKILLS.md](../SKILLS.md) | partial | Design only; no discovery integration or relevance policy in runtime | Later |
| SK-02 | Inspect publisher/source/license/hash/dependencies | [SKILL_SYSTEM.md](SKILL_SYSTEM.md) checklist | partial | No manifest parser, checksum/license/dependency validation implementation | Later |
| SK-03 | Sandbox-test skills and enforce capabilities | [SKILL_SYSTEM.md](SKILL_SYSTEM.md), [THREAT_MODEL.md](THREAT_MODEL.md); Trust allows only task.status | partial | No isolated skill runner; untrusted skills cannot be installed yet | Later |
| SK-04 | Version pin, monitor, revoke, uninstall, rollback | [SKILL_SYSTEM.md](SKILL_SYSTEM.md) design | partial | No versioned registry or rollback implementation | Later |
| SK-05 | Skills cannot self-grant permissions | src/max_core/trust.py hardcoded one-action allowlist and negative tests; [SECURITY.md](../SECURITY.md) | partial | Current deny-by-default prototype covers no real skill entry points; future adapters must enforce it | Later |
| EV-01 | Learn from owner corrections/preferences and outcomes | [EVOLUTION_ENGINE.md](EVOLUTION_ENGINE.md), [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md) | partial | No persisted learning, provenance review or user controls | Later |
| EV-02 | Convert repeated tasks into evaluated reusable workflows | [EVOLUTION_ENGINE.md](EVOLUTION_ENGINE.md), [CORE.md](../CORE.md) | partial | No workflow generator, sandbox evaluation or rollback | Later |
| EV-03 | Benchmark models/agents and experiment on gaming PC | [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md), [DEVICE_EXECUTION.md](DEVICE_EXECUTION.md) | unverified | No real hardware/model benchmark or authorized node | Later |
| EV-04 | Never silently change authentication, policy, payments or production code | [EVOLUTION_ENGINE.md](EVOLUTION_ENGINE.md), [THREAT_MODEL.md](THREAT_MODEL.md), [AGENTS.md](../AGENTS.md) | partial | Design guardrail; no Evolution runtime or isolation tests yet | Later |
| UI-01 | Windows 11 holographic cyan orb | [DESKTOP_ARCHITECTURE.md](DESKTOP_ARCHITECTURE.md), IDENTITY.md; no client code in tree | partial | No desktop app, UI tests or Windows physical verification | Later |
| UI-02 | Arch/Hyprland overlays, Wayland focus safety | [DESKTOP_ARCHITECTURE.md](DESKTOP_ARCHITECTURE.md) platform test grid | unverified | No actual compositor/portal or click-through tests | Later |
| UI-03 | iPhone authenticated browser client | [DESKTOP_ARCHITECTURE.md](DESKTOP_ARCHITECTURE.md), roadmap | partial | No web/mobile UI or device tests; do not assume background voice | Later |
| UI-04 | Gaming/focus mode does not steal focus | [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md) §3, desktop test grid | unverified | No fullscreen-game measurements or deployed client | Later |
| UI-05 | Reported RTX 4060 8 GB, 32 GB RAM and installed Ollama | [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md), [DECISIONS.md](DECISIONS.md) P-008 | unverified | Owner-reported only; do not provision or promise model performance | Later |
| ST-01 | PostgreSQL authoritative structured state | src/max_core/store.py, models.py, migrations 0001/0002; ephemeral Postgres CI | partial | Tasks/approvals/outbox implemented; memory/vault/production database absent | Later |
| ST-02 | Google Drive preferred for files/knowledge | [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md) authority matrix and OAuth scope strategy | partial | No Drive adapter, connected account or tested ACLs | Later |
| ST-03 | GitHub source/nonsecret docs; temporary local caches | [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md), .gitignore, [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md) | partial | Git source confirmed; scratch lifecycle isolation not implemented | Later |
| ST-04 | Encryption, vault key management and data separation | [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md), [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md) | partial | DB objectives plaintext; vault/secret-store roles and encryption unimplemented | Later |
| ST-05 | Retention, deletion/export, indexed-memory correction | [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md), [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md) | partial | No retention worker, erasure evidence or user controls | Later |
| ST-06 | Encrypted offsite backup and tested restore | [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md) candidate RPO/RTO and backup design | unverified | No backup implementation, restoration evidence or owner-approved recovery targets | Later; blocker to production |
| COM-01 | Two-way telephony with authenticated inbound callers | [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md) signed webhook/paired session plan | unverified | No provider, number, legal clearance or live test | Later |
| COM-02 | Official WhatsApp Business API | [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md) official channel policy | partial | No Meta account/adapter, template/consent verification | Later |
| COM-03 | Scoped authorized email and recipient confirmation | [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md); Trust denies send tools | partial | No provider, OAuth or sender/recipient verification | Later |
| COM-04 | Quiet hours, authenticated inbound commands, recording/transcript controls | [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md), [VOICE_ARCHITECTURE.md](VOICE_ARCHITECTURE.md) | partial | Design only: no recording or quiet-hour enforcement | Later |
| COM-05 | Cross-session continuity voice/phone/desktop/mobile | [AGENT_RUNTIME.md](AGENT_RUNTIME.md), [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md) | partial | No unified sessions across channels or authenticated client | Later |
| COM-06 | India-specific route, provider costs, telecom compliance | [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md) cites Twilio India limitations | unverified | No viable alternative certified/validated, tariffs or consent mechanism | Later |
| FIN-01 | Per-payment trusted confirmation with recipient/amount/currency/source | [SECURITY.md](../SECURITY.md), [THREAT_MODEL.md](THREAT_MODEL.md); current Trust explicitly denies payments | partial | Requirement documented and payments unavailable; no payment UX/step-up proof | Later |
| FIN-02 | Never store UPI PIN, OTP or CVV; never expose auth factors to LLM | [SECURITY.md](../SECURITY.md), [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md) | partial | Policy present, no actual sensitive-data intake/DLP integration tested | Later |
| FIN-03 | Authoritative payment status, no blind retries | [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md), [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md) | partial | No payment adapter, authoritative reconciliation or replay integration test | Later |
| FIN-04 | Encrypted identity document vault and verified recipient sharing | [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md), [SECURITY.md](../SECURITY.md) | partial | No encrypted vault, step-up confirmation or controlled delivery | Later |
| FIN-05 | Security/evolution cannot self-amend privileges | [EVOLUTION_ENGINE.md](EVOLUTION_ENGINE.md); hardcoded Trust allowlist | partial | No real skill/evolution or source-change runtime to test | Later |
| GOV-01 | Separate MAX project; unrelated bots untouched | [AGENTS.md](../AGENTS.md); repo tree shows only MAX files | complete | Runtime deployment/service topology not verified | No |
| GOV-02 | Owner manually reviews/merges all milestone PRs | [AGENTS.md](../AGENTS.md), [DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md); PRs #1–#7 merged by owner | complete | Keep this governance requirement | No |
| GOV-03 | No infrastructure, accounts or deployments during audit | Scope of this documentation branch; GitHub diff must prove no runtime changes | complete | Audit PR must remain docs-only and unmerged | No |
| GOV-04 | Test evidence and release gates | .github/workflows/core-ci.yml; prior PR #7 CI: 125 tests on each Python version, mock and ephemeral DB | partial | No live-provider/device/backup proof, SAST, license/secret scanning or production release review | Later |

**Interpretation:** The table makes intentional **design-only** work visible without treating it as shipped. Most future capabilities are partial by design, not a reason to restart architecture. [PHASE0_AUDIT.md](PHASE0_AUDIT.md) ranks discrepancies, and [PHASE0_EXIT_CRITERIA.md](PHASE0_EXIT_CRITERIA.md) separates architecture sufficiency from deployment readiness.
