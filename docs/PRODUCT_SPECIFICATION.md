# MAX Product Specification — Baseline

**Status:** Requirements consolidated from owner-provided project conversations. This is a working baseline; open technical questions are tracked separately.

## 1. Product vision

MAX is the owner's personal, voice-first AI assistant: a persistent companion that can converse, perform authorised tasks, use tools, manage projects, remember context, learn from outcomes, and recover intelligently from failures.

## 2. Owner and development process

The owner is a complete beginner in software engineering. The technical lead is responsible for architecture, stack choices, UI/UX, engineering planning, security, testing strategy, and precise Codex assignments. Codex is the primary implementation worker. Explain decisions in plain language and avoid unnecessary technical questions.

Any suitable programming language may be selected based on performance, reliability, ecosystem maturity, maintainability, cross-platform needs, and security.

## 3. Platforms and UI

- Windows 11 and Arch Linux + Hyprland desktop companions.
- iPhone browser-based interface.
- Primary desktop visual is a floating holographic orb based on the owner's uploaded cyan/blue orb reference.
- Orb can remain above ordinary windows, be moved/resized, and show activity states.
- Gaming/focus mode hides or quiets the visual companion and handles commands in the background without stealing focus.

## 4. Voice and communication

- Wake-word activation and voice-first interaction.
- Speech recognition and speech generation with low-latency conversational turn-taking.
- Interruptible speech and natural pauses.
- Original, appropriately licensed JARVIS-inspired British male voice character; exact actor voice reproduction is not assumed permitted.
- Inbound and outbound telephone calls, with authenticated callers, quiet hours, and task-context continuity.
- WhatsApp and email integration for authorised messages, document delivery, instructions, and notifications.

## 5. Intelligence and model access

Owner reports access to approximately 1.2 billion tokens across these provider accounts/endpoints: Google AI Studio, Grok, Cerebras, NVIDIA NIM, Mistral, OpenRouter, Z.ai/Zhipu, Ollama Cloud, and OVH AI Endpoints. This is owner-reported and must be verified provider by provider.

Build a provider-independent model gateway with model capability metadata, health, routing, latency/cost budgets, fallback, and local-only/degraded modes.

Local machine reported: Windows 11 gaming PC, NVIDIA RTX 4060 with 8 GB VRAM and 32 GB system RAM; Ollama is already installed. Confirm current details before provisioning. Local inference and suitable experiments are desired. Large foundation-model training is not an assumed capability.

## 6. Agent harness and skills

- Persistent task lifecycle: understand, plan, delegate, authorize, execute, observe, recover, verify, remember, report.
- Dynamic specialist agents with bounded contexts and permissions.
- MCP plus native API/internal tools.
- Central tool/capability registry.
- Skill discovery, inspection, source/licence/dependency review, isolation, testing, versioning, rollback, and permission governance.
- Skill content and external content remain untrusted.

## 7. Browser and research

MAX should use a dedicated browser profile/environment separate from the owner's personal Chrome. Support browsing, research, documentation, website interactions, screenshots, and extraction. Prefer no-cost direct browsing and self-hosted metasearch where practical; do not promise unlimited free search or bypass site restrictions.

## 8. Memory, knowledge and storage

- PostgreSQL is the proposed authoritative store for structured application state, tasks, memory metadata, and indexes.
- Google Drive is the preferred user-controlled document and knowledge storage destination, with an initially reported 15 GB allocation and possible upgrade to 100 GB.
- Local storage for caches and temporary workspaces.
- GitHub for source code and non-secret documentation.
- Backup versioning and tested restore procedures required.
- User-approved memory correction/export/deletion controls required.

## 9. Execution and gaming

The gaming PC should be an authorised execution node that MAX can wake, use for coding/development tasks, verify work, and shut down after safe completion. The intended workflow includes opening a supplied GitHub project, editing in an authorised VS Code or equivalent environment, running tests/builds, preparing commits/merges, deploying when authorized, verifying deployment, and shutting down safely.

Use isolated workspaces, scoped credentials, activity logs, and explicit approval boundaries. Do not bypass authentication or anti-cheat protections.

Gaming mode should support background Spotify controls, home automation, and other permitted commands without interrupting gameplay. MAX should not steal focus or show non-urgent overlays.

Valorant account switching is a desired convenience for accounts the owner is authorized to use; implementation must respect Riot authentication, MFA, and anti-cheat terms. Per-account settings may be stored as user profiles without storing authentication factors insecurely.

## 10. Personal integrations

Spotify playback control; home automation (TV, AC, and other owner-authorized devices); GitHub and coding workflow; Railway and Cloudflare infrastructure integrations; Google Drive; email and WhatsApp; inbound/outbound telephony.

Each integration requires verification of API availability, account permissions, pricing, and regional constraints.

## 11. Personal Vault and finance

The owner wants MAX to access personal documents (including Aadhaar, passport, and other records) and financial information for retrieval and authorized workflows.

Requirements:
- Encrypted vault and strict access controls.
- Strong authentication, audit, and recipient verification.
- Sensitive document sharing through approved channels only.
- Never store UPI PINs, OTPs, or card CVVs.
- Never expose authentication factors to an AI model.
- Every transaction requires explicit trusted confirmation with recipient, amount, currency, and source details.
- Verify transaction outcome from an authoritative source before reporting success.
- No blind retries for payments.

## 12. Learning and self-healing

Evolution Engine should learn from owner corrections and verified outcomes, evaluate proposed workflow improvements, and run regression tests. Executable production changes and security/permission changes require controlled review.

Recovery Engine should detect, diagnose, classify, repair in isolation, test, restore, and learn. Use finite retries, backoff, circuit breakers, repair budgets, failure fingerprints, cooldowns, safe mode, and human escalation. An independent supervisor must operate outside the main MAX process.

## 13. Hosting and boundaries

A separate Railway project is intended for MAX. Existing Discord bots and unrelated projects/infrastructure must remain untouched. No infrastructure changes or local installations occur without a scoped plan and appropriate owner approval.

## 14. Initial development principle

Start with secure, testable foundation: repository standards, architecture records, authentication/authorization model, configuration and secret handling, health/observability, model gateway interface, task persistence, and a minimal vertical slice. Do not build all integrations at once.
