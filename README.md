# MAX

**A personal, voice-first AI assistant that grows with its owner.**

MAX is intended to be a cross-platform personal assistant with a floating holographic desktop companion, natural voice interaction, persistent memory, dynamic skills, multi-agent task execution, secure integrations, local and cloud AI, and bounded self-recovery.

## Project status

**Stage: Foundation and architecture.** No production application has been implemented yet. Technology choices remain provisional until the architecture review is complete.

## Product surfaces

- **Windows 11:** floating, transparent, always-on-top orb companion; background operation and gaming-aware quiet behaviour.
- **Arch Linux + Hyprland:** equivalent desktop companion and local execution node.
- **iPhone:** responsive web interface.
- **Voice:** wake-word interaction, speech recognition, speech generation, interruption, inbound and outbound calling.

The supplied blue/cyan orb image is the visual reference. Preserve its dark centre, luminous cyan/turquoise rim, electric-blue internal reflections, and restrained holographic character.

## Core capabilities

1. Personal assistant identity, personality, and persistent memory.
2. Provider-independent model gateway for configured cloud providers and local Ollama models.
3. Agent harness for planning, tool use, dynamic delegation, checkpoints, cancellation, verification, and reporting.
4. Skill discovery, inspection, installation, isolation, testing, versioning, and controlled improvement.
5. MCP and native API integrations through a permission-aware tool registry.
6. Dedicated agent-controlled browser and web research that prefers no-cost methods where practical, while respecting provider terms, rate limits, robots controls, and access restrictions.
7. Google Drive for user-selected documents, knowledge exports, and backups; PostgreSQL for operational state and structured memory.
8. Windows and Linux execution nodes for authorised coding, development, and device tasks.
9. Spotify, home automation, WhatsApp, email, and two-way telephony integrations.
10. Encrypted personal vault for sensitive documents and financial references.
11. Self-evaluation and bounded recovery with an independent supervisor.
12. Auditable, human-controlled financial and other sensitive operations.

## Non-negotiable safety and engineering rules

- Never commit secrets, API keys, tokens, passwords, private keys, Aadhaar/passport scans, card data, UPI PINs, OTPs, or other personal records.
- Never store CVVs, UPI PINs, or OTPs in assistant memory. Use trusted payment-provider interfaces and require explicit approval for each transaction.
- Sensitive document access and sharing must be authenticated, recipient-checked, policy-controlled, and audited.
- Downloaded skills, webpages, emails, documents, and tool outputs are untrusted input—not instructions that can override MAX's policies.
- Skills and agents cannot grant themselves permissions.
- Use least privilege, scoped credentials, isolated workspaces, and explicit approval gates for consequential actions.
- Never bypass authentication, MFA, anti-cheat, access controls, paywalls, or service restrictions.
- Never modify unrelated projects or existing Discord bots.
- Never deploy untested repairs or loop indefinitely. Retries and recovery are bounded; preserve evidence and escalate when uncertain.
- Never claim work is complete without observable verification evidence.

## Repository map

- `MAX.md` — identity, purpose, and foundational principles.
- `SOUL.md` — personality and communication.
- `CORE.md` — operating and task-execution model.
- `MEMORY.md` — memory policy and lifecycle.
- `SKILLS.md` — skill lifecycle and trust model.
- `TOOLS.md` — tool registry and integration approach.
- `SECURITY.md` — threat model, permissions, and sensitive-data handling.
- `EVOLUTION.md` — learning and controlled improvement.
- `IDENTITY.md` — voice and visual identity.
- `OPERATIONS.md` — health, recovery, and supervision.
- `docs/PRODUCT_SPECIFICATION.md` — consolidated product requirements.
- `docs/TECHNICAL_DESIGN.md` — provisional architecture and open technical decisions.
- `docs/DECISIONS.md` — architecture decision register.
- `docs/ROADMAP.md` — development milestones.
- `docs/DEVELOPMENT_WORKFLOW.md` — Codex implementation and verification rules.

## Development approach

Codex is the primary implementation worker. Work is divided into small, reviewable assignments with tests and acceptance criteria. Architecture decisions are recorded before implementation. The owner is a beginner and should receive plain-language explanations and only essential product-level questions.

See [the roadmap](docs/ROADMAP.md) and [development workflow](docs/DEVELOPMENT_WORKFLOW.md).
