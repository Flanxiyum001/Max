# Configuration — canonical names, defaults, and who reads them

**Rule: one setting, one name.** Every variable MAX reads is listed here
exactly as the source reads it (`grep -rhoE '"MAX_[A-Z0-9_]+"' src/`), with the
module that consumes it. There are no aliases and no deprecated spellings;
nothing in this table is a documentation invention.

Precedence: **process environment first, `.env` only fills gaps**
(`src/max/config.py::_apply_dotenv`). Copy `.env.example` to `.env` (it is
gitignored) and edit there. Secrets are never printed, never logged, never
part of `max health`, and `.dockerignore` excludes `.env` from the image.

## 1. Provider and agent core — `src/max/config.py`

| Variable | Default | Meaning |
|---|---|---|
| `MAX_PROVIDER` | `mock` | `mock` (offline, used by tests/CI) or `openai` (any OpenAI-compatible endpoint) |
| `MAX_BASE_URL` | empty | Base URL when `MAX_PROVIDER=openai`, e.g. `https://host/v1` |
| `MAX_MODEL` | empty | Model id reported by the provider |
| `MAX_API_KEY_ENV` | `FREELLM_API_KEY` | **Name** of the env var holding the key — never the key itself |
| `MAX_API_KEY` | unset | Optional direct key; prefer `MAX_API_KEY_ENV` + a secret store |
| `MAX_TIMEOUT_S` / `MAX_RETRIES` | `60` / `1` | Provider request timeout and retry budget |
| `MAX_MAX_ITERS` | `8` | Agent loop step ceiling |
| `MAX_HISTORY_CHAR_BUDGET` | `24000` | Prior conversation character limit, full history still stored |
| `MAX_TOOLS_ALLOW` / `MAX_TOOLS_DENY` | empty | Explicit tool allow/deny lists (`a,b,c`); empty = category policy |
| `MAX_DATA_DIR` | `var` | Sessions, memory, audit log, task store (relative to repo root) |
| `MAX_WORKSPACE` | `workspace` | Root every file tool may read/write |
| `MAX_SKILLS_DIRS` | `skills` | Colon-separated active skill directories |
| `MAX_ENABLE_SKILLS` | `1` | Master switch for the skill registry |
| `MAX_AUTOPROMOTE` | `skill,docs` | Categories the self-improvement loop may auto-promote; sensitive categories can never be auto-promoted |
| `MAX_OPENCLAW_URL` / `MAX_OPENCLAW_TOKEN` | unset | Loopback gateway used only by `max bench --config B` |

## 2. Discord — `src/max/discord/config.py`

| Variable | Default | Meaning |
|---|---|---|
| `MAX_DISCORD_ENABLE` | `0` | **Advisory** master switch — see §5 |
| `MAX_DISCORD_TOKEN` | unset | Bot token (env/secret store only) |
| `MAX_DISCORD_ALLOWED_CHANNELS` | empty | No-prefix conversation channels |
| `MAX_DISCORD_IGNORED_CHANNELS` | empty | Never replied to; beats the allowlist |
| `MAX_DISCORD_GUILD_IDS` | empty | Guild allowlist; empty = any guild containing an allowlisted channel |
| `MAX_DISCORD_ADMINS` / `MAX_DISCORD_ADMIN_ROLES` | empty | Who may run `allow`/`deny`/`channels` |
| `MAX_DISCORD_DM_MODE` | `off` | `off` \| `allowlist` \| `everyone` |
| `MAX_DISCORD_DM_USERS` | empty | Used when `dm_mode=allowlist` |
| `MAX_DISCORD_RESPONSE_MODE` | `all` | `all` \| `mention` \| `command` \| `wake` |
| `MAX_DISCORD_COMMAND_PREFIX` | `!max` | Command prefix |
| `MAX_DISCORD_COOLDOWN_S` | `2` | Per-channel conversation cooldown |
| `MAX_DISCORD_ERROR_COOLDOWN_S` | `30` | Provider-error backoff base |
| `MAX_DISCORD_MAX_MESSAGE_LEN` | `1900` | Reply chunking limit (Discord caps at 2000) |
| `MAX_DISCORD_HISTORY_LIMIT` | `12` | Messages kept per session |
| `MAX_DISCORD_ALLOWLIST_FILE` | `var/discord/allowlist.json` | Persisted allow/deny edits |

## 3. Capabilities (all optional — MAX starts without any of them)

| Variable | Default | Meaning |
|---|---|---|
| `MAX_WORKER_ENABLE` | `0` | Coding worker (isolated git worktree) |
| `MAX_WORKER_REPOS` | empty | Allowlisted repositories; empty = this repository |
| `MAX_WORKER_TIMEOUT_S` / `MAX_WORKER_NETWORK` | `120` / `0` | Command timeout; network denied by default |
| `MAX_CODESEARCH_ENABLE` | `1` | Tree-sitter/regex repository intelligence |
| `MAX_MCP_SERVERS` | empty | JSON array of MCP servers; no tool is auto-enabled |
| `MAX_BROWSER_ENABLE` / `MAX_BROWSER_ALLOW_HOSTS` | `0` / empty | Playwright MCP research, read-only by default |
| `MAX_DESKTOP_ENABLE` | `0` | Local desktop control (screen, mouse/keyboard, terminal) via `max.desktop.server` |
| `MAX_DESKTOP_TIMEOUT_S` | `30` | Per-command wall clock in the desktop server (hard cap on every child) |
| `MAX_DESKTOP_APPROVAL_TTL_S` | `600` | How long a terminal approval request stays grantable |
| `MAX_DESKTOP_GUI_TTL_S` / `MAX_DESKTOP_GUI_ACTIONS` | `300` / `50` | One granted input session: lifetime and event count |
| `MAX_STT_BACKEND` | `mock` | `mock` \| `faster-whisper` (`pip install -e '.[stt]'`) |
| `MAX_STT_MODEL_SIZE` / `_COMPUTE_TYPE` / `_DEVICE` / `_LANGUAGE` | `tiny`/`int8`/`cpu`/auto | faster-whisper settings |
| `MAX_STT_TIMEOUT_S` / `MAX_STT_MAX_DURATION_S` / `MAX_STT_MODEL_DIR` / `MAX_STT_LOCAL_FILES_ONLY` | `30`/`300`/empty/`1` | Limits; `local_files_only=1` downloads nothing at startup |
| `MAX_ORCH_ENABLE` / `MAX_ORCH_AUTO` | `1` / `1` | Supervised multi-agent layer and its delegation decision |
| `MAX_ORCH_MAX_WORKERS` / `MAX_ORCH_DEPTH` | `2` / `1` | Concurrency (hard cap 3) and child depth |
| `MAX_ORCH_TASK_TIMEOUT_S` / `MAX_ORCH_TOOL_BUDGET` | `180` / `120` | Wall clock and shared tool-call budget |
| `MAX_ORCH_EXEC` | `0` | Guarded command runner for coder/tester |
| `MAX_ORCH_KEEP_WORKSPACES` | `0` | Keep task worktrees for inspection |
| `MAX_ORCH_CODER_BACKEND` | `max` | `max` \| `openhands` |
| `MAX_ORCH_OH_MODEL` / `_OH_BASE_URL` / `_OH_KEY_ENV` / `_OH_ITERS` | empty | OpenHands adapter (key is a *name*, never a value) |

## 4. Evaluation-only toggles — `src/max/eval/harness.py`, `src/max/bench.py`

| Variable | Meaning |
|---|---|
| `MAX_ENABLE_IMPROVE` | Include improvement-scan scenarios in `max eval run` |
| `MAX_ENABLE_MCP` | Include MCP scenarios in `max eval run` |

These three names (`MAX_ENABLE_IMPROVE`, `MAX_ENABLE_MCP`, `MAX_API_KEY`)
were read by the source but missing from `.env.example`; they are documented
there as of this revision.

## 5. Semantics worth knowing

- **`MAX_DISCORD_ENABLE` is advisory.** `max discord run` starts whenever a
  token is present and prints a warning when the flag is `0` (backwards
  compatible: existing deployments keep working). `max discord check` reports
  both states and exits `1` only for a genuinely broken config
  (`enable=1` with no token). The reported reason is audited as
  `discord_enable_flag_off`.
- **Every routed Discord message is audited** as `discord_message_outcome`
  with `action` (`reply`/`command`/`suppressed`/`ignored`), `reason`, channel,
  guild and command name — never message contents. Non-reply outcomes are
  rate-limited to one entry per channel and reason per 60 s, except messages
  that mention MAX.
- **`empty_message` is a diagnosis, not a shrug.** Counters feed
  `!max status`, which points at the Message Content Intent when messages
  arrive with no text.
- **Desktop control is opt-in and never self-authorising.**
  `MAX_DESKTOP_ENABLE=0` leaves the three `desktop_*` tools in the
  default-deny `exec` category. Setting it to `1` registers the built-in MCP
  server **and** puts those three names on the policy allowlist; a
  `MAX_TOOLS_DENY` entry still wins because deny is checked first. Enabling it
  does *not* authorise any command: read-only commands run, everything else
  needs `max desktop approve <id>` at the console. The server process receives
  only `PATH`/`LANG` from the MCP manager, so no token or key can reach it —
  see `docs/SECURITY.md` §8.

## 6. Docker and Railway

| Where | Setting |
|---|---|
| `Dockerfile` | `ENV MAX_DATA_DIR=/app/var` (absolute; source also accepts relative paths resolved against the repo root, which is `/app` in the image) |
| `Dockerfile` | `CMD python -m max discord run`; `railway.toml` repeats it as `startCommand` — two spellings of the same command, kept in sync by hand |
| Image contents | `.dockerignore` excludes `.env`, `.env.*`, `var/`, `workspace/`, `tests/`, `docs/` — **no secrets and no runtime state enter the image** |
| Runtime secrets | `MAX_DISCORD_TOKEN`, provider keys and channel ids must be set in the platform's variable UI (Railway → service → Variables), never in Git |

## 7. Names that are *not* MAX's

`MAX_MOCK_PROVIDER_KEY`, `OPENCLAW_*`, `OPENAI_API_KEY` and similar appear in
docs or third-party tooling; MAX never reads them. `FREELLM_API_KEY` is only a
*default value* of `MAX_API_KEY_ENV`, i.e. MAX reads whatever variable name is
configured there.

## 8. Validation and diagnostics

```bash
.venv/bin/python -m max discord check        # config, intents, permissions, warnings; exit 1 if misconfigured
.venv/bin/python -m max health --json        # status: ok, provider_check, skills, policy (no secrets)
.venv/bin/python -m max desktop status       # desktop config, input backend, pending approvals
.venv/bin/python -m max eval run             # 14/14 scenarios
```
