# Setup, Operation and Recovery

Everything below was executed in this workspace; commands are copy-paste
ready from the repository root.

## 1. Reproducible setup (fresh clone)

```bash
# Python 3.11+ (this session used 3.12.3 via uv)
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'   # pyyaml + discord.py + pytest
# (equivalent: .venv/bin/pip install -e '.[dev]')

# configuration
cp .env.example .env          # optional; defaults are offline (mock provider)
# edit .env only if you have a real OpenAI-compatible endpoint + key

# policy integrity pin (must match config/policy.yaml)
.venv/bin/python -m max improve trust-policy   # already pinned in this repo

# verify
.venv/bin/python -m pytest                 # 167 unit/integration tests
.venv/bin/python -m max health             # status: ok
.venv/bin/python -m max eval run           # 14/14 scenarios
.venv/bin/python -m max discord check      # Discord readiness report
```

## 2. Everyday commands

```bash
.venv/bin/python -m max ask "hello"                 # one-shot chat (mock by default)
.venv/bin/python -m max ask "…" --session work --json
.venv/bin/python -m max discord check [--json]      # Discord config/intent/permission audit
.venv/bin/python -m max discord run                 # start the text bot (needs MAX_DISCORD_TOKEN)
.venv/bin/python -m max skills list                 # registry + status
.venv/bin/python -m max skills show csv-summary
.venv/bin/python -m max eval run [--filter 07] [--json]
.venv/bin/python -m max improve scan                # static/secret findings
.venv/bin/python -m max improve run proposals/fix.yaml --json
.venv/bin/python -m max improve list|show <id>|approve <id>|rollback <id>
.venv/bin/python -m max bench --configs A,D
.venv/bin/python -m max health --json               # machine-readable health
```

Exit codes: `0` healthy/pass, `1` failing/rejected — safe for cron/systemd
`OnFailure` hooks later.

## 3. Using a real provider (FreeLLM or any OpenAI-compatible API)

```bash
cat >> .env <<'EOF'
MAX_PROVIDER=openai
MAX_BASE_URL=<your verified endpoint base URL, e.g. https://host/v1>
MAX_MODEL=<model id your provider reports as available>
MAX_API_KEY_ENV=FREELLM_API_KEY
EOF
export FREELLM_API_KEY='…'      # from your secret store; never commit it
.venv/bin/python -m max health  # shows api_key_set: true (value never printed)
```

The adapter classifies 401/403 → `auth`, 429 → `rate_limited` (retried),
5xx/connection → `server_error` (retried), timeouts → `timeout`, bad
payloads → `bad_response`; the agent converts all of them into a friendly
reply and an audit entry, so the service never dies on a provider error.

> **Endpoint not verified in this environment:** no FreeLLM variables were
> present, so `MAX_BASE_URL`/`MAX_MODEL` are deliberately empty in
> `.env.example` rather than guessed. Fill them from your provider's docs.

## 4. Skills lifecycle

```bash
.venv/bin/python -m max skills stage path/to/candidate-dir   # validate + stage
.venv/bin/python -m max skills eval  <name> --passed --notes "why"
.venv/bin/python -m max skills promote <name>                # requires passing eval
.venv/bin/python -m max skills rollback <name>               # last known-good
```

Skills live in `skills/<name>/SKILL.md` (Agent Skills standard). Flagged
skills (injection language, `curl|sh`, exec requests, embedded secrets)
cannot be promoted or loaded.

## 5. Start / stop the optional services

**Mock model provider** (experiments only, loopback):

```bash
nohup python3 var/openclaw/mock_provider.py > var/nanobot/mock.log 2>&1 & echo $! > var/nanobot/mock.pid
kill "$(cat var/nanobot/mock.pid)"
```

**OpenClaw gateway** (optional; ~1 GB RSS — see docs/BENCHMARKS.md):

```bash
bash var/openclaw/start-gateway.sh        # prints gateway_pid, writes gateway.pid
curl -s http://127.0.0.1:18899/health     # {"ok":true,"status":"live"}
PGID=$(cut -d= -f2 var/openclaw/gateway.pid); kill -TERM -- -$PGID   # clean stop (~0.5 s)
```

Stop rule used in this project: kill only the PID/PGID recorded in the
pidfile — never a name pattern.

**nanobot** (config C, external tool):

```bash
"$HOME/.local/bin/nanobot" agent -m "hi" -c var/nanobot/config.json -w var/nanobot/ws --classic
```

## 6. Health checks and logs

| Check | Where |
|---|---|
| Service health | `max health --json` (`status`, provider check, skills, policy integrity, last eval) |
| Audit trail | `var/audit/audit.jsonl` (`tool_allowed`, `tool_rejected`, `injection_flagged`, `provider_error`) |
| Session transcripts | `var/sessions/*.jsonl` |
| Evaluation runs | `eval/runs/*.json` (gitignored) + summary printed by `max eval run` |
| Improvement reports | `eval/reports/<id>.json` (every gate, baseline vs candidate, verdict) |
| Benchmarks | `benchmarks/results/<ts>/results.json` |
| OpenClaw gateway log | `var/openclaw/gateway.log` |

## 7. Git checkpoints and recovery

History after this stage:

```
checkpoint 1  feat(max): Stage 2 core — providers, skills, gated self-improvement, evaluation suite
checkpoint 2  docs: architecture, benchmarks, candidate comparison, security, setup, roadmap
```

```bash
git log --oneline              # see checkpoints
git status                     # should be clean after a checkpoint
# undo the last checkpoint but keep files:
git reset --soft HEAD~1
# return the whole tree to the last checkpoint (discards uncommitted work!):
git reset --hard <sha>
# recover a change auto-promoted by MAX:
.venv/bin/python -m max improve list
.venv/bin/python -m max improve rollback <id>     # git revert of max(improve) commit
# recover a skill:
.venv/bin/python -m max skills rollback <name>
```

`.env`, `var/`, `.venv/`, `eval/runs/`, `eval/reports/`, `.max/`,
`benchmarks/results/` are gitignored, so a `git clean` will never be needed
for them and no credential can be committed by accident.

## 8. Optional vs required services

| Component | Required? |
|---|---|
| MAX CLI + mock provider (tests/evals) | **required** (everything works offline) |
| Real OpenAI-compatible endpoint | optional — needed only for real inference |
| OpenClaw gateway | optional — off by default, no channels connected |
| nanobot | optional — benchmark comparison only |
| MCP servers (`max.mcp.MCPClient`) | optional — tests use the bundled mock server |

Nothing starts on boot, nothing binds beyond loopback, no sensitive
integration (messaging accounts, tunnels, systemd services) was configured.

## 9. Optional capabilities (advanced integration)

Everything below is **off or mock by default**: MAX starts and chats without
any of it.

```bash
# optional extras — install only what you need
uv pip install --python .venv/bin/python -e '.[codesearch]'   # tree-sitter
uv pip install --python .venv/bin/python -e '.[stt]'          # faster-whisper
uv pip install --python .venv/bin/python -e '.[mcp]'          # official MCP SDK (not required)

# verify each capability without touching production
.venv/bin/python -m pytest -q                       # full suite
.venv/bin/python -m max health                      # config switches are in the payload
grep -c . .env                                     # copy .env.example first
```

| Switch | Default | Effect |
|---|---|---|
| `MAX_WORKER_ENABLE` / `MAX_WORKER_REPOS` / `MAX_WORKER_TIMEOUT_S` / `MAX_WORKER_NETWORK` | `0` / — / `120` / `0` | isolated coding worker (worktree + allowlisted commands) |
| `MAX_CODESEARCH_ENABLE` | `1` | tree-sitter symbol tools (regex fallback if not installed) |
| `MAX_MCP_SERVERS` | empty | JSON array of MCP servers; each disabled unless `"enabled":true`, no tool auto-enabled |
| `MAX_BROWSER_ENABLE` / `MAX_BROWSER_ALLOW_HOSTS` | `0` / — | guarded Playwright MCP research (read-only) |
| `MAX_STT_BACKEND` (+ `MAX_STT_*`) | `mock` | speech-to-text adapter; nothing downloaded at startup |

Troubleshooting an optional capability:

* tool returns `REJECTED` → its category is denied by the permission policy;
  add the tool name to `MAX_TOOLS_ALLOW` deliberately.
* tool returns `ERROR: ... disabled by configuration` → set the matching
  switch above; the message names the variable.
* `speech_status` reports `available: false` with the install command.
* worker refuses a repository → `MAX_WORKER_REPOS` must contain it.
