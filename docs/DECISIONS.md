# MAX Architecture Decision Register

This log distinguishes owner-confirmed product requirements from technical proposals.

## Confirmed product decisions

| ID | Decision | Status |
|---|---|---|
| P-001 | MAX is the owner's personal voice-first assistant | Confirmed |
| P-002 | Windows 11 and Arch Linux + Hyprland desktop clients; iPhone web client | Confirmed |
| P-003 | Floating cyan/blue holographic orb based on supplied image | Confirmed |
| P-004 | Gaming/focus mode hides foreground UI and runs permitted work quietly | Confirmed |
| P-005 | Owner is a beginner; technical lead handles engineering decisions and Codex assignments | Confirmed |
| P-006 | Codex is the primary implementation worker | Confirmed |
| P-007 | Multi-provider cloud AI and local Ollama capability | Confirmed direction |
| P-008 | RTX 4060 8 GB VRAM, 32 GB RAM gaming PC as local execution/inference node | Owner-reported; verify before provisioning |
| P-009 | Google Drive is preferred document/knowledge storage | Confirmed preference |
| P-010 | PostgreSQL as proposed operational database | Previously agreed direction; validate |
| P-011 | Dynamic agents, MCP/native tools, and skill acquisition | Confirmed requirement |
| P-012 | Dedicated agent browser separate from personal Chrome | Confirmed requirement |
| P-013 | WhatsApp, email, inbound/outbound telephony | Confirmed requirement |
| P-014 | Encrypted personal vault for identity documents and financial references | Confirmed requirement |
| P-015 | Every payment requires explicit transaction confirmation | Confirmed safety requirement |
| P-016 | Never store UPI PIN, OTP, or card CVV | Security requirement |
| P-017 | Self-learning through memory, skills, evaluation, and controlled evolution | Confirmed requirement |
| P-018 | Self-healing with finite retries and independent supervisor | Confirmed requirement |
| P-019 | Separate Railway project; do not touch existing Discord bots | Confirmed boundary |
| P-020 | Gaming PC may be woken for authorized coding tasks and shut down safely afterward | Confirmed goal; implementation policy TBD |

## Technical proposals (not frozen)

| ID | Proposal | Next action |
|---|---|---|
| T-001 | Python/FastAPI for core API and AI orchestration | Prototype and compare |
| T-002 | Go for node agent/supervisor where useful | Benchmark only if needed |
| T-003 | TypeScript UI with Tauri desktop | Validate Windows and Hyprland |
| T-004 | PostgreSQL for operational state | Design schema and backup plan |
| T-005 | Ollama as initial local model runtime | Benchmark models within 8 GB VRAM |
| T-006 | OpenClaw as a candidate component/foundation | Review code, architecture, licence, security, extensibility |
| T-007 | SearXNG for self-hosted metasearch | Validate engines, hosting, rate limits, privacy, terms |
| T-008 | Playwright/CDP-based isolated browser | Prototype isolation and prompt-injection controls |
| T-009 | Google Drive API adapter | Verify OAuth scopes, quotas, and backup semantics |
| T-010 | Railway hosting | Validate topology, cost, and isolation |

## Required decisions before production

- Local inference: which tasks must work offline versus optional fallback?
- OpenClaw: adopt, selectively reuse, fork, or custom runtime?
- Exact model/provider routing and usage budgets.
- Desktop framework and Hyprland integration approach.
- Voice/STT/TTS provider and licensed voice.
- India-compatible telephony and WhatsApp provider, pricing, and compliance.
- Home automation protocol/hub.
- Vault key management and recovery strategy.
- Google Drive backup/encryption policy.
- Gaming node wake-up and secure job transport.
- Approval policy for merges, deployments, purchases, document sharing, and shutdown.
