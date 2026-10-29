# Integrations — adoption decisions for six upstream projects

Status: **Phase 0 evaluation complete**, decisions recorded against verified
upstream state (checked 26 Oct 2026). Everything below describes what is
actually in this repository; nothing is claimed without a test or a command.

Decision vocabulary: **adopted** (dependency integrated) · **integrated as
subprocess/service** · **pattern borrowed** (no dependency) · **deferred**
(documented reason) · **blocked** (missing credential/hardware/platform).

| # | Project | Verified upstream | Decision | Where |
|---|---|---|---|---|
| 1 | OpenHands Software Agent SDK | `openhands-sdk` 1.58.2, MIT, Python ≥3.12, LiteLLM-based; Docker/Agent Server only for remote sandboxes | **Adapter built, not installed by default** (optional extra `[openhands]`); guarded MAX runner is the default backend | `src/max/orchestration/coding.py` + `src/max/worker/` |
| 2 | tree-sitter | `master` core 0.27.1, Python binding 0.26.0, MIT, wheels, no runtime deps | **Adopted as optional extra** (`[codesearch]`), regex fallback when absent | `src/max/codesearch/` |
| 3 | MCP Python SDK | `main`, 2.3.0, MIT, Python ≥3.11 | **Deferred** behind a stable adapter boundary; current stdio client stays the backend | `src/max/mcp/backend.py` |
| 4 | Promptfoo | `main`, 0.125.1, MIT, **Node ≥22.22**, large JS graph | **Deferred** as external runner; dataset delivered in this repo | `eval/promptfoo/` |
| 5 | faster-whisper | `master`, 1.2.1, MIT, Python ≥3.9, CTranslate2 + model downloads | **Adopted as optional extra** (`[stt]`), lazy, mock default | `src/max/speech/` |
| 6 | Playwright MCP | `main`, 0.0.83, Apache-2.0, Node ≥18, browser binaries | **Integrated as subprocess** behind MAX's guard; disabled by default | `src/max/browser/` |

## 1. OpenHands Software Agent SDK — adapter built, backend optional

Verified (26 Oct 2026): package `openhands-sdk` 1.58.2, MIT, Python ≥3.12,
API surface `LLM` / `Agent` / `Tool` / `Conversation`, LiteLLM-style
`provider/model` strings with a configurable `base_url`, local workspace runs
in-process (Docker/Agent Server only for remote sandboxes),
`max_iteration_per_run` for bounded runs. Sources:
https://docs.openhands.dev/sdk/getting-started ·
https://pypi.org/project/openhands-sdk/ ·
https://docs.openhands.dev/sdk/arch/workspace ·
https://docs.openhands.dev/sdk/guides/security

**Decision:** install it only when explicitly requested
(`pip install -e '.[openhands]'` + `MAX_ORCH_CODER_BACKEND=openhands`).
Rationale: it brings LiteLLM, FastMCP and a second tool loop — real weight and
a second execution philosophy — for a configuration where MAX already has a
bounded, audited runner. It is an *optional backend*, never a second agent
loop: MAX's supervisor still owns the task record, the plan, the grant, the
limits and the result schema.

What the adapter does (`src/max/orchestration/coding.py`):

* lazy import; probes for `LLM`, `Agent`, `Conversation` and fails with an
  actionable message if the installed version does not expose them,
* `available()` reports the exact blocker (not installed / model unset / key
  env var missing) and the supervisor falls back to the built-in backend,
* provider model, base URL and the **name** of the key env var come from
  configuration; the key itself is never read into a task, log or prompt,
* explicit tool allowlist — terminal/execution tools are only offered when
  `MAX_ORCH_EXEC=1`, so the SDK cannot silently exceed MAX's command policy,
* SDK/provider exceptions become a failed task state, never an agent crash.

The built-in MAX runner (`src/max/worker/`, borrowed workflow shape — task
record, isolated worktree, argv allowlist, filtered env, diff secret scan,
approval requests the model can create but never grant) remains the default
and is what CI and the benchmark exercise.

Revisit if: coding tasks consistently need multi-step autonomous exploration
beyond the bounded command list — then install the extra and run it **inside
the same task worktree**, behind the same grant and audit, not as a second
agent loop. Never give it Docker socket access on the always-on laptop.

## 2. tree-sitter — adopted (optional extra)

`pip install -e '.[codesearch]'` → `tree-sitter` + `tree-sitter-python`.
When absent, `max.codesearch` falls back to bounded regex rules for
Python/JS/TS/Go/Java/shell and reports `backend: regex-fallback`; when a file
has no grammar it is reported as unsupported and callers keep using the
existing `search_files` tool.

Limits enforced in code: 512 KiB/file, 2000 files, 500 symbols, 6000-char
output, skip list for generated/binary/secret-looking files, no symlink
following, path containment via `resolve_within`. Parse errors are surfaced
(`parse_errors`) instead of pretending every file is valid.

License: MIT. Native build: none (prebuilt wheels).

## 3. MCP Python SDK — deferred behind an adapter boundary

Reason: MAX already ships a working stdio MCP client (`src/max/mcp/client.py`,
protocol `2024-11-05`, 7 passing tests). Two unrelated MCP implementations
would be worse than one, and the SDK's HTTP transports would add a network
surface the project does not need yet.

What was built instead: `src/max/mcp/backend.py` defines the **stable internal
interface** (`list_tools` / `call` / lifecycle) plus per-server policy:
disabled by default, explicit `tool_allow` (nothing auto-enabled), `tool_deny`,
`approval_required`, whitelisted environment, argument-size limits, injection
flagging, audit events, typed errors that never crash the agent.

Migration path: implement the SDK client as a second backend behind the same
manager (`_default_client_factory`) once HTTP transport is genuinely required;
the core, tests and tool surface do not change.

Security note: no local MCP server is exposed over the network by MAX; servers
are spawned as child processes with a filtered environment.

## 4. Promptfoo — deferred as an external runner, dataset delivered

Why deferred: Promptfoo is a Node ≥22 tool with a large JS dependency graph,
while MAX is a Python project with `pip install -e '.[dev]'` as its only
install path. Adding a second runtime for evaluations that MAX's own
14-scenario suite already runs deterministically would grow CI time and install
size without a measured gain. Its config/scripts execute with the invoking
user's privileges, which is another reason not to wire it in blindly.

What exists now: `eval/promptfoo/` — a versioned, provider-agnostic dataset of
12 adversarial cases (unauthorized tool calls, repository/browser/MCP
injection, cross-user memory, secret exfiltration, unauthorized GitHub writes,
dangerous shell requests, worker timeout/cancel, malformed MCP responses,
prohibited navigation, misleading test reports, approval bypass) with
deterministic assertions that MAX's own suite can consume.

Adoption trigger: if you want provider A/B comparison or prompt regression
gating in CI, `npm i -g promptfoo && promptfoo eval -c eval/promptfoo/` on a
machine where Node ≥22 exists. Clearly marked as requiring real credentials
for model-backed cases; offline cases use mocks.

## 5. faster-whisper — adopted (optional extra, not loaded at startup)

`pip install -e '.[stt]'`. Backend selected by `MAX_STT_BACKEND`
(`mock` default). The wrapper imports `faster_whisper` only on first use and
loads the model with `local_files_only=True` by default, so **bot startup
never downloads anything**. Missing package → actionable `SttError`, text chat
unaffected (`speech_status` reports it).

Bounds: file size, WAV duration, concurrency semaphore, transcription timeout,
temporary audio file deleted in a `finally` block, audit records metadata only
(byte count, duration, latency) — never transcripts or audio.

Benchmarks: **not fabricated** — see `docs/BENCHMARKS.md`; model weights and
`[stt]` install are required before any real number can be produced.

## 6. Playwright MCP — integrated as a guarded subprocess, disabled by default

`src/max/browser/` is a policy layer that runs *before* any navigation:
scheme allowlist (`http/https`), no credentials-in-URL, port allowlist
(80/443), blocklists for localhost/loopback/link-local/private ranges/cloud
metadata, optional host allowlist, and DNS resolution checks (SSRF/redirect
defence). Only after the guard passes does the request reach the MCP manager,
which spawns `npx @playwright/mcp` (or any configured command) as a child
process with a filtered environment.

Read-only by default: no form-submission, mutation or authenticated-session
tools are registered. Browser tools are category `net` → denied by the
default permission policy, so they require explicit `MAX_TOOLS_ALLOW`.

Blocked here: browser binaries are not installed in this environment, so the
live research workflow is **not verified** — unit tests cover the guard, the
gating and the failure paths instead.

## 7. Desktop-control resources — evaluated 29 Oct 2026

Five projects were suggested for giving MAX "arms and legs": local machine
control, browser automation, a local model server and a grab-bag of MCP
tools. All five were checked upstream before deciding. Upstream facts below
were read from the repositories and package indexes on 29 Oct 2026.

| Resource | Upstream (verified 29 Oct 2026) | Decision | Why |
|---|---|---|---|
| Open Interpreter | `openinterpreter/openinterpreter` (Rust) 0.0.56, **Apache-2.0**; legacy Python dist `open-interpreter` 0.4.3 (Oct 2024), classic line **AGPL-3.0-only** | **Pattern borrowed, dependency rejected** | It *is* a second agent loop that runs model-chosen code on the host — exactly what MAX's mission forbids; the Python line is AGPL, and the Rust line brings its own model client. MAX already has a bounded, audited runner. |
| UI-TARS-desktop | `bytedance/UI-TARS-desktop` 0.3.1 (8 Oct 2026), **Apache-2.0**, TypeScript/Electron/Node ≥20, needs a vision-language model | **Deferred** | An always-running Electron app plus VLM weights on an 8 GB integrated-graphics laptop. MAX is text-first; the screenshot tool below is the seam if a multimodal provider is ever added. |
| browser-use | `browser-use/browser-use` 0.13.14 (26 Oct 2026), **MIT**, Python ≥3.11; drives browsers over **CDP** (`cdp-use`), *not* Playwright | **Deferred (duplicate of an existing capability)** | MAX already ships a guarded Playwright MCP browser layer with an SSRF/private-range guard and read-only tool surface. Adding browser-use would be a second browser agent stack with its own LLM loop. Revisit only if the CDP-based controller proves strictly better than the existing guard. |
| Ollama | `ollama/ollama` 0.40.13 (28 Oct 2026), **MIT**, Go | **Adopted as configuration — zero code, zero dependencies** | MAX's provider is already OpenAI-compatible, and Ollama serves an OpenAI-compatible subset including `/v1/chat/completions`. No account, no cost; only the model download touches the network. |
| Goose MCP extensions | `aaif-goose/goose` (formerly `block/goose`) 1.57.0 (28 Oct 2026), **Apache-2.0**, Rust | **Pattern borrowed: usable as ordinary MAX MCP servers; none installed** | MAX's MCP manager already spawns any stdio MCP server from `MAX_MCP_SERVERS`, so a Goose extension is a config entry, not a dependency. Each extension drags its own runtime (`npx`, `uvx`, Java, Docker) and may download packages, so none are enabled by default. |

### Ollama — how to actually wire it (no code changes)

```bash
# .env — see docs/CONFIGURATION.md §1
MAX_PROVIDER=openai
MAX_BASE_URL=http://127.0.0.1:11434/v1
MAX_MODEL=qwen2.5-coder:7b
MAX_API_KEY_ENV=OLLAMA_API_KEY
OLLAMA_API_KEY=ollama        # the local server wants a key *value* present and ignores it
```

Verified against https://docs.ollama.com/api/openai-compatibility: the local
server exposes an OpenAI-compatible subset at `/v1`, is free, and needs no
account. Model weights are a separate license/cost question — check the model
card before pulling one onto the laptop. Everything else about MAX
(timeout, retries, health check, mock fallback) is unchanged.

### Open Interpreter — what was borrowed instead

The useful idea is a *local terminal harness with an approval boundary*. MAX
implements that itself in `src/max/desktop/server.py` (new, this revision):
argv-only execution, a fixed deny list, workspace-bounded paths, a filtered
child environment, and a human approval that the model can request but never
grant. That is the Open Interpreter shape without the AGPL dependency, the
second agent loop, or unrestricted shell access.

### Goose extensions — how to add one if you ever need it

A Goose stdio extension is an MCP server. Add it to `MAX_MCP_SERVERS` with an
explicit `tool_allow` and (for anything that writes) `approval_required`:

```bash
# `command` is whatever the extension's own docs say to run — e.g. a stdio
# MCP server started with `npx -y <package>` or `uvx <package>`.
MAX_MCP_SERVERS=[{"id":"myext","command":["npx","-y","<the-extension-package>"],"enabled":true,"tool_allow":["list_events"],"approval_required":["create_event","delete_event"],"env_allow":["MYEXT_CREDENTIALS_PATH"]}]
```

The package name above is a placeholder: no specific Goose extension was
installed or verified in this repository, so none is named as an example that
works.

Rules that do not change: no tool is visible without `tool_allow`, credentials
never travel through `env_allow` (secret-shaped names are stripped by
`build_env`), and no server starts until a tool from it is actually called.

## Dependency and license summary

| Package | Version constraint | License | Where |
|---|---|---|---|
| tree-sitter | `>=0.24` | MIT | extra `[codesearch]`, dev |
| tree-sitter-python | `>=0.23` | MIT | extra `[codesearch]`, dev |
| faster-whisper | `>=1.2` | MIT | extra `[stt]` |
| mcp | `>=1.2` | MIT | extra `[mcp]` (declared, unused by default) |
| pyautogui | `>=0.9.54` | BSD-3-Clause | extra `[desktop]` (declared; not installed in CI) |
| promptfoo | — | MIT | not installed (Node) |
| playwright-mcp | — | Apache-2.0 | not installed (Node) |

Core installation remains `pyyaml` + `discord.py` only.

## Deferred / blocked — explicit list

* **OpenHands SDK as a runtime** — deferred (competing agent loop, Docker).
* **MCP SDK transport** — deferred (adapter ready, current client works).
* **Promptfoo runner** — deferred (Node ≥22, no measured need yet).
* **Playwright live research** — blocked (browser binaries absent); guard and
  gating are tested.
* **faster-whisper real benchmark** — blocked (model weights not present;
  `[stt]` not installed in CI); adapter + mock benchmark are tested.
* **Open Interpreter dependency** — rejected (AGPL on the Python line; a
  second host-executing agent loop). Pattern borrowed into
  `src/max/desktop/` instead.
* **UI-TARS-desktop** — deferred (Electron + VLM on an 8 GB laptop);
  screenshot seam exists at `desktop_screen`.
* **browser-use** — deferred (MIT, but a duplicate of the guarded Playwright
  browser layer already in `src/max/browser/`).
* **Goose MCP extensions** — none installed (each drags `npx`/`uvx`/Docker
  runtimes); the `MAX_MCP_SERVERS` path already supports them.
* **Live desktop input/screenshot verification** — blocked (no `pyautogui`
  and no display in this environment); the approval, policy and protocol paths
  are tested, and the missing-backend error path is asserted.
* **Discord voice/music (Phases 6)** — not started, not claimed.
