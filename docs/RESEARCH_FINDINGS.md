# MAX Phase 0B — architecture research findings

**Evidence date:** 2026-11-05. **Status:** Architecture recommendation, not an implementation or verified live provider contract. Sources below are primary documentation and project repository sources. Do not confuse documented upstream capability with a capability proven in MAX.

## Product requirement and ownership

MAX is a **single-owner personal AI assistant**, not a new company, not a wrapper sold as another product. [Product specification](PRODUCT_SPECIFICATION.md), [MAX identity](../MAX.md), and the owner-provided Phase 0 assignment define requirements. Preserve the owner's chosen cyan orb, original British-style voice, Windows 11/Arch Hyprland/iPhone browser, model access, dynamic skills, protected memory, secure execution, personal communication and controlled evolution. Never replace MAX identity files with developer prompts.

**Discrepancy:** the Phase 0 assignment names **FreeLLMAPI as the intended model gateway** and two API formats. Existing PRODUCT_SPECIFICATION.md focuses on direct multi-provider access and does not identify a concrete FreeLLMAPI deployment. Public documentation for [lov0u/freellmapi](https://github.com/lov0u/freellmapi/blob/main/docs/api.md) documents an OpenAI-compatible proxy and Anthropic/Claude client support. **It is not proven that this repository is the owner's intended service**. Decision: design MAX Gateway to speak both OpenAI Chat Completions-style and Anthropic Messages-style adapters and optionally target that self-hosted proxy, but do not install or couple MAX to it until owner confirms the exact endpoint, license, security posture, versions and credentials. Do not assert availability, quotas, or feature parity.

## Recommended stack — contingent on a working prototype

| Decision | Proposed default | Alternatives considered | Reason / cost / security |
| --- | --- | --- | --- |
| Core API/orchestrator | Python 3 with FastAPI and explicit task state machine | TypeScript/Node; Go; adopted agent runtime | Good AI/library ecosystem and async HTTP; guard against blocking calls and unbounded workers; container, engineering and hosting cost |
| Background tasks | PostgreSQL-backed task records + bounded worker, then dedicated queue only if measured need | In-process jobs; Celery/Redis; Temporal | Persistent authority and minimal initial operations; outbox and crash recovery matter more than queue brand; do not run irrecoverable work only in API memory |
| Operational DB | PostgreSQL with migrations and transactionally stored tasks, approvals, outbox, audit indices | SQLite for single-machine prototype; MySQL | Structured concurrency, row locks, JSON metadata and recoverability; requires managed DB/backups, test actual cost |
| API/event channel | HTTP JSON with authenticated SSE for status; WebSocket only for interactive audio/control where needed | WebSocket everywhere; polling | SSE simplest for server-to-client progress; reconnect cursors and authorisation needed; neither transport proves reliability |
| Desktop | Tauri 2 + TypeScript web UI **prototype first** | Electron; native Qt; GTK layer-shell | Small web UI footprint and shared orb components; native window permissions and Wayland overlay behavior unproven |
| iPhone | Private responsive web UI/PWA | Native iOS client | Lower initial maintenance; do not promise background microphone/wake-word on mobile |
| Browser | Playwright with dedicated context/profile and dedicated worker | Embedded OpenClaw browser; Selenium | Browser contexts isolate cookies/storage, not a hostile page sandbox. Add process/network isolation, download quarantine, SSRF and prompt-injection controls |
| Search | Direct sanctioned site access; optional private SearXNG | Paid search services | SearXNG needs operations and upstream engines can block/captcha; search is not guaranteed free or unlimited |
| Model interface | MAX-owned normalized gateway, explicit OpenAI/Anthropic transports, optional proxy | Bind directly to FreeLLMAPI/one vendor | Supports privacy policy, budgets, model capabilities and fallback; verify each model's actual streaming/tool/JSON support |
| Local inference | Ollama integration behind gateway, only after approved host tests | llama.cpp/vLLM | Lower per-token metered fees, local privacy; host CPU/GPU/power and model license/VRAM are real constraints |
| Google Drive | Optional scoped file/export/backup destination | S3-compatible object store; local encrypted backups | Familiar owner-controlled files; OAuth verification, quota, revision expiry and outage make it unfit as only DB/backup |
| Communications | Official API adapters, initially disabled | Unofficial WhatsApp bridges and unverified call bots | High privacy, regional, consent and compliance costs; no unofficial account automations |
| Supervision | OS/service-level supervisor separate from agent | Self-monitoring only | A failed main process cannot reliably restart itself; bounded restart budgets and alerting required |

**Licenses and cost:** evaluate license at a pinned release, including data/model weights, voice models, provider terms and commercial restrictions. No numerical hosting, telecom or inference price is assumed without configured account/region usage and recent quotes.

## OpenClaw assessment

[Official README](https://github.com/openclaw/openclaw), [official architecture](https://docs.openclaw.ai/), [security](https://docs.openclaw.ai/gateway/security), [sandboxing](https://docs.openclaw.ai/gateway/sandboxing). OpenClaw provides gateway/channel routing, agent sessions, tools, plugins, skill distribution and browser/device patterns. The published repository says MIT; pin and confirm any redistributable third-party notices. **Material security fact:** OpenClaw sandboxing is off by default, and the Gateway/plugins can remain outside the sandbox. This is a configuration and trust-boundary concern, not an allegation of a specific vulnerability. MAX needs a small first-party orchestrator with owner-scoped approvals and durable task/finance/vault policies rather than inheriting another runtime's host authority. **Recommendation: learn from selected designs, avoid adopting or forking core now.** Reconsider a guarded adapter if a specific feature saves measured work and passes isolation/security tests. Full rubric in [OPENCLAW_EVALUATION.md](OPENCLAW_EVALUATION.md).

## MCP, browser and research

[MCP authorization spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) describes transport authorization, which can be optional; **MCP is not MAX's permission engine**. MAX validates scope, origin, tool schema, resource identity, policy, approval, timeout and evidence for both MCP and native tools. Tool output is untrusted.

[Playwright browser contexts](https://playwright.dev/docs/browser-contexts) isolate cookies and storage; use a separate OS user/container for hostile browsing where feasible. Downloads are quarantined; block cloud metadata, loopback, internal/LAN and link-local destinations unless explicitly allowlisted; cap redirects. The personal Chrome profile is never reused. [SearXNG install docs](https://docs.searxng.org/admin/installation.html) imply operating an actual service, not limitless free API.

## Desktop and mobile

[Tauri 2 window configuration](https://v2.tauri.app/reference/config/) documents transparent and always-on-top options, but compositor support is platform-specific. **Gate Hyprland claims on a real Arch Wayland test**: pinned/float rules, click-through, focus, game full screen, multi-monitor, fractional scaling, persistence, and native audio. Windows tests must cover borderless exclusive games, focus preservation, and GPU use. iPhone browser supports a foreground companion by default; background wake-word/recording is **not** part of initial accepted scope.

## Voice

[openWakeWord](https://github.com/dscripka/openWakeWord) is a candidate for local wake detection, but its repository says bundled pretrained model weights are CC BY-NC-SA, distinct from Apache-2.0 code; a custom/appropriately licensed MAX wake-word model may be required. [faster-whisper](https://github.com/SYSTRAN/faster-whisper) is a candidate local speech-to-text implementation (MIT code); benchmark actual CPU/GPU latency. [Piper](https://github.com/rhasspy/piper) code is MIT in its referenced repository; individual voice weight licenses and project maintenance must be audited. An original licensed British-English voice is a creative brief, not an authorization to clone an actor. Barge-in, echo cancellation, mute/privacy indicator, audio data retention and device selection need test evidence.

## Data/storage and persistence

[PostgreSQL PITR docs](https://www.postgresql.org/docs/current/continuous-archiving.html) explain base backup + WAL archives; logical pg_dump does **not** itself enable PITR. [Drive scope guidance](https://developers.google.com/workspace/drive/api/guides/api-specific-auth) recommends narrow `drive.file` where feasible. [Drive revision behavior](https://developers.google.com/workspace/drive/api/guides/manage-revisions) explicitly permits automatic pruning; Drive revision history is **not** a durable backup policy. Data classification, retention, encryption, manifest and restore checks in [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md).

## India telephony (important restriction)

[Twilio's India Voice Guidelines](https://www.twilio.com/en-us/guidelines/in/voice) list domestic calls as unavailable for that product; international outbound calls to India require non-Indian caller IDs, with anti-abuse restrictions. [Twilio +91 Caller ID help](https://help.twilio.com/articles/57566259695003) says outbound calling from Indian +91 caller ID is blocked. **Do not conclude Twilio supplies a workable Indian domestic two-way personal calling solution.** Telecom supplier selection is deferred pending regional, legal, consent, price, incoming DID, webhook, recording and caller-auth checks. Design supports future validated providers; prefer authenticated in-app voice first. See [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md).

## Verification boundaries and outstanding decisions

**Verified by source:** upstream documented APIs/features/license notices and published restrictions in linked official docs; read-only checks of named GitHub project files. **Not verified:** MAX deployment, actual device hardware/specs, any owner provider account or token quota, FreeLLMAPI identity, real integrations, host compatibility, provider billing/SLAs, India telecom feasibility, user-supplied private specs beyond checked repository docs.

**Owner choices only when needed:** exact FreeLLMAPI source/URL; acceptable local-only data categories; preferred calling number/provider if calling phase begins; consent for external account connections; backup recovery/retention requirements; whether to commit to a specific voice license. No credentials requested in Phase 0.

## Source ledger (primary URLs, checked 2026-11-05)

- [OpenClaw upstream](https://github.com/openclaw/openclaw), [security](https://docs.openclaw.ai/gateway/security), [sandbox coverage](https://docs.openclaw.ai/gateway/sandboxing/supported-capability-matrix).
- [FreeLLMAPI public project](https://github.com/lov0u/freellmapi/blob/main/docs/api.md) — **candidate identity only, not confirmed owner project**.
- [Tauri config](https://v2.tauri.app/reference/config/); [Playwright isolation](https://playwright.dev/docs/browser-contexts); [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).
- [PostgreSQL PITR](https://www.postgresql.org/docs/current/continuous-archiving.html); [Drive limits](https://developers.google.com/workspace/drive/api/guides/limits), [scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [revisions](https://developers.google.com/workspace/drive/api/guides/manage-revisions).
- [SearXNG operations](https://docs.searxng.org/admin/installation.html); [openWakeWord](https://github.com/dscripka/openWakeWord); [faster-whisper](https://github.com/SYSTRAN/faster-whisper); [Piper](https://github.com/rhasspy/piper).
- [Twilio India voice](https://www.twilio.com/en-us/guidelines/in/voice); [Twilio +91 outbound](https://help.twilio.com/articles/57566259695003).
