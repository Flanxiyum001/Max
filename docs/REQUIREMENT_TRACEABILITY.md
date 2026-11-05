# MAX Phase 0 deliverable and acceptance traceability

**As of:** 2026-11-05. Phase 0 is architecture-only. 'Present' means a file can be fetched, **not** its interfaces or described technologies are implemented.

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

Supplementary: [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md), [OPENCLAW_EVALUATION.md](OPENCLAW_EVALUATION.md), [ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md), [REPO_INVENTORY.md](REPO_INVENTORY.md), [VALIDATION_PLAN.md](VALIDATION_PLAN.md).

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

- [x] Documents/designs can be reviewed in the PR — presence to be confirmed by GitHub fetch.
- [ ] All internal relative links and diagrams rendered/validated; record real results in PR.
- [ ] All user-supplied owner specification documents checked against this register. If those private source documents were not available, record the gap rather than inventing requirements.
- [ ] Complete recursive tracked-file inventory, not just known-path reads; see REPO_INVENTORY limitations.
- [ ] Any provider account capability and local device test completed (not in Phase 0; carry forward as explicit gates).
- [ ] Owner manually reviews and merges; do not auto-merge or deploy.

**Do not mark an incomplete check as passed.** A document list is not a security audit or technical proof.
