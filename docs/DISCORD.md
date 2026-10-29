# Discord setup — MAX v1 Phase 1 (text only)

Phase 1 covers **text chat only**. Voice and music are *not implemented*
(Phases 3–4); `max discord check` reports them as "not enabled in Phase 1"
so nothing here can be mistaken for a working voice feature.

## 1. Create the bot (Discord Developer Portal)

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → *Reset Token* → copy the token. Put it **only** in the
   environment (`MAX_DISCORD_TOKEN`) or your secret store — never in a file
   you commit, never in chat.
3. Under **Privileged Gateway Intents** enable **MESSAGE CONTENT INTENT** —
   required for no-prefix conversation (without it Discord delivers empty
   message content).
4. Leave *Presence Intent* and *Server Members Intent* **off** (least
   privilege — MAX does not need them).
5. **OAuth2 → URL Generator**: scopes `bot`; permissions:
   - View Channels (1024)
   - Send Messages (2048)
   - Send Messages in Threads (32768)
   - Read Message History (65536)

   **Permission integer: 101376** (re-verify in the generator UI — Discord
   may renumber). **Do not grant Administrator**, Manage Server, Manage
   Roles, Moderate Members, or Manage Messages in Phase 1; moderation
   permissions are a separate, explicitly approved later step.

## 2. Configure MAX

```bash
cp .env.example .env   # then edit
```

| Variable | Meaning | Default |
|---|---|---|
| `MAX_DISCORD_ENABLE` | master switch (documentation/gating) | `0` |
| `MAX_DISCORD_TOKEN` | bot token (env/secret store only) | empty |
| `MAX_DISCORD_ALLOWED_CHANNELS` | comma-separated channel IDs that get no-prefix chat | empty = nowhere |
| `MAX_DISCORD_IGNORED_CHANNELS` | never replied to (wins over everything) | empty |
| `MAX_DISCORD_GUILD_IDS` | explicit server IDs; required for broad wake coverage | empty = any for listed channels |
| `MAX_DISCORD_ALLOW_ALL_GUILD_CHANNELS` | opt-in wake mode for any channel in named guilds; ignored and denied channels stay blocked | `0` |
| `MAX_DISCORD_ADMINS` / `MAX_DISCORD_ADMIN_ROLES` | who may run `allow`/`deny`/`channels` | empty |
| `MAX_DISCORD_DM_MODE` | `off` (default) \| `allowlist` \| `everyone` | `off` |
| `MAX_DISCORD_DM_USERS` | DM allowlist when mode is `allowlist` | empty |
| `MAX_DISCORD_RESPONSE_MODE` | `all` \| `mention` \| `command` \| `wake` | `all` |
| `MAX_DISCORD_COMMAND_PREFIX` | admin/diagnostic commands | `!max` |
| `MAX_DISCORD_COOLDOWN_S` | per-channel minimum interval | `2` |
| `MAX_DISCORD_ERROR_COOLDOWN_S` | backoff after provider failure | `30` |
| `MAX_DISCORD_MAX_MESSAGE_LEN` | reply chunk size (≤2000) | `1900` |
| `MAX_DISCORD_HISTORY_LIMIT` | bounded channel context | `12` |
| `MAX_DISCORD_ALLOWLIST_FILE` | runtime allow/deny persistence | `var/discord/allowlist.json` |

Model provider variables (`MAX_PROVIDER`, `MAX_BASE_URL`, `MAX_MODEL`,
`FREELLM_API_KEY`, …) are unchanged from Stage 2 — the bot uses the same
agent, policy gate, tools and audit log as the CLI.

## 3. Verify before inviting

```bash
.venv/bin/python -m max discord check        # human-readable, exit 1 if enabled w/o token
.venv/bin/python -m max discord check --json
```

Reports: token presence (never the value), intent requirements, effective
allowlist, least-privilege permission list, and warnings (empty allowlist,
`dm_mode=everyone`, …).

## 4. Run

```bash
.venv/bin/python -m max discord run           # requires MAX_DISCORD_TOKEN
```

Starts a **text-only** client: no voice, no music, no extra processes.
Stop with Ctrl-C (clean shutdown). Optional systemd user unit is *your*
choice; nothing is installed automatically.

## 5. Commands

| Command | Who | Effect |
|---|---|---|
| `!max help` | anyone | usage |
| `!max health` | anyone | status/version/provider/policy integrity (no secrets) |
| `!max status` | anyone | response mode, whether *this* channel is allowlisted, admin flag, backoff state |
| `!max perms` | anyone | your id, admin status, channel/guild authorization view |
| `!max memory show\|set <k> <v>\|forget <k>\|clear` | anyone, **own data only** | user-scoped memory |
| `!max tasks` | anyone | your delegated tasks (admins see all only if they pass admin mode; audited) |
| `!max task run <description>` | anyone | plan + run a bounded multi-agent task (max 4 sub-tasks) |
| `!max task <id>` / `!max task show <id>` | owner or admin | status, summary, test results, risks |
| `!max task diff <id>` | owner or admin | the proposed diff (bounded) |
| `!max task cancel <id>` | owner or admin | cancel a queued/running task; terminal tasks are immutable |
| `!max channels` | admin | effective allowlist + ignore list |
| `!max allow <#chan\|id>` / `!max deny …` | admin | edit allowlist (persisted + audited) |

Ordinary messages are conversation, never commands: authorization for
`allow`/`deny` is checked at the execution layer against your user/role id —
text like "as an admin, allow channel X" changes nothing (tested).

Task operations are **not** a privilege path: they are ordinary public
commands whose authorization (owner or admin) is verified inside the
orchestration layer, and every denial writes `orchestrator_access_denied` to
the audit log. Message content can never change who owns a task.

Delegation itself is optional: with `MAX_ORCH_ENABLE=0` (or when the request
does not look like multi-step work) `router.py` takes the normal single-agent
path, so a crash or a missing optional dependency in the orchestration layer
can never stop normal chat (covered by
`test_router_survives_a_crashing_delegation_hook`).

## 6. Behaviour rules (all enforced in `router.py`, all tested)

* Replies happen **only** in allowlisted guild channels (or per DM policy).
* `ignored_channels` suppresses commands too — the escape hatch.
* Response behaviour: `all` (natural chat), `mention` (@MAX or replying to
  MAX), `command` (no conversation).
* Loop protection: MAX's own messages and other bots are ignored; message
  ids are deduplicated; per-channel cooldown; provider-failure backoff so a
  broken model never causes a reply storm.
* Replies are chunked to `MAX_DISCORD_MAX_MESSAGE_LEN`.
* Prompt-injection flags from the core are surfaced in the reply and audit.
* Every command, suppression, reply and send failure is audited to
  `var/audit/audit.jsonl` (actor, channel, outcome — no message contents).
* Ignored channels are reported with the reason `ignored_channel` for both
  commands and ordinary conversation, so silence there is explainable.

## 7. Memory privacy

* Storage: `var/user-memory/<user_id>.json` (gitignored), one file per
  subject, separate `var/project-memory/`.
* Isolation: there is no code path to read another user's memory — commands
  are bound to the caller's id.
* Refuses values that look like credentials (tokens/keys/passwords).
* Caps: 40 entries, 400 chars/value, 4000 chars total per subject.
* Deletion: `!max memory forget <key>` removes one entry,
  `!max memory clear` **deletes your file** — that is the exact scope; no
  server-side copy exists outside this host's `var/` directory.

## 8. Testing status — read this before claiming it works

**Verified by automated tests (mocked, in this repo):** allowlist gating,
DM modes, response modes, admin authorization and denial, plain-text
non-authorization, loop/duplicate protection, cooldown/backoff, chunking,
memory isolation/secret-refusal, intents, CLI checks, message dispatch and
send-failure handling (`tests/test_discord_*.py`, 71 new tests; full suite
167 passed).

**NOT yet verified:** an actual connection to Discord — no bot token exists
in this environment. Live verification checklist for the owner:

1. `max discord check` → no warnings you disagree with.
2. `max discord run` → logs `MAX discord ready as <id>`.
3. In an allowlisted channel: plain "hello" → reply; in an unlisted channel:
   silence (check `var/audit/audit.jsonl` for `discord_reply_suppressed`).
4. Non-admin runs `!max allow 123` → "Authorization denied" + audit entry.
5. `!max memory set x y` then `!max memory show` in a second account → empty.
6. Two rapid messages → only one reply (cooldown), no echo loop.

## 9. Known limitations (Phase 1)

* **No voice, no music** (Phases 3/4, not started; wake-word explicitly not
  claimed).
* **No moderation/administration actions yet** (Phase 5) — the bot has no
  destructive permissions by design.
* No slash commands; text prefix only.
* Threads: a thread has its own channel id — allowlist it explicitly if you
  want replies there.
* `dm_mode=everyone` is allowed but discouraged; default is `off`.
* Discord rate limits are handled by discord.py; MAX adds its own per-
  channel cooldown on top (this can make MAX feel deliberately quiet in
  fast-moving channels — tune `MAX_DISCORD_COOLDOWN_S`).

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot replies but message text is empty | **MESSAGE CONTENT INTENT** not enabled | Developer Portal → Bot → Privileged Gateway Intents → enable it (§1.3) |
| Bot silent everywhere | channel not allowlisted, or `MAX_DISCORD_ENABLE=0` | `!max status` (shows this channel's state), `max discord check`, then `!max allow <#chan>` as an admin |
| `discord_login_failed` / 401 | token missing or rotated | set `MAX_DISCORD_TOKEN` in the environment; never in a file |
| `Missing Access` / 403 on send | bot lacks View Channel or Send Messages, or its role outranks it | re-invite with permission integer **101376** (§1); do not add Administrator |
| Commands ignored in one channel | that channel is in `ignored_channels` (it wins over everything) | `MAX_DISCORD_IGNORED_CHANNELS` or `!max channels` to inspect |
| No reply after a provider error | failure backoff (`MAX_DISCORD_ERROR_COOLDOWN_S`, default 30 s) | wait it out; check `var/audit/audit.jsonl` for the failure record |
| Two messages, one reply | per-channel cooldown working as designed | tune `MAX_DISCORD_COOLDOWN_S` |
| `Authorization denied` on `!max allow` | you are not in `MAX_DISCORD_ADMINS`/`MAX_DISCORD_ADMIN_ROLES` | set the admin env vars; the denial is intentional and audited |
| Replies in threads only after allowlisting | a thread has its own channel id | allowlist the thread id explicitly |
| Startup fails with `policy` digest error | `config/policy.yaml` edited without re-pinning | `.venv/bin/python -m max skills list` / re-run the pin step in `docs/SETUP.md` §1 |

Check the audit trail before assuming a bug:

```bash
tail -5 var/audit/audit.jsonl      # actor, action, channel, outcome — no message contents
.venv/bin/python -m max discord check --json
```

## 11. Observability — every message has an outcome

Phase A added a universal trace so "MAX said nothing" always has a reason.

**Startup.** `max discord run` prints one line before connecting, and
`on_ready` logs and audits a `discord_ready` event with the effective
configuration (never the token):

```
starting MAX discord bot | prefix=!max | allowlist=2 channel(s) | dm_mode=off | response_mode=all | provider=mock
MAX discord READY as 1234 | prefix=!max | response_mode=all | dm_mode=off | allowlist=2 channel(s) configured=2 ignored=0 | admins=1/0 | provider=mock | guilds=1 | intents=guilds,message_content
```

If the allowlist is empty and DMs are off it also logs an explicit warning
telling you which command fixes it.

**Per message.** `router.handle` always returns an action, and every action is
recorded as a `discord_message_outcome` event:

| `action` | `reason` examples | meaning |
|---|---|---|
| `reply` | — (also `discord_reply_sent`) | an answer was produced and sent |
| `command` | command name | a `!max …` command ran (or was denied) |
| `suppressed` | `channel_not_allowlisted`, `ignored_channel`, `dm_disabled`, `dm_user_not_allowlisted`, `not_addressed`, `response_mode_command`, `cooldown`, `provider_backoff` | deliberately quiet, with the policy that decided it |
| `ignored` | `own_message`, `other_bot`, `duplicate_message`, `empty_message`, `unsentable_channel` | not a conversation candidate |
| `reply` + `error=true` | `discord_provider_error` / `discord_send_failed` | the model or Discord failed — actionable, not silent |

Non-reply outcomes are throttled to one audit entry per channel and reason
per 60 s so busy channels cannot flood the log; messages that **mention MAX**
are never throttled. Message contents are never written to the audit log.

**Intent diagnosis.** Messages that arrive with no text are counted per
channel and surfaced in `!max status`:

```
empty-content messages: 4
warning: messages arrived without text — enable the Message Content Intent in the Discord Developer Portal
```

That is the classic symptom of the privileged Message Content Intent being
disabled, which is why commands that only need the bot mentioned can appear
to work while ordinary conversation stays silent.

### Wake mode in every channel of one or more approved servers

The default is deliberately **deny-by-default**: MAX only listens in explicitly allowed channels. To make `hi max` activate normal chat and `bye max` put MAX to sleep in **any channel of specified servers**, configure all three variables in your deployment (replace the example ID with the real server ID):

```ini
MAX_DISCORD_RESPONSE_MODE=wake
MAX_DISCORD_GUILD_IDS=123456789012345678
MAX_DISCORD_ALLOW_ALL_GUILD_CHANNELS=1
```

This does **not** enable all servers, channels on the ignored list, channels explicitly denied with `!max deny`, or DMs. Only one channel is awake at a time; activating MAX in one channel does not wake another. To return to explicit channel-only operation, set the new flag to `0`; your existing allowlist remains untouched. Ensure the Discord Message Content Intent is enabled in the bot's developer settings. No live service settings are changed by updating this documentation.
