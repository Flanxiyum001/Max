# MAX dependency and framework evaluation — Phase 0B

**Evidence date:** 2026-11-05. No software installed. These are research candidates, not products that MAX claims to contain or deploy.

| Candidate | Upstream primary evidence | Good for | Why not unconditional adoption | Cost/license/status |
| --- | --- | --- | --- | --- |
| OpenClaw | [Upstream](https://github.com/openclaw/openclaw), [security](https://docs.openclaw.ai/gateway/security) | Channels, sessions, browser/tools patterns | Default sandbox off, host-level gateway/plugins; broad coupling for strict MAX permissions | Repo advertises MIT; security configuration/maintenance burden |
| Hermes Agent | [Nous Research project](https://github.com/NousResearch/hermes-agent), [tools docs](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/tools.md) | Memory learning/skill acquisition workflows, tool catalogs and delegation inspiration | Learning logic is not a trusted authorization layer; adopting whole agent changes MAX's runtime/identity; review executable tool surfaces | Project license and subdependencies must be reverified at pinned version; runtime costs and account options vary |
| OpenHands Software Agent SDK | [Official SDK](https://github.com/OpenHands/software-agent-sdk), [architecture docs](https://github.com/OpenHands/docs/blob/main/sdk/arch/overview.mdx) | Isolated software-development agents with Python/TypeScript API and workspaces | Could become a **coding-worker adapter**, not the master MAX policy plane; local-workspace tool risk | MIT in upstream repository listing; Docker/remote infrastructure cost |
| MCP | [Official authorization spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) | Tool server interoperability and schema exchange | Authorization for HTTP transport may be optional; not MAX owner permission or task approval | Protocol not vendor-dependent; each server has independent license/cost |
| FastAPI | [Official docs](https://fastapi.tiangolo.com/) | Python API, async streaming and type validation | Async framework does not itself guarantee durable execution; workers and DB needed | Open-source, hosted CPU/DB cost |
| PostgreSQL | [Official docs](https://www.postgresql.org/docs/current/) | Transactions, durable task/outbox state, constrained memory | More operations than local SQLite; migrations/restore required | Open-source software; hosting/backup fee |
| Tauri 2 | [Window config](https://v2.tauri.app/reference/config/) | Web UI desktop wrapper, transparency, always-on-top flags | Native OS/Wayland compositor limitations; verify on owner devices | Framework ecosystem/maintenance + target-specific testing |
| Playwright | [Context isolation](https://playwright.dev/docs/browser-contexts) | Dedicated browser profile and automation | Cookie isolation != hardened sandbox; untrusted page/network controls remain | Browser download/runtime CPU, abuse prevention |
| SearXNG | [Operations docs](https://docs.searxng.org/admin/installation.html) | Private self-hosted metasearch | Upstream rate limits, captchas, search quality, self-host overhead | Server/network cost; no unlimited-free guarantee |
| openWakeWord | [Project README](https://github.com/dscripka/openWakeWord) | Local wake detection | Bundled pretrained model weights have different noncommercial license than code | Apache-2.0 code; CC BY-NC-SA bundled weights; check license before use |
| faster-whisper | [Project](https://github.com/SYSTRAN/faster-whisper) | Local STT | Actual latency/GPU fit unknown; benchmark device | MIT implementation; model licenses vary |
| Piper | [Project code/license](https://github.com/rhasspy/piper/blob/master/LICENSE.md) | Offline TTS | Original voice quality/license and maintained implementation must be verified | MIT referenced code; weights individually licensed |
| Google Drive API | [Scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [revision retention](https://developers.google.com/workspace/drive/api/guides/manage-revisions) | File/export destination | Not durable DB or sole backup; restricted scopes, quota, OAuth and purged revisions | Account storage + operational/API quotas |

## Selection rubric for each dependency

Score before approval: **specific task coverage** (must show reproducible outcome), **permission isolation** (must pass negative tests), **data custody**, **maintenance burden**, **license and provenance**, **cost at expected load**, **deletion/rollback**, and **Windows/Hyprland compatibility**. A small dependency adapter with untrusted output is preferable to importing a second central authority. MAX components own all user-facing branding, permissions, task state and safety outcomes.

## Recommended reuse points

- OpenHands as a constrained **coding task executor** in later PRs after sandbox and audit proofs.
- Hermes/OpenClaw concepts as research references; adapters only if a specific versioned component passes explicit review.
- MCP as typed transport, MAX Trust as mandatory enforcement regardless of whether tool came from MCP or native API.
- Playwright browser with separate account/profile/workspace and egress policy; never personal Chrome.
- Voice packages only after code/weight licenses, device latency and quality tests.

No blanket source copying, unverified license assumption, installer execution, or externally branded UI is authorized by this evaluation.
