# Implementation status and repository audit

Labels: **[tested]** implemented and covered by tests in this repo ·
**[partial]** implemented, integration only partially verified · **[mock]**
prototype/mocked only · **[blocked]** external dependency, hardware or
credential prevents verification · **[not implemented]** not started.

Nothing on this page is marked complete because an import succeeded.

---

## 1. Audit — what is actually in the repository

### 1.1 Architecture and entry points (verified)

| Layer | Module | Entry point |
|---|---|---|
| CLI | `src/max/cli.py` | `python -m max …` (`ask`, `health`, `eval`, `improve`, `skills`, `discord`, `bench`) |
| Agent loop + policy | `src/max/core/agent.py`, `core/tools.py`, `core/factory.py` | `build_agent()` — single tool registry, single executor, audit log |
| Providers | `src/max/providers/` | `mock` (default) and OpenAI-compatible adapter |
| Discord transport | `src/max/discord/bot.py` (I/O only) → `router.py` (all decisions) | `max discord run` |
| Memory | `src/max/core/usermemory.py`, `core/builtins.py` | caller-scoped `!max memory …` |
| Skills + self-improvement | `src/max/skills/registry.py`, `src/max/improve/pipeline.py` | staged → evaluated → promoted, integrity-pinned policy |
| MCP | `src/max/mcp/client.py`, `mcp/backend.py` | configured servers, no tool auto-enabled |
| Coding worker | `src/max/worker/{tasks,policy,tools}.py` | isolated git worktree, allowlisted argv |
| Multi-agent orchestration | `src/max/orchestration/` | supervisor → bounded scheduler → worker grants |
| Repository intelligence | `src/max/codesearch/` | tree-sitter when installed, regex fallback |
| Speech | `src/max/speech/` | mock + faster-whisper backends behind one interface |
| Browser research | `src/max/browser/` | URL guard (SSRF/private-range/redirect checks) + Playwright MCP provider |
| Evaluation | `src/max/eval/`, `benchmarks/` | 14 scenarios, improvement scan, orchestration benchmark |

Deployment: `Dockerfile` + `railway.toml` (added by the owner's PR #2),
`.github/workflows/ci.yml` (tests, static scan, health, eval, skills).
No other deployment manifests exist.

### 1.2 Findings from this audit

1. **Ordinary messages could be silent while commands appeared to work.**
   Three distinct causes, now all instrumented:
   - empty message content (privileged **Message Content Intent** off at the
     Discord side) — previously returned `ignored/empty_message` with **no
     audit record at all**;
   - `cooldown` and `provider_backoff` suppressions — also previously
     unrecorded;
   - empty allowlist / DMs off — the bot is *supposed* to be silent, but
     nothing said so at startup.
   **Fixed in Phase A** (§3): every route now produces a
   `discord_message_outcome` event, empty-content counters feed
   `!max status`, and `on_ready` logs the effective channel policy.
2. **`MAX_DISCORD_ENABLE` was documented as a master switch but never read
   by `max discord run`** — only by `discord check`. Now explicit and
   backwards-compatible: the run path warns and audits
   `discord_enable_flag_off` instead of silently ignoring the flag, and
   `discord check` separates advisory warnings from a `misconfigured`
   exit status. See `docs/CONFIGURATION.md` §5.
3. **`.env.example` omitted three variables the source reads** —
   `MAX_API_KEY`, `MAX_ENABLE_IMPROVE`, `MAX_ENABLE_MCP`. Documented now.
4. **`required_permissions()` advertised a non-existent permission**
   (`"Use Slash/Focus? (not required in Phase 1)"`). Removed; the list now
   matches `docs/DISCORD.md` exactly and is asserted by a test.
5. **Documentation drift**: `docs/DISCORD.md` troubleshooting blamed
   `MAX_DISCORD_ENABLE=0` for silence without mentioning the empty-allowlist
   and intent cases in the same breath; `docs/ARCHITECTURE.md` referenced
   `MAX_MOCK_PROVIDER_KEY`, which MAX does not read (it belongs to the
   external OpenClaw tool). Both clarified.
6. **Ignored channels reported the wrong reason** for ordinary conversation
   (`channel_not_allowlisted` instead of `ignored_channel`), making the
   silence unexplainable. Fixed in the router.
7. **Two spellings of the start command** (`Dockerfile CMD` and
   `railway.toml startCommand`) — harmless but must be kept in sync;
   documented rather than removed, because `railway.toml` belongs to the
   owner's merged PR.
8. **No secret exposure found.** `.dockerignore` excludes `.env`, `var/`,
   `workspace/`; token is never printed; health payload pops `key`;
   provider label used in banners is the provider *name* only.

### 1.3 Production startup path (verified by reading the code, not run)

`railway.toml startCommand` / `Dockerfile CMD` → `python -m max discord run`
→ `cmd_discord` → `build_router(core)` → real agent, real audit log, real
memory, orchestration supervisor if enabled → `MaxDiscordBot` → the same
`DiscordRouter` used by the tests. Provider configuration comes from the
process environment (Railway Variables), falling back to `mock` when unset —
which means **an unconfigured deployment replies with `MOCK_REPLY:` rather
than failing loudly**. That is by design for tests, and is called out in
`docs/SETUP.md` §3 as the first thing to change in production.

### 1.4 Resources (8 GB always-on laptop)

Measured earlier in `docs/BENCHMARKS.md`: core idle RSS ~25 MB, ~28 MB under
full evaluation; the whole test suite runs in ~15 s. Optional components are
lazy: speech loads a model only when used, tree-sitter only when installed,
MCP/browser only when configured, orchestration disabled with
`MAX_ORCH_ENABLE=0`. Nothing always-on is added by default; no local model
inference is required.

### 1.5 Security gaps and honest limitations

* **No kernel/container sandbox** for the coding worker (documented in
  `docs/SECURITY.md` §7) — mitigated by allowlisted argv, filtered
  environment, cwd confinement and network denial.
* **No live Discord verification** in this environment (no token) — every
  Discord behaviour is covered by mocked tests and must be re-checked by the
  owner with `docs/DISCORD.md` §8 and §11.
* **Voice, music, moderation, scheduler and multi-client API do not exist**
  in the codebase at all (§2) — no stubs are pretending otherwise.

---

## 2. Capability matrix

| Capability | Status | Evidence / blocker |
|---|---|---|
| Agent loop, tool registry, policy gate, audit | **[tested]** | 300+ unit tests, integrity-pinned policy, `max health` `policy_ok` |
| Provider adapters (mock / OpenAI-compatible) | **[tested]** + **[partial]** | mocked unit tests; no real endpoint credentials here |
| Discord no-prefix chat, allowlist, commands, loop guards, rate limit, backoff | **[tested]** | `tests/test_discord_*.py` (72 tests) |
| Message outcome tracing, intent diagnostics, startup banner (Phase A) | **[tested]** | `tests/test_discord_observability.py` |
| Caller-scoped memory with privacy caps | **[tested]** | `tests/test_usermemory.py` |
| Skills registry, provenance, staged promotion, rollback | **[tested]** | `tests/test_skills.py`, `max improve scan` 0 findings |
| Evaluation suite (14 scenarios) + improvement scan | **[tested]** | `max eval run` 14/14, `max improve scan` 0 |
| MCP client (schema validation, per-tool permissions, timeouts) | **[tested]** | `tests/test_mcp.py`, `tests/test_mcp_backend.py` (mock servers) |
| Coding worker: worktree, allowlisted commands, diff, approvals | **[tested]** | `tests/test_worker.py` |
| Multi-agent orchestration: scheduler, grants, conflict/review checks | **[tested]** | `tests/test_orchestration.py`, `benchmarks/orch_bench.py` |
| Tree-sitter / regex repository intelligence | **[tested]** | `tests/test_codesearch.py`; grammar availability depends on install |
| Speech-to-text (mock + faster-whisper adapter) | **[mock]** + **[blocked]** | adapter tested with mocks; real model download/inference not run here |
| Browser research (URL guard + Playwright MCP) | **[tested]** guard / **[mock]** provider | SSRF and host-policy tests pass; no browser binary installed here |
| Discord live connection, real model replies | **[blocked]** | no bot token, no provider credentials in this environment |
| Voice receive (discord-ext-voice-recv) | **[not implemented]** | no voice modules exist; compatibility must be verified first |
| TTS (Kokoro) | **[not implemented]** | no TTS module |
| Music (Lavalink/Wavelink) | **[not implemented]** | no music module |
| Moderation module | **[not implemented]** | bot has no destructive permissions by design today |
| Scheduled/proactive tasks | **[not implemented]** | — |
| Multi-client authenticated API / web UI | **[not implemented]** | — |

---

## 3. Phase A changes (this revision)

**Code**

* `src/max/discord/router.py` — universal `discord_message_outcome` audit
  trace (throttled for pure ignores, never throttled when MAX is mentioned),
  empty-content counters, `diagnostics()` surfaced in `!max status`,
  accurate `ignored_channel` reason for conversation.
* `src/max/discord/bot.py` — `startup_summary()` (pure, tested) logged and
  audited at READY with response mode, DM policy, allowlist/ignored/admin
  counts, sanitized provider name, intents and guild count; typing indicator
  around the model call; `on_message_edit` traced (never answered — replying
  to edits loops); send failures now include the failed action/reason;
  fixed the permission list.
* `src/max/cli.py` — `discord_check_report()` extracted as a pure,
  unit-testable function separating warnings from `misconfigured` (exit 1);
  added the token-but-disabled advisory warning, an admin-configuration
  warning, and the provider label on the startup line.

**Configuration / docs**

* `.env.example` — documented `MAX_API_KEY`, `MAX_ENABLE_IMPROVE`,
  `MAX_ENABLE_MCP`; corrected the `MAX_DISCORD_ENABLE` description.
* New `docs/CONFIGURATION.md` — canonical variable inventory generated from
  the source, Docker/Railway mapping, non-MAX names called out.
* New `docs/IMPLEMENTATION_STATUS.md` — this audit and the capability matrix.
* `docs/DISCORD.md` §6/§11 — outcome table, startup banner, intent diagnosis.

**Tests** — new `tests/test_discord_observability.py` (13 tests): outcome
tracing for reply/command/suppressed/ignored/error paths, throttling rules,
mention exemption, empty-content intent diagnosis, startup summary secrecy,
exact permission list, check-report exit semantics.
