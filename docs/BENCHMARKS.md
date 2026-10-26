# Stage 2 — Benchmark and Comparison Report

All numbers in this file were produced in this workspace on 2026-10-26 by
`max bench` and `max eval run`. Raw JSON: `benchmarks/results/*/results.json`
and `eval/runs/*.json`. **Every measurement uses the mock provider — no real
inference, no network, no API keys** (per the brief: use mock providers
whenever real inference is unnecessary).

## Test environment

| Item | Value |
|---|---|
| OS | Ubuntu 24.04.5 LTS (container) |
| Python | 3.12.3 (`uv`-managed venv) |
| Node | v24.21.0 (used only for OpenClaw) |
| CPU | 2 vCPU |
| RAM | reported 3.8 GiB at audit start, 16.2 GiB later in the session (host reconfiguration); **all figures below are per-process RSS**, which is what transfers to the 8 GB target laptop |
| Repo at Phase 1 | `README.md` containing only `# Max` — **no code, no tests, no measurable baseline** |

**Baseline note:** because Stage 1 was never committed, there was no existing
application to measure. The first measurement point is MAX v0.2.0 itself,
recorded as configuration A below. `git log` shows the pre-existing history is
a single "Initial commit" with just the README.

## Method

* `max bench --configs …` runs each MAX configuration in a **fresh
  subprocess** so memory figures do not bleed between configs.
* Latency = 20 sequential mock round-trips through the full agent loop
  (session append included); median and p95 reported.
* `startup_ms` = wall time to import MAX and build a wired agent in-process.
* `cold_start_ms` = wall time of a cold `python -m max health --json`
  subprocess (median of 3).
* `idle_rss_kb` = VmRSS after the workload + 0.5 s settle.
  `peak_rss_kb` = `ru_maxrss(max(self, children))` over the whole worker
  (includes git/pytest children during the evaluation suite).
* Evaluation pass rate = the 14-scenario suite; scenarios whose subsystem is
  disabled in that configuration are **skipped, not counted as passes**
  (`executed`/`skipped` reported separately).

## Results — MAX configurations (A vs D)

From `benchmarks/results/20261026T042038Z/results.json`:

| Metric | **A: MAX Core** | **D: MAX full (skills+eval+improve)** |
|---|---|---|
| Evaluation pass rate | **8/8 executed (100%)**, 6 skipped¹ | **14/14 (100%)**, 0 skipped |
| Median mock latency (p95) | 0.52 ms (0.60 ms) | 1.44 ms (1.57 ms) |
| Idle RSS | **24.6 MB** | **25.0 MB** |
| Peak RSS (incl. test children) | 24.8 MB | 27.7 MB |
| Agent build (warm) | 0.5 ms | 14.6 ms |
| Cold start (`max health`) | 103–124 ms | 103–124 ms |
| Eval suite wall time | 0.05 s (8 executed) | 1.34 s (14 executed) |
| Integration complexity | stdlib + `pyyaml`; 1 process | + registry state, `config/policy.yaml`, eval suite |

¹ Skipped in A: skill discovery, skill loading, skill-injection rejection,
MCP integration, improvement-rejected, improvement-promoted — i.e. the
Stage-2 subsystems. A's 100% covers only what A claims to do; **capability
coverage is 8/14 vs 14/14**, which is the real difference between the two.

Skill/eval machinery therefore costs ~0.9 ms per turn and ~0.4 MB RSS —
irrelevant next to model latency in a real deployment.

## Results — configuration B: OpenClaw gateway

Measured while the isolated gateway was running (loopback:18899, token auth,
mock model provider):

| Metric | Measured |
|---|---|
| Install footprint (`npm install --prefix`) | **619 MB** |
| Start → `{"ok":true,"status":"live"}` | **15.8 s** (startup log: 14.8 s to post-attach) |
| Idle RSS right after start | **862 MB** |
| RSS at 37 s idle | **1.03 GB** |
| RSS after one agent turn | **1.25 GB** |
| Clean shutdown (TERM → port refusing) | **0.53 s** |
| `GET /health` latency (MAX `bench --configs B`) | **22.6 ms**, HTTP 200 |
| Conversation (`openclaw agent -m … --json`) | exit 0, reply from mock provider `maxmock/max-mock-1` |
| `POST /tools/invoke` with token (`sessions_list`) | HTTP 200, real result |
| `POST /tools/invoke` **without** token | **401** |
| `POST /tools/invoke` with wrong token | **401** |
| `POST /tools/invoke` tool `exec` (hard-deny list) | **404** (denied) |
| `/v1/chat/completions`, `/v1/models` (disabled surface) | **404** |
| Skills discovery | 22/55 ready; `skills info add-model-provider` loads SKILL.md details |
| MCP bridge (`openclaw mcp serve` ← MAX MCP client) | initialize OK, protocol 2024-11-05, 9 tools listed, `conversations_list` returned `conversations: 0` |

**Security failures observed: none.** Auth, deny-list and disabled-surface
behaviour all matched the documentation.

**Verdict:** OpenClaw integrates cleanly and passes its basic test, but
**~1 GB idle RSS (31× MAX Core) plus a 619 MB install** makes it a poor
always-on default on an 8 GB laptop. It remains an optional gateway, off by
default.

## Results — configuration C: nanobot

| Metric | Measured |
|---|---|
| Install (`uv tool install nanobot-ai`) | **311 MB**, v0.3.5 |
| `nanobot --version` | 731 ms |
| One-shot conversation vs local mock (`nanobot agent -m … --classic`) | exit 0, **2.09 s** wall, **145 MB** peak RSS |
| Eval-suite parity | not applicable — different skill format and no gated self-improvement loop; its CLI was probed instead |

nanobot is the lightweight alternative: ~7× less peak RSS than OpenClaw's
gateway for a conversation, but it does not speak the Agent Skills standard
and provides no evaluation/promotion pipeline, so it does not replace MAX
Core on capability grounds.

## Integration complexity (subjective but grounded)

| Config | External services | Config surface | Install cost | Glue code in MAX |
|---|---|---|---|---|
| A | 0 | 8 env vars (`.env.example`) | 2 PyPI packages | 0 lines |
| B | 1 gateway (+ its model provider) | +`MAX_OPENCLAW_URL`, `MAX_OPENCLAW_TOKEN`, gateway env | 619 MB npm | HTTP probe only (documented in `docs/ARCHITECTURE.md`) |
| C | 0 | nanobot's own `config.json` + preset | 311 MB uv | none (external CLI) |
| D | 0 | +`MAX_AUTOPROMOTE`, `config/policy.yaml` | unchanged | registry + pipeline (already in tree) |

## Security failures and unresolved limitations

* No unauthorized tool call succeeded in any configuration; every rejection
  is in the audit log (`var/audit/audit.jsonl`) and asserted by scenario 06.
* Prompt-injection scenarios 07/08 pass: content flagged, dangerous tool
  still denied, malicious skill blocked from loading and from promotion.
* **Unresolved / limitations:**
  1. No real-model latency or quality numbers — the FreeLLM endpoint and
     key were not present in this environment (see `docs/SECURITY.md`).
  2. Config B measures gateway health, not an end-to-end MAX→OpenClaw
     *conversation through MAX's provider adapter* — that path needs the
     gateway's OpenAI surface enabled (disabled by default, 404 verified).
  3. OpenClaw RSS figures come from one machine and one session; treat them
     as ±20%.
  4. All measurements ran on a container, not the physical laptop; re-run
     `max bench` there before final sizing.

## Reproduce

```bash
uv venv --python 3.12 .venv && .venv/bin/pip install -e '.[dev]'  # or uv pip
.venv/bin/python -m max eval run --label reproduce
.venv/bin/python -m max bench --configs A,D
# with the optional gateway running:
MAX_OPENCLAW_URL=http://127.0.0.1:18899 .venv/bin/python -m max bench --configs B
PATH="$HOME/.local/bin:$PATH" .venv/bin/python -m max bench --configs C
```

## Results — advanced capability integration (26 Oct 2026)

Measured in this workspace (Linux, Python 3.12.3, single run, mock provider).
Reproduce with the commands below; numbers are observed, not estimated.

| Gate | Before (Phase 0 baseline) | After integration |
|---|---|---|
| `pytest -q` | **167 passed** (12.02 s) | **250 passed** (12.41 s) |
| `max health` | exit 0, `status: ok` | exit 0, `status: ok` |
| `max eval run` | 14/14 (100%), 1.41 s | 14/14 (100%) |
| `max improve scan` | 0 findings | 0 findings |
| New test modules | — | worker, codesearch, browser guard, MCP backend, speech, adversarial dataset |

Capability-specific observations:

| Capability | Measured | Blocked / not measured |
|---|---|---|
| tree-sitter indexing | `tree-sitter 0.26.0` + `tree-sitter-python 0.25.0` installed; symbol index over the fixture repo returns functions/classes with line ranges; regex fallback used when absent | grammars for JS/TS/Go/Java not installed → regex rules used (declared, not a claim of full parsing) |
| Coding worker | worktree create → run → diff → discard completes in-process; per-command timeout and output caps enforced | no kernel sandbox → **not** a container benchmark |
| MCP backend | fake-server tests cover start/list/call/error/timeout paths in milliseconds | real SDK server not exercised (`mcp` extra not installed here) |
| Browser guard | URL validation is pure in-memory work (<1 ms per check) | **blocked**: Playwright browser binaries absent → no navigation benchmark |
| Speech (mock) | `benchmark()` reports real per-sample latency for the mock backend | **blocked**: faster-whisper `[stt]` and model weights absent → no real transcription number is claimed |
| Promptfoo | dataset of 13 versioned cases consumed by 10 offline tests | **deferred**: Node ≥22 runtime not part of this Python project |

```bash
# reproduce
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_adversarial_dataset.py -q
.venv/bin/python -m max eval run --label after-integration
.venv/bin/python -m max improve scan
```
