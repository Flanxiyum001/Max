# MAX — Windows Desktop Assistant + Discord Agent

MAX now has a **voice-first Windows companion** (compact by default; an optional
full dashboard is available using `--dashboard`), with optional push-to-talk
transcription, Windows voice output, safe app launching,
browser searches, and persisted gaming settings profiles. The original
Discord agent remains supported. For full capabilities and limitations,
see [Windows PC setup](docs/PC-WINDOWS.md).

## Windows PC quick start

From PowerShell in your cloned MAX directory:

```powershell
git pull
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[pc,stt,desktop]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m max pc status --json
.\.venv\Scripts\python.exe -m max pc smoke
.\.venv\Scripts\python.exe -m max pc run
```

Double-click `scripts\launch-max-pc.bat` after installation. The compact assistant lets normal applications remain visible. Enter
`hi max`, speak through **TALK**, then `bye max`. Configure a real
model via `.env` for actual AI responses. The default model is a mock,
and the app does **not** automatically alter game settings or play Spotify
without authorized playback integration.

Desktop controls use explicit grants, and the microphone is never
accessed passively. Windows CI tests the controller and CLI but cannot
replace an interactive test on your own gaming PC.

---

## Historical Stage 2 notes

The following sections are a historical snapshot. Individual test counts,
dependency lists and older Discord status claims below may be out of date;
use current GitHub Actions and `docs/PC-WINDOWS.md` for current state.


MAX is a lightweight, always-on-friendly assistant core with an Agent-Skills
skill registry, a policy-guarded self-improvement loop, a reproducible
evaluation suite, an OpenAI-compatible provider adapter, and optional
integrations with OpenClaw, MCP and nanobot.

Stage 1 was never committed to this repository (the repo contained only this
README), so Stage 2 first built a working core and then integrated the
open-source candidates on top of it — measured, tested, and documented.

## Quick start

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest              # 167 tests
.venv/bin/python -m max health          # status: ok
.venv/bin/python -m max eval run        # 14/14 scenarios, ~1.5 s
.venv/bin/python -m max ask "hello"     # offline mock provider by default
.venv/bin/python -m max discord check   # Discord readiness (Phase 1 text bot)
```

> **v1 status:** Discord text MVP implemented and unit-tested (branch
> `v1/discord-text`); live connection pending a bot token. Voice and music
> are **not implemented** — see [docs/V1-CHECKLIST.md](docs/V1-CHECKLIST.md).

Real model? Copy `.env.example` → `.env`, set `MAX_PROVIDER=openai`,
`MAX_BASE_URL`, `MAX_MODEL`, export your key as `$FREELLM_API_KEY`. The
endpoint is deliberately left blank here: no FreeLLM credentials existed in
this environment and endpoints are never guessed.

## Measured results (summary — details in [docs/BENCHMARKS.md](docs/BENCHMARKS.md))

| Config | Evaluation | Median latency | Idle RSS | Notes |
|---|---|---|---|---|
| **A** MAX Core | 8/8 executed (6 subsystem scenarios skipped) | 0.52 ms | **24.6 MB** | skills/MCP/improve off |
| **B** A + OpenClaw gateway | health probe 22.6 ms | n/a | **~1.0 GB** | gateway passes its security matrix; too heavy always-on |
| **C** nanobot 0.3.5 | one-shot conversation OK | 2.09 s wall | 145 MB peak | 311 MB install; different skill format |
| **D** MAX full | **14/14 executed, 100%** | 1.44 ms | **25.0 MB** | +14.6 ms build, +0.4 MB vs A |

Cold start 103–124 ms; 96/96 unit+integration tests green; no security
failures observed in any configuration.

## What's in the box

| Deliverable | Location |
|---|---|
| Working code | `src/max/` (providers, core loop, skills, MCP, improve, eval, bench, CLI) |
| Architecture + integration report | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Test results & resource measurements | [docs/BENCHMARKS.md](docs/BENCHMARKS.md), `eval/runs/`, `benchmarks/results/` |
| Candidate repository comparison | [docs/CANDIDATES.md](docs/CANDIDATES.md) |
| Setup / operation / recovery commands | [docs/SETUP.md](docs/SETUP.md), [.env.example](.env.example) |
| Discord setup (intents, least-privilege perms, privacy) | [docs/DISCORD.md](docs/DISCORD.md) |
| v1 implementation checklist | [docs/V1-CHECKLIST.md](docs/V1-CHECKLIST.md) |
| Dependencies, licenses, security concerns | [docs/SECURITY.md](docs/SECURITY.md) |
| Prioritized next stage | [docs/ROADMAP.md](docs/ROADMAP.md) |
| Evaluation suite (14 scenarios) | `eval/suite/*.yaml` |
| Trusted improvement policy | `config/policy.yaml` + `config/policy.sha256` |
| Shipped demo skill (Agent Skills standard) | `skills/csv-summary/` |

## Implemented vs deferred

**Implemented and tested in this stage**

* MAX core: agent loop, JSONL sessions, bounded memory, workspace-sandboxed
  tools, audit log, CLI (`health/ask/skills/eval/improve/bench`).
* OpenAI-compatible provider adapter: configurable base URL/model, key from
  env only, timeouts + retries, typed errors that never crash the service;
  full test coverage with a mock (no credentials needed).
* Agent Skills registry: spec-compliant validation, discovery/progressive
  disclosure, sha256 provenance with drift detection, staging, evaluation
  history, promote, rollback; one shipped skill (`csv-summary`) run and
  verified.
* Autonomous improvement: isolated worktree/branch, static + secret +
  protected-path gates, baseline-vs-candidate test comparison, structured
  JSON reports, auto-promote under policy, human-approval path, rollback;
  policy integrity-pinned and provably non-bypassable for sensitive
  categories (tests included).
* MCP: stdio client (`initialize`/`tools/list`/`tools/call`) bridged into
  the agent's permission policy; tested against a bundled mock server **and**
  against the real `openclaw mcp serve`.
* Evaluation suite: 14 scenarios covering conversation, context retention,
  storage, restart recovery, tool execution, unauthorized rejection,
  skill discovery/loading, MCP, code-edit regression, provider failure, and
  prompt injection via file *and* skill — reproducible and gate-aware.
* Benchmarks A/B/C/D with raw JSON output.
* OpenClaw (optional): installed in isolation, gateway health/auth/tool
  deny-list/skills/MCP/conversation/shutdown/restart all verified.

**Deferred (with reasons)** — see [docs/ROADMAP.md](docs/ROADMAP.md):
real-endpoint verification (no credentials present), laptop re-measurement,
streaming responses, model-driven skill-script execution, session-history
search, replacing MAX Core with OpenClaw (fails the 8 GB budget), DSPy/GEPA
learning loop (one gated writer only; AGPL evolver avoided), channel
pairing/tunnels/systemd auto-exposure (explicitly out of scope).

## Safety posture in one paragraph

Exec and network tools are denied by default by a policy the model cannot
see or edit; every tool decision is audited; injection markers in untrusted
content are flagged and never grant permissions; flagged skills cannot load
or promote; improvements are integrity-pinned, gated, reversible and
commit-tracked; no secrets are committed; nothing listens beyond loopback.
Read [docs/SECURITY.md](docs/SECURITY.md) for the honest limitations.
