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

## Phase 0B architecture decision records — proposals awaiting implementation gates

| ID | Decision / rationale | Status | Validation or owner gate |
| --- | --- | --- | --- |
| ADR-001 | MAX owns core task policy and memory, rather than wholesale OpenClaw adoption; smaller audited trust boundary | Recommended | Re-evaluate if isolated adapter outperforms custom implementation |
| ADR-002 | Python/FastAPI modular core; no service sprawl initially | Proposed | Typed API + latency/worker/restart benchmarks |
| ADR-003 | PostgreSQL authoritative tasks, approvals and outbox; encrypted offsite backup separate | Proposed | Migration, crash recovery, backup/restore proof |
| ADR-004 | MAX-owned gateway supports OpenAI-compatible and Anthropic Messages-style formats | Proposed | Both protocol fixture sets, real provider contract checks |
| ADR-005 | Owner selected `tashfeenahmed/freellmapi` for MAX with **both local and cloud inference**; separate it from `lov0u/freellmapi` | **Project selected; deployment unverified** | Confirm owner instance/version, explicit inference egress per model, route restrictions, formats and cost; no secrets in docs |
| ADR-006 | Tauri 2 UI candidate; Hyprland overlay and Windows gaming must pass physical tests | Conditional | Demonstrate no focus theft, pinning, graphics, audio and accessibility |
| ADR-007 | Playwright dedicated context with network/process boundary; optional SearXNG | Proposed | SSRF, storage, prompt injection, captcha and operations costs |
| ADR-008 | Local wake-word plus separate licensed STT/TTS adapters, iPhone foreground first | Proposed | Benchmark; audit model and voice-weight license |
| ADR-009 | No selected India PSTN provider; authenticated in-app voice precedes PSTN | **Blocked by regional provider proof** | Actual inbound/outbound routing, consent, webhook authentication |
| ADR-010 | Google Drive scoped files/exports, not sole backup; memory and vault separated | Proposed | OAuth scopes, restore/deletion proof, key recovery owner decision |
| ADR-011 | Human-reviewed milestone PRs against verified default `max` | **Owner-confirmed process** | Separate branch/PR, owner manually merges; never auto-merge |
| ADR-012 | Evolution can update low-risk learning under policy; executable/security changes never self-promote | Required guardrail | Regression suite, privilege-denial and rollback test |

These recommendations do **not** silently replace product requirements or claim deployment. See [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md), [OPENCLAW_EVALUATION.md](OPENCLAW_EVALUATION.md), [THREAT_MODEL.md](THREAT_MODEL.md) and [REPO_INVENTORY.md](REPO_INVENTORY.md).

## Outstanding explicit owner decisions

1. FreeLLMAPI project is now owner-selected as **`tashfeenahmed/freellmapi`** and both local/cloud inference are required. Still decide exact owner deployment/version, verified upstream destinations, provider model/credential policy, and when each mocked adapter may be smoke-tested. Keep both protocol adapters.
2. Which categories are **local-only** vs cloud-permitted? Proposed default: restricted identity/financial/vault items local-only and never sent to general models.
3. What retention and recovery expectations are acceptable (candidate RPO 24h / RTO 4h pending owner approval)?
4. Which original voice/weight licenses, microphone consent and wake behavior are acceptable?
5. If PSTN calling becomes viable, what Indian number/provider, consent and pricing is owner-approved?
6. Which remote-device actions (code edits, merges, deploys, shutdown) require explicit per-action approval? Default high-impact actions do.
7. Complete repository tree inventory and real Windows/Hyprland hardware verification before freezing performance/platform decisions.

Decision owner: the user. Engineering worker recommends, explains and supplies evidence; does not grant permissions or spend/deploy.

## Milestone 3 reviewed implementation constraints

- **ADR-004 implemented at contract level only:** OpenAI **Chat Completions** and Anthropic **Messages** transports are available as Python adapters and mock-tested. This does **not** mean live account/API compatibility is verified, and does not include OpenAI Responses API. The intended **project** is now selected as `tashfeenahmed/freellmapi`, but its actual running instance and compatibility remain unresolved (ADR-005).
- **Privacy default:** external cloud calls require explicit opt-in and a per-request USD ceiling with configured token prices; confidential/restricted data stays local-only. No automatic provider discovery or blanket fallback.
- **Pricing limitation:** input counts are conservative text-envelope estimates, not an exact tokenizer, and an admission ceiling is not an absolute charge guarantee. Future global quota/account reconciliation and authoritative usage checks remain open.
- **Trust boundary:** all model-originated tool calls remain inert proposals; no execution until MAX Trust grants a typed, audited operation. Streaming does not retry/fallback after any response begins.
- **Human-controlled rollout:** review GitHub CI, code and policy before merging; no provider secrets, backend deploy or account actions are authorized here.

## Milestone 4 security posture — implementation status

- **Trust registered capability:** `task.status` only; it returns task ID, state and revision. All side-effectful tools and security/financial changes are **unregistered and denied**, even with a locally approved grant.
- **Approval:** local bearer-authenticated owner route can approve a **single read-only** scoped action. The grant is bound to task/actor/session/device/proposal ID/parameters, has a short expiry and is transactionally single-use. The bearer token is **not human-presence/MFA proof** and must never authorize real purchases, deployments, messages, shell or vault sharing.
- **Independent verification:** trusted in-process reader interface provides separate observed bytes; only matching SHA-256 can advance a task from `awaiting_verification` to `completed`. There is no real external effect observer or attestation yet; this is a mocked internal verification contract.
- **Storage:** additive migration `0002_trust_verification` stores approval ledger, basic audit and evidence check records; readiness now checks revision 0002. DB audit is not tamper-evident and requires future role separation.
- **Review control:** no migration on a real database, no merge, no deploy or external tool capability without separate owner approval. See [TRUST_IMPLEMENTATION.md](TRUST_IMPLEMENTATION.md).


## Post-PR #7 requirements audit — disposition (2026-11-05)

**Audit baseline:** `max` at `72f67b1ec6a737c813f4ca633457a9ca39223b94`; [findings](PHASE0_AUDIT.md), [traceability](REQUIREMENT_TRACEABILITY.md), [exit criteria](PHASE0_EXIT_CRITERIA.md).

- **Already implemented, not merely proposed:** ADR-002's Python/FastAPI local Core (milestone 1); part of ADR-003's PostgreSQL task/checkpoint/outbox and approval/verification migrations (milestones 2 and 4); and ADR-004's **mock-tested** dual-format text Gateway (milestone 3). This does **not** prove a worker, memory, backup, live model provider or any production service.
- **Explicitly preserve owner choice of FreeLLMAPI:** owner specified two formats. The active public [tashfeenahmed/freellmapi](https://github.com/tashfeenahmed/freellmapi) [API documentation](https://github.com/tashfeenahmed/freellmapi/blob/main/docs/en/api/01-rest-api.md) documents both Chat Completions and Anthropic Messages. The previously cited `lov0u/freellmapi` is a **different public repository**, not evidence of the owner's desired source. **ADR-005 is partially resolved:** owner selected `tashfeenahmed/freellmapi` and local/cloud inference; deployed instance and upstream routing remain unverified. No account/API quotas or actual models were verified.
- **Current Trust scope:** a single read-only `task.status` method; single-use local bearer-approved grant and fixture evidence observer. The contract is **not a reusable production approval token or verified external effect framework**. Never register high-risk actions based on present tests.
- **Unresolved owner gates:** restricted vs locally allowed data categories and retention preferences, actual FreeLLMAPI deployment and verified ultimate inference destinations, backup RPO/RTO and key recovery, gaming-PC hardware and Ollama, authentic device/remote identity, regional calling/consent and voice licensing. Do not decide these silently or request credentials during documentation review.
- **Next small milestone recommendation (not authorized for execution by audit):** local read-only task timeline + terminal client, using existing task/outbox state and the bootstrap token on loopback only. It closes an observable slice before cloud/provider or desktop/voice work. See [PHASE0_AUDIT.md](PHASE0_AUDIT.md).

## Milestone 9 Stage 2 — local HTTP is not local inference

**Owner-confirmed:** `tashfeenahmed/freellmapi` is the intended project and both local and cloud inference must be supported. **Not confirmed:** running deployment topology, version, actual upstream egress/model routes, pricing, quota or provider format compatibility. Source selection is not runtime attestation. The narrowly scoped gateway policy therefore introduces explicit `InferenceDestination` values `VERIFIED_LOCAL`, `CLOUD` and default `UNKNOWN` (denied). A loopback proxy forwarding to cloud must be tagged `CLOUD` and pass cloud/privacy/price authorization; absence of a reviewed destination denies all requests. No live credentials or provider requests are in scope. The remote-vs-loopback HTTP validation and existing protocol adapters remain unchanged.

## Milestone 10 — personal MCP and unattended device work

Owner clarified that MAX is a personal assistant, separate from Aventas, and
requested MCP/plugin connections and remotely initiated PC coding through PR/CI,
followed by safe shutdown and reporting. This authorizes implementation work;
it does not establish a deployed device identity, live endpoint or wake relay.
Implement separate reviewed milestones, preserving the default owner-merge
workflow. Start with [explicit MCP discovery](MCP_DISCOVERY.md); discovery never
grants tool execution. PC control and live side effects require authentic device
policy, isolation and independent completion evidence before activation.
