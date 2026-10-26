# Integrations — adoption decisions for six upstream projects

Status: **Phase 0 evaluation complete**, decisions recorded against verified
upstream state (checked 26 Oct 2026). Everything below describes what is
actually in this repository; nothing is claimed without a test or a command.

Decision vocabulary: **adopted** (dependency integrated) · **integrated as
subprocess/service** · **pattern borrowed** (no dependency) · **deferred**
(documented reason) · **blocked** (missing credential/hardware/platform).

| # | Project | Verified upstream | Decision | Where |
|---|---|---|---|---|
| 1 | OpenHands Software Agent SDK | `main`, v1.58.2, MIT, Python ≥3.12, heavy; agent runtimes use Docker/remote hosts | **Pattern borrowed → own worker**; SDK deferred | `src/max/worker/` |
| 2 | tree-sitter | `master` core 0.27.1, Python binding 0.26.0, MIT, wheels, no runtime deps | **Adopted as optional extra** (`[codesearch]`), regex fallback when absent | `src/max/codesearch/` |
| 3 | MCP Python SDK | `main`, 2.3.0, MIT, Python ≥3.11 | **Deferred** behind a stable adapter boundary; current stdio client stays the backend | `src/max/mcp/backend.py` |
| 4 | Promptfoo | `main`, 0.125.1, MIT, **Node ≥22.22**, large JS graph | **Deferred** as external runner; dataset delivered in this repo | `eval/promptfoo/` |
| 5 | faster-whisper | `master`, 1.2.1, MIT, Python ≥3.9, CTranslate2 + model downloads | **Adopted as optional extra** (`[stt]`), lazy, mock default | `src/max/speech/` |
| 6 | Playwright MCP | `main`, 0.0.83, Apache-2.0, Node ≥18, browser binaries | **Integrated as subprocess** behind MAX's guard; disabled by default | `src/max/browser/` |

## 1. OpenHands Software Agent SDK — pattern borrowed, SDK deferred

Why: the SDK brings an agent runtime, its own tool loop and (for real
execution) Docker or a remote host. That competes with MAX's authoritative
agent loop, policy gate and tool registry instead of extending them, and it
adds a privileged host dependency to an 8 GB always-on laptop.

What MAX took instead: the *workflow shape* — task record, isolated
workspace, bounded plan, approved command list, diff review, approval
request, discard. Implemented directly in `src/max/worker/`:

* `CommandPolicy` — deny-by-default argv allowlist (exact prefixes), no
  shell strings, no `-c`, no network tools, no git write subcommands,
  working-directory containment, filtered environment (no host secrets),
  timeout and output caps.
* `TaskManager` — one git worktree + branch per task, secret scan over the
  diff **and** untracked files, cancel/discard lifecycle, approval requests
  that the model can create but never grant.

Resource cost: zero new dependencies. Security: no Docker requirement, no
unrestricted shell, model cannot widen the policy.

Revisit if: MAX needs multi-step autonomous coding beyond what a bounded
command list can express — then run OpenHands in an isolated container with
its own network policy, as a *worker behind this same TaskManager*, not as a
second agent loop.

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

## Dependency and license summary

| Package | Version constraint | License | Where |
|---|---|---|---|
| tree-sitter | `>=0.24` | MIT | extra `[codesearch]`, dev |
| tree-sitter-python | `>=0.23` | MIT | extra `[codesearch]`, dev |
| faster-whisper | `>=1.2` | MIT | extra `[stt]` |
| mcp | `>=1.2` | MIT | extra `[mcp]` (declared, unused by default) |
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
* **Discord voice/music (Phases 6)** — not started, not claimed.
