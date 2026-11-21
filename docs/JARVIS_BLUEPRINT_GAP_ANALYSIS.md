# "Jarvis mode" blueprint — gap analysis against the current tree

Status: **analysis only.** No runtime code changed. Compared against `max`-based branch at
`105172d` (after PR #49). "Exists" means code and tests are in the tree, not that it was
run against a live provider, microphone or device.

## Why the blueprint can't be applied as written

The blueprint was written without access to this repository ("I couldn't open your
src/max code"). Three of its assumptions don't hold:

1. **No `router.py` / `router.toml` was supplied.** They aren't in this repo or this
   session. The package is `max_core`, not `max`.
2. **A router already exists:** `max_core.gateway.router.Gateway`. It handles
   fail-closed privacy routing (`Privacy`, `InferenceDestination.VERIFIED_LOCAL`),
   pre-egress secret blocking (`privacy_guard`), worst-case USD admission, OpenAI and
   Anthropic adapters, bounded streaming (`gateway/streaming.py`), and task-aware ranking
   (`Gateway.plan_task` with `TaskKind`). A second router with its own `private=True`
   flag would bypass those checks. That breaks the AGENTS.md rule that trusted code
   enforces permissions.
3. **Some steps conflict with recorded decisions.** See "Conflicts needing an owner
   decision" below.

**Recommendation:** treat the blueprint as a feature checklist. Extend the existing
Gateway rather than add a router beside it.

## Blueprint item → current state

| Blueprint item | Current tree | Gap |
| --- | --- | --- |
| Role routing (fast / smart / utility) | `TaskKind` ranking in `gateway/task_planner.py`, admitted through `Gateway.eligible()` | No `UTILITY`/upkeep kind. No live provider is configured (Gateway is mock-tested only). |
| `private=True` never leaves the machine | `Privacy.CONFIDENTIAL/RESTRICTED` + `VERIFIED_LOCAL` destination; `UNKNOWN` never eligible | Equivalent exists. Keep it; don't add a parallel flag. |
| Streaming | `Gateway.stream()` + SSE normalisation | Not yet wired to a user-facing transport end to end. |
| Council mode | None | New. Must reuse `Gateway.complete` per member so privacy and budget checks still apply. |
| Context compaction | None | New. Summaries are model output, so treat them as untrusted data, never instructions. |
| Self-check of risky tool plans | `mcp_policy.review_gateway_proposals` (deterministic, pinned schemas); proposals stay inert | A model-based reviewer may add signal, but it **cannot replace** the deterministic policy or Trust approval. |
| `max router status` CLI | `lifecycle_cli`, `timeline_cli`; Desktop Command Deck shows model activity | Small addition: print `selection_metadata()` / budget view. |
| Memory: facts, episodes, raw logs | `memory.py`: owner-scoped, review-first (propose → owner review → retrieve), revisioned, audited, with expiry; optional pgvector in `semantic_memory.py` | No "episode" or "raw log" tiers yet. No keyword search. Backend is PostgreSQL, not SQLite (see conflicts). |
| Nightly "dream" job, owner shown changes | Review queue already forces owner approval of suggested memories | Needs a scheduler and a utility-lane summariser that only **proposes**. |
| Source and date on every memory | Records are revisioned with an audit trail | Check that provenance (source and observed-at) is exposed in `RetrievedMemory`. |
| Scheduler and event triggers | `connect.py` + `BOUNDED_PROACTIVITY.md`: typed events (CI failed, calendar upcoming, build finished) → non-executable drafts, capped at 50 | No scheduler or morning briefing. Inbound event sources are local bridge claims only. |
| Quiet hours and interruption budget | `presence/notifications.py`: `quiet_mode`, `FocusState.QUIET_TIME`, focus-aware delivery | No daily interruption budget counter. |
| "May act on pre-approved things" | Trust grants are **single-use and exact-action**; only `task.status` is executable | Standing pre-approval is a new trust model (see conflicts). |
| STT / wake word / TTS / barge-in | `voice/pipeline.py` (injected STT/TTS, sentence chunking, cancellation), `voice/barge_in.py`, `voice/local_activation.py` | No real engines wired (faster-whisper, Piper, wake-word model). Needs physical testing on the RTX 4060 machine. |
| Small local model for the private lane | `native_intelligence` is a tiny from-scratch research model, **not** usable as an assistant | Needs a reviewed local inference server registered as `VERIFIED_LOCAL`. |
| MCP read-only tools first | `mcp_discovery.py` (observe only), `mcp_policy.py` (risk tiers, pinned schemas), Trust audit | No MCP tool is executable yet. Each tool still needs a policy entry, an eval and an audit line, as the blueprint says. |
| Loopback dashboard + kill switch | Desktop Command Deck (M17) and native desktop (M21/M22) | No memory browser, pending-approval view or kill switch. |
| Keys in environment only | `.env.example`, `config.py` fail-closed | Already the rule. |
| Injection evals per tool | Gateway, policy and harness tests | Keep adding one per new tool. |

## Conflicts needing an owner decision

These need a decision record in `docs/DECISIONS.md` before code is written.

1. **SQLite + FTS5 vs PostgreSQL.** ADR-003 / P-010 / T-004 make PostgreSQL authoritative,
   and memory already lives there with optional pgvector. Options:
   (a) keep PostgreSQL and add `tsvector` full-text search to `MemoryStore` (recommended,
   no second datastore); (b) add SQLite only for a desktop-local raw-log tier.
2. **"Discord text MVP live."** P-019 says not to touch existing Discord bots, and Core is
   loopback-only by design. A new Discord bot is a new remote ingress with its own token,
   identity and injection surface. It needs its own threat-model entry, a separate bot or
   application, and explicit approval. It's not a step to fold into the router work.
3. **Pre-approved autonomous actions.** The blueprint lets Max act on "things you
   pre-approved". Trust only has single-use, exact-action grants today. Standing grants
   need scope, expiry, revocation, rate limits and audit, so this is a separate
   high-risk milestone.
4. **Free-tier providers that may train on prompts.** Any such route must be classified
   `Privacy.PUBLIC`-only in its `TrustedRoutePolicy`. That's a route-binding decision,
   not a code default.

## Proposed milestone order

Each row is one focused PR from the latest owner-merged `max`, following AGENTS.md.

| # | Milestone | Depends on |
| --- | --- | --- |
| A | Add `TaskKind.UTILITY` + router-status CLI over existing `selection_metadata()` | — |
| B | Configure one real provider route (owner-supplied key, env only) with live smoke check | owner key and decision 4 |
| C | Stream Gateway output to the desktop client | B |
| D | Memory full-text search + episode tier (decision 1) | — |
| E | Proposal-only "dream" summariser feeding the existing review queue | B, D |
| F | Scheduler + morning-briefing **draft** via `connect` + daily interruption budget | — |
| G | First read-only MCP tool made executable through Trust (policy, eval, audit) | — |
| H | Real STT/TTS/wake-word adapters behind existing voice interfaces (needs the owner's PC) | C |
| I | Context compaction; council / model self-check as extra signal | B |
| J | Standing approvals (decision 3), then write tools one at a time | G |
| — | Discord surface: only after decision 2 | — |

## Not done here

No code, provider configuration, credentials, Discord work or deployment. No tests were
run for this document. The local environment lacks the project dependencies (`fastapi`
isn't installed), so not even the existing suite was run as a baseline.
