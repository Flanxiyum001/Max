# Security, Dependencies and Licenses

## 1. Threat model (what MAX defends against)

| Threat | Control | Test |
|---|---|---|
| Model calls a dangerous tool | `PermissionPolicy`: `exec`/`net` categories default-deny, explicit deny beats allow, policy lives in config — not in the model's context | `tests/test_tools.py`, eval 06 |
| Model escapes the workspace | path sandbox rejects absolute paths, `..`, symlinks (`resolve_within`) | `tests/test_tools.py::test_symlink_escape_rejected` |
| Prompt injection via file contents | injection markers in tool output → flagged + audited; **flagging never grants permissions** — the rejected tool stays rejected | eval 07, `tests/test_agent.py` |
| Prompt injection via skill instructions | registry security flags block loading **and** promotion of flagged skills | eval 08, `tests/test_skills.py` |
| Secret leakage | API keys read only from env, never in `redacted()`, error messages never echo headers, audit truncates args, `.env` gitignored, secret-scan on improvement diffs | `tests/test_config.py`, `tests/test_providers_openai.py` |
| Self-improvement weakens its own guardrails | `config/policy.yaml` integrity-pinned (`policy.sha256`); protected paths (policy, pipeline, permission code, `.git/**`) rejected; existing test edits always need human approval; sensitive categories can never auto-promote | `tests/test_improve.py` |
| Malicious/failed model provider crashes the service | typed `ProviderError` taxonomy, timeout + bounded retries, agent converts failures to a reply | `tests/test_providers_openai.py`, eval 11 |

## 2. Honest limitations

1. **Approval separation is structural, not cryptographic.** An operator (or
   anything with shell access as the same user) can re-pin the policy hash.
   The guarantee is that the *model* has no tool to do it. For real
   separation: run `max improve approve` as a different OS user, or on
   another host, and sign the policy file.
2. **Scanners are heuristics, not a boundary** (same position as Hermes'
   SECURITY.md): the injection patterns and secret regexes can be evaded;
   the load-bearing control is the permission policy plus the absence of an
   exec tool in the default profile.
3. **No sandbox for the agent process itself.** MAX runs with the user's
   privileges. Untrusted code execution (if ever enabled) must go through an
   OS-level sandbox (bubblewrap/systemd-nspawn), which is deferred.
4. **No real provider credentials were available or used.** No FreeLLM
   endpoint/key exists in this environment (verified: no `*API_KEY*`,
   `*LLM*`, `*MODEL*`, `*BASE_URL*` variables present). Nothing was invented;
   `.env.example` ships with empty values. If a key is later added, keep it
   in `.env` (gitignored) or a secret store, never in the repo.
5. **OpenClaw caveats:** tool sandboxing is off by default there; its
   bearer token is full-operator scope; advisories exist — re-check before
   pinning a version. MAX's experiment bound it to loopback with a random
   token and enabled no channels.
6. **`0.0.0.0` is on the deny-pattern list** and no service in this project
   listens beyond loopback; there is no auto-exposure, no systemd install, no
   tunnel.

## 3. Dependency inventory and licenses

### Runtime (installed for MAX)

| Package | Version | License | Used for |
|---|---|---|---|
| PyYAML | 6.0.3 | MIT | skill frontmatter, scenario/proposal/policy parsing |
| (Python stdlib) | 3.12.3 | PSF | everything else: urllib, sqlite-less JSONL storage, subprocess, http.server |

### Development

| Package | Version | License |
|---|---|---|
| pytest | 9.1.1 | MIT |
| (its deps: pluggy, iniconfig, packaging, pygments) | — | MIT |

### Optional / experimental (installed outside the project, gitignored)

| Component | Version | License | Size | Status |
|---|---|---|---|---|
| OpenClaw (+ npm deps) | 2026.10.5 | MIT | 619 MB | optional gateway, off by default |
| nanobot (`uv tool install nanobot-ai`) | 0.3.5 | MIT (package says *Alpha*) | 311 MB | benchmark config C only |

### Studied but **not** installed

| Component | License | Note |
|---|---|---|
| hermes-agent | MIT | design donor only; heavy pinned dep tree not justified |
| hermes-agent-self-evolution | MIT (Darwinian Evolver: **AGPL-3.0**) | method donor only; AGPL component deliberately avoided |
| agentskills (`skills-ref`) | Apache-2.0 (docs CC-BY-4.0) | spec adopted; reference CLI self-labelled "not for production", so MAX ships its own validator |

MAX itself has **no LICENSE file yet** — that is an owner decision (see
`docs/ROADMAP.md`), and no third-party source was copied into this repo.

## 4. Secret handling in this workspace

* `var/openclaw/provider.key`, `var/openclaw/gateway.token` — locally
  generated random values for the experiment, mode 0600, inside `var/`
  which is **gitignored**. They are not printed anywhere in this repo's docs.
* `max health --json` reports only `api_key_set: true/false`.
* Audit log truncates tool arguments to 200 chars.
* Nothing in `git status` or the commit contains credential material
  (verified before committing: `git diff` review + no `.env` file exists).

## 5. Network exposure audit

| Listener | Bind | Auth | Lifecycle |
|---|---|---|---|
| mock model provider :8756 | 127.0.0.1 | none (local, no secrets) | started/stopped per experiment |
| OpenClaw gateway :18899 | loopback (explicit `--bind loopback`) | token (random) | stopped after test |
| MAX | none (CLI only) | n/a | n/a |

No channel (Telegram/WhatsApp/Discord), no tunnel, no systemd service, no
public exposure was configured — as required by the brief.
