# MAX — Architecture and Stage 2 Integration Report

## 1. MAX Core (what was built)

Stage 1 was never committed (repo contained only `README.md`), so Stage 2
first established a working core and then layered the open-source
integrations on top. Everything is Python 3.11+ with a single runtime
dependency (`pyyaml`).

```
src/max/
├── config.py           env/.env configuration, secret redaction
├── cli.py              max health|ask|skills|eval|improve|bench
├── bench.py            per-subprocess benchmark workers (A/B/C/D)
├── providers/
│   ├── base.py         canonical messages, ProviderError taxonomy
│   ├── openai_compat.py  FreeLLM/OpenAI-compatible adapter (timeouts,
│   │                    retries, 401/429/5xx/bad-JSON classification)
│   └── mock.py         scripted deterministic provider for tests/benches
├── core/
│   ├── agent.py        loop: provider → policy gate → dispatch → repeat
│   ├── tools.py        registry, PermissionPolicy, path sandbox, audit,
│   │                   prompt-injection markers
│   ├── builtins.py     read_file, list_files, write_file, memory_add,
│   │                   load_skill, run_command(exec, denied), http_request(net, denied)
│   ├── session.py      append-only JSONL transcripts (restart recovery)
│   ├── memory.py       bounded curated notes (MEMORY.md, hard char cap)
│   └── factory.py      wire everything from configuration
├── skills/registry.py  Agent-Skills validation + staging/eval/promote/rollback
├── mcp/client.py       stdio MCP client (initialize/list/call) + tool bridging
├── improve/
│   ├── policy.py       integrity-pinned trusted policy (policy.sha256)
│   ├── analysis.py     static rules + secret scan over diffs
│   └── pipeline.py     worktree → gates → report → promote/reject → rollback
└── eval/harness.py     14-scenario reproducible evaluation suite
```

### Control flow of one turn

```
user text
  → system prompt (base + skill index [names/descriptions only] + memory snapshot)
  → session history (JSONL)
  → provider.complete()            ── ProviderError → graceful reply + audit
  → tool_calls?
       no  → persist, return text
       yes → PermissionPolicy.check()        ← owned by config, NOT the model
               denied  → "REJECTED: …", audited, model told why
               allowed → handler (path-sandboxed) → output scanned for
                         injection markers → flagged + audited if matched
             → append tool results → loop (bounded by max_iters)
```

Key invariants (each has a test):

* the model never reaches exec/net tools by default (`category` default-deny),
* every allow/reject lands in `var/audit/audit.jsonl`,
* provider failures degrade to a friendly message, never a crash,
* file tools cannot leave the workspace (absolute paths, `..`, symlinks all
  rejected),
* prompt-injection content is flagged, and flagging never grants permissions.

## 2. Skill subsystem (Agent Skills standard)

* Format: `skills/<name>/SKILL.md` with YAML frontmatter per the
  [spec](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)
  (required `name`/`description`, name must equal directory name, optional
  `license`, `compatibility`, `metadata` string map, `allowed-tools`).
* Progressive disclosure: only names+descriptions enter the system prompt;
  the full body loads via the `load_skill` tool.
* MAX layers on top of the standard:
  * **provenance** — sha256 recorded on stage/promote; editing an active
    skill marks it `modified` and `load()` refuses it (drift detection),
  * **staging → evaluation → promote** — promotion requires a passing
    recorded evaluation and zero security flags,
  * **security flags** — prompt-injection language, `curl | sh`,
    hardcoded secrets, `allowed-tools` requesting exec → blocked from
    loading *and* promoting,
  * **rollback** — previous versions archived under
    `var/registry/versions/<name>/`; `max skills rollback <name>` restores
    the last known-good copy (verified by test, including the
    "nothing older to restore" error case).

Shipped demo skill: `skills/csv-summary` — discovers, loads, and runs
(`tests/test_skills.py::test_shipped_csv_summary_*`), registered through the
real stage → evaluate → promote cycle (status `active` in
`max skills list`).

## 3. Autonomous improvement workflow (Phase 5)

```
max improve run proposal.yaml
 1. Policy.load() — verifies config/policy.yaml against config/policy.sha256
    (tampering → PolicyIntegrityError, pipeline refuses to run)
 2. baseline test suite on current tree
 3. git worktree add -b max/auto/<id>  (isolated; never touches your tree)
 4. apply patch (unified diff)
 5. gates: patch-applies · compile · static-analysis · protected-paths ·
    deny-patterns · secret-scan · no-test-regression
 6. policy.decide():  reject | human | auto
 7. structured report → eval/reports/<id>.json (every gate + metrics)
 8. auto only if allowed: apply to the primary tree → re-test → commit
    `max(improve): <id> …`   (post-promote failure ⇒ automatic restore)
 9. max improve rollback <id>  → git revert (or archive restore fallback)
```

Policy facts that the model cannot change:

| Control | Where it lives |
|---|---|
| auto-promote categories (default `skill`, `docs`) | `config/policy.yaml` |
| sensitive categories (credentials/authorization/sandbox/network/deployment) → **always** human | same, checked before auto-allow |
| protected paths (`policy.yaml`, `policy.sha256`, `improve/policy.py`, `improve/pipeline.py`, `core/tools.py`, `.git/**`) | same, checked against the diff |
| deny patterns (`eval(`, `shell=True`, `verify=False`, `chmod 777`, `0.0.0.0`, …) | same, checked against added lines |
| integrity pin | `config/policy.sha256`; re-pin only via `max improve trust-policy` |
| existing test files modified | always human approval |

Honest limitation: with shell access an adversary could also re-pin the
hash. The structural guarantee is that the *model* has no tool to do so —
`trust-policy` is an operator command, not a tool in the agent's registry.
True separation needs approval from a different OS user/host (see
`docs/SECURITY.md`).

Demonstrations executed here: a docs change auto-promoted + committed +
reverted via rollback; a code change held for `max improve approve`; a
test-breaking change rejected before touching the tree; a policy-tampering
patch rejected; a sensitive-category patch not auto-promotable even when the
policy list was widened (test proves non-bypassability).

## 4. Phase 3 — OpenClaw integration report

**Setup used (isolated, loopback only, no channels, no personal accounts):**

```bash
npm install --prefix var/openclaw/install openclaw        # 619 MB
node var/openclaw/install/node_modules/openclaw/scripts/postinstall-bundled-plugins.mjs
export OPENCLAW_STATE_DIR=…/var/openclaw/state
export OPENCLAW_CONFIG_PATH=…/var/openclaw/config/openclaw.json
export OPENCLAW_WORKSPACE_DIR=…/var/openclaw/workspace
export MAX_MOCK_PROVIDER_KEY=…      # random, var/openclaw/provider.key (0600)
export OPENCLAW_GATEWAY_TOKEN=…     # random, var/openclaw/gateway.token (0600)
openclaw config patch --file var/openclaw/provider.patch.json   # custom model provider
openclaw config set gateway.mode local && openclaw config validate
bash var/openclaw/start-gateway.sh   # gateway run --port 18899 --auth token --bind loopback
```

**Verified behaviour** (details and numbers in `docs/BENCHMARKS.md`):
startup 15.8 s → `/health` live; unauthenticated `/tools/invoke` = 401,
wrong token = 401, `exec` = 404 (hard deny list), OpenAI surface = 404
(disabled by default); conversation through `openclaw agent -m … --json`
returned the mock model's reply; `skills list` 22/55 ready and
`skills info <name>` loads a skill; clean shutdown in 0.53 s; restart to
healthy again in 15.8 s.

**Interfaces MAX can use (documented by OpenClaw, ranked for MAX):**

| Interface | Direction | Status here |
|---|---|---|
| `openclaw mcp serve` (stdio MCP) → MAX's `max.mcp.MCPClient` | MAX reads conversations/events, sends messages | **verified**: handshake + 9 tools + `conversations_list` call |
| `POST /tools/invoke` + `Authorization: Bearer <gateway token>` | invoke one gateway tool | **verified** (200/401/404 matrix) |
| `openclaw agent -m "<text>" --json` CLI | one-shot turn | **verified** |
| Gateway WebSocket protocol v4 (token handshake) | full control plane | documented, not needed yet |
| OpenAI-compatible `POST /v1/chat/completions` | would let MAX use its *existing* `openai_compat` adapter against the gateway | **disabled by default (404)** — enable only if this becomes the primary path |

**Channel pairing / sandbox / permissions (docs + probes):** gateway binds
loopback by default and requires WS auth; unknown DM senders need pairing
(1-hour codes, 3 pending max); tool sandboxing is **off by default** and
moves to Docker/Podman when enabled; tool allow/deny policy is independent
of the HTTP hard-deny list. None of this was loosened for the experiment,
and no messaging accounts were connected.

**Recommendation:** keep OpenClaw optional. It passes its basic integration
test and gives MAX a channel gateway + MCP surface, but at ~1 GB idle RSS it
should stay off unless its channel features are actually needed; MAX Core
remains the primary assistant. A clean shutdown script and config live under
`var/openclaw/` (gitignored).

## 5. Configuration A/B/C/D definitions

* **A — MAX Core:** `MAX_ENABLE_SKILLS=0 MAX_ENABLE_IMPROVE=0 MAX_ENABLE_MCP=0`
* **B — A + OpenClaw:** A/B with the gateway running and `MAX_OPENCLAW_URL` set
* **C — lightweight alternative:** nanobot 0.3.5 via its own CLI
* **D — MAX full:** default configuration (everything enabled)

## 6. What runs where

| Service | Default | Required by |
|---|---|---|
| MAX (`max ask`, `max eval run`, …) | always on, in-process | core |
| OpenClaw gateway (loopback:18899) | **off** | optional, `MAX_OPENCLAW_URL` |
| nanobot CLI | **off** (external tool) | optional benchmark only |
| mock model provider (127.0.0.1:8756) | **off** | experiments only |

No service is exposed beyond loopback, and nothing auto-starts on boot.
