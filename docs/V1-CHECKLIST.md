# MAX v1 — Implementation checklist

Status legend: **[done]** verified by tests/commands in this repo · **[pending]**
not started · **[blocked]** cannot proceed without user credentials/hardware ·
**[deferred]** deliberately later.

## Phase 0 — Repository preservation and baseline **[done]**

- [x] Inspected branch, status, history (3 commits: initial + Stage 2 core + docs), 65 tracked files, clean tree.
- [x] Confirmed no deployment manifests exist (no Railway/Docker/Procfile files to preserve).
- [x] Baseline: `pytest` **96 passed**; `max health` **status ok**; `max eval run` **14/14 (100%)**; `max improve scan` **0 findings**.
- [x] Confirmed no pre-existing Discord/voice/music code (`rg` over src/tests/docs → only doc mentions).
- [x] Feature branch `v1/discord-text` created; no files overwritten.

## Phase 1 — Discord text and coding MVP **[done — code+tests; live run blocked on token]**

- [x] Allowlisted no-prefix chat (guild text channels only) — `router.py`, tested
- [x] Explicit DM policy (off by default; `allowlist`/`everyone` modes) — tested
- [x] Loop / duplicate / self-reply protection + rate limiting + failure backoff — tested
- [x] `!max` command set: help, health, status, perms, memory, channels, allow/deny — tested
- [x] Central authorization for config changes (execution-layer admin check + audit) — tested incl. social-engineering case
- [x] Per-channel conversation context (bounded) + caller-scoped memory with secret refusal — tested
- [x] Coding read tools through existing policy gate + new `search_files` tool — tested
- [x] Message chunking, provider-failure handling, injection flags surfaced — tested
- [x] Env-var documentation ([.env.example](../.env.example)) + [docs/DISCORD.md](DISCORD.md) (intents + least-privilege perms)
- [x] Regression: baseline 96 tests stayed green; suite now **167 passed**; eval 14/14; scan 0 findings

**Exit criteria:** *live conversation with an authorized tester is [blocked] —
no Discord token in this environment; steps 1–6 of the live checklist in
`docs/DISCORD.md` §8 must be run by the owner.*

## Phase 2 — Coding workflow and integrations

- [ ] Isolated branch/worktree code-change flow surfaced to Discord (reuse `max improve`)
- [ ] Diff + test-result presentation in chat
- [ ] Scoped GitHub/MCP credentials (approval required for push/PR/protected branch)
- [ ] Tests: tool authorization, malicious repo content, secret redaction, timeout, rollback

## Phase 3 — Voice (both modes) — not started

- [ ] Select/document Discord voice implementation (discord.py voice + ffmpeg)
- [ ] Explicit activation command first; conversation mode second
- [ ] STT/TTS providers configurable; listening indicators; pause/resume/leave
- [ ] Privacy: no raw-audio retention by default, documented transcript retention
- [ ] Wake-word: **deferred** until a working prototype exists (not claimed now)

## Phase 4 — Music + Spotify feasibility — not started

- [ ] Permitted source adapter + queue controls (play/pause/skip/stop/queue/NP/volume/leave)
- [ ] Session ownership + DJ/moderator authorization
- [ ] Spotify: document supported API capabilities only; **no streaming claim** until verified

## Phase 5 — Skills, self-improvement, hardening — partially inherited

- [x] Skill registry with provenance, staging, evaluation gate, rollback (Stage 2)
- [x] Policy-integrity pin + approval separation (Stage 2)
- [x] Map new Discord capabilities to skill/eval suites (Discord paths are
      exercised through the existing policy gate, eval suite and 71 tests)
- [~] Security review + feature-readiness report — `docs/SECURITY.md`
      (threat model, limitations, dependency licenses, secret handling,
      network audit) covers Stage 2 + Phase 1; a fresh Phase 5 review of
      code written after this checklist is still open
- [x] Setup docs incl. intents/permissions/troubleshooting — `docs/DISCORD.md`
      §1–§8 + §10, `docs/SETUP.md` §5–§8
- [x] Deployment proposal (separate document, **no auto-deploy**) —
      [`docs/DEPLOYMENT.md`](DEPLOYMENT.md): options A/B, approval gate,
      rollback; nothing deployed, no Railway/Docker/Procfile config exists

## Cross-cutting constraints (verification status)

- Existing API/module boundaries preserved: Stage 2 core touched only
  additively (`search_files` tool registration, health-payload refactor,
  new `discord` subpackage).
- No secrets committed; token read from env only, never logged.
- Real Discord connection **not executed** (no token here) — every Discord
  behaviour is covered by mocked tests and must be re-verified live by the
  owner using `docs/DISCORD.md` §8.
- No deployment files changed or created; **nothing deployed** — the only
  automation is `.github/workflows/ci.yml` (see below).
- Dependency added: `discord.py>=2.7.1` (MIT) — declared in `pyproject.toml`.

## CI status — green

Workflow `.github/workflows/ci.yml` (pytest, static scan, health, eval,
skill validation; mock provider, no secrets) runs on every push and PR.

| Run | Trigger | Commit | Conclusion |
|---|---|---|---|
| CI #1 | push | `e66a9e8` on `v1/discord-text` | **success** (26 s) |
| CI #2 | pull request #1 | `e66a9e8` | **success** (30 s) |
| CI #3 | push | `30822fd` on `main` | **success** (25 s) |

`v1/discord-text` was merged into `main` (merge commit `30822fd`); the
merged tree is byte-identical to the feature branch, so local results and
CI results describe the same code. Local re-verification on that tree:
**167 passed**, `max health` exit 0 (`status: ok`, `policy_ok: true`),
`max improve scan` exit 0 (0 findings), eval **14/14 (100%)**.

All commits are authored by the project owner — `Flanxiyum001
<pourushnair6@gmail.com>` on every code commit — with **zero**
`Co-Authored-By` / `Generated with` trailers and zero matches for
freebuff/codebuff anywhere in tracked files.

### Test totals

| | baseline | after Phase 1 | after advanced integration |
|---|---|---|---|
| pytest | 96 passed | 167 passed | **250 passed** |
| eval suite | 14/14 | 14/14 | 14/14 |
| improve scan | 0 findings | 0 findings | 0 findings |

## Advanced capability integration — status

| Capability | Status | Evidence / blocker |
|---|---|---|
| **A. Isolated coding worker** | **implemented + tested** | `src/max/worker/`, `tests/test_worker.py`: worktree isolation, argv allowlist, env filtering, diff + secret scan (incl. untracked), cancel/discard, approval separation, task caps |
| **B. Tree-sitter repository intelligence** | **implemented + tested** | `src/max/codesearch/`, `tests/test_codesearch.py`: symbols with line ranges, malformed-source reporting, skip rules, path/symlink escape rejection, symbol-vs-text-search comparison; regex fallback when the binding is absent |
| **C. MCP interoperability** | **implemented + tested (local stdio backend)** | `src/max/mcp/backend.py` adapter boundary, per-server allow/deny/approval, env filtering, typed failures; official MCP SDK **deferred** (see `docs/INTEGRATIONS.md` §3) |
| **D. Playwright MCP browser research** | **guard + gating tested; live browsing blocked** | `src/max/browser/`, `tests/test_browser_guard.py`; browser binaries are not installed here, so no navigation was executed |
| **E. Promptfoo evaluation layer** | **dataset + offline gate implemented; runner deferred** | `eval/promptfoo/` (13 cases), `tests/test_adversarial_dataset.py` (10 tests); Promptfoo needs Node ≥22 — `docs/INTEGRATIONS.md` §4 |
| **F. faster-whisper speech-to-text** | **adapter + mock tested; real benchmark blocked** | `src/max/speech/`, `tests/test_speech.py`: limits, temp-file deletion, no transcript logging, lazy availability; `[stt]` extra and model weights absent |
| OpenHands SDK as worker runtime | **adapter built; backend optional (not installed)** | `src/max/orchestration/coding.py`, `[openhands]` extra, `MAX_ORCH_CODER_BACKEND`; falls back to the guarded MAX runner — `docs/INTEGRATIONS.md` §1 |
| Voice / shared music (Phase 6) | not started — not claimed | — |

All new tools are registered through the single `ToolRegistry`/`ToolExecutor`
boundary (permission check + audit), are category-gated (`exec`/`net` denied by
default), and the core starts with every optional dependency missing.

## Multi-agent orchestration — status

| Requirement | Status | Evidence / blocker |
|---|---|---|
| Dynamic delegation decision | **implemented + tested** | `should_delegate`: length floor + actionable signal; simple questions decline in <0.1 ms (`benchmarks/orch_bench.py` `+decline` rows; `test_simple_question_is_not_delegated`) |
| ≥2 independent tasks in parallel | **implemented + tested** | researcher ∥ coder overlap asserted in `test_parallel_research_and_coding_tasks_run_concurrently`; concurrency capped at 2 (hard cap 3) |
| Dependency-aware scheduling | **implemented + tested** | order, failed-dependency propagation, cycle and plan-size refusal, timeout, crash isolation, cancellation |
| Coder in isolated workspace with reviewable diff | **implemented + tested end-to-end** | `test_full_plan_edits_runs_tests_and_reviews`: real git worktree, real edit, **real pytest run**, diff, cleanup, source repo untouched |
| Tester reports real results / reviewer inspects independently | **implemented + tested** | blocked runs are reported as `blocked`, never `passed`; reviewer is read-only and cannot approve its own change |
| Task status, ownership, cancellation, cleanup | **implemented + tested** | `!max tasks` / `task run|show|diff|cancel`, ownership denial audited, atomic `var/tasks/*.json` store, worktree discard at plan end |
| Single consolidated Discord reply | **implemented + tested** | `Supervisor.render` produces one message; delegation failure falls back to normal chat (`test_router_survives_a_crashing_delegation_hook`) |
| Optional: core works when disabled/absent | **implemented + tested** | `MAX_ORCH_ENABLE=0`; missing OpenHands SDK reports a reason and falls back |
| Benchmarks report actual numbers | **done, no speedup claimed** | `benchmarks/orch_bench.py`, results in `docs/BENCHMARKS.md` — parallel ≈ serial on this workload (inside noise) |
| Real-model comparison (single vs multi agent) | **blocked** | no provider credentials in this environment; mock only |
| Live Discord task commands | **blocked on token** | same status as every other Discord feature here |
| Kernel-level sandbox for workers | **not done** | documented gap in `docs/SECURITY.md` §7 |

Gate for this phase: **299 passed** (251 before + 48 new), health `ok`,
eval **14/14**, improve scan **0 findings**, no secrets committed, nothing
pushed or deployed.