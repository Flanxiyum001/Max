# Deployment proposal — review required, nothing deployed automatically

**Current state (verified, not assumed):** the repository contains a
deployment *definition* but no deployment made by this session. A
repo-wide search finds `.github/workflows/ci.yml`, `Dockerfile`,
`.dockerignore` and `railway.toml` (the latter three added by PR #2,
commit `e88267f`, merged to `main` on 27 Oct 2026) — and no `Procfile`,
`nixpacks.toml` or `fly.toml`. The image builds the package and runs
`python -m max discord run` (Discord needs no inbound HTTP port).

**No Railway service, volume, environment variable or production
setting was created, read or modified by this session** — the connected
Railway integration cannot enumerate projects (it needs a workspace id
that is not available here, and its GitHub-repo listing currently fails
server-side), so no resource was touched. No secrets exist in Git. The
Docker image itself has **not** been built here (no Docker daemon in
this environment); `pip install .` from the same `pyproject.toml`
succeeds, and every path the Dockerfile copies exists in the tree.

That is intentional: MAX v1 deliverable 10 requires a *proposal*, and
Phase 6 forbids deploying without explicit approval.

## 1. What CI already guarantees (green)

`.github/workflows/ci.yml` runs on every push and pull request:

| Step | Command | Result |
|---|---|---|
| Install | `pip install -e ".[dev]"` | ok |
| Tests | `python -m pytest` | **299 passed** |
| Static scan | `python -m max improve scan` | **0 findings** |
| Health | `python -m max health --json` | `status: ok`, `policy_ok: true` |
| Evaluation | `python -m max eval run --label ci` | **14/14 (100%)** |
| Skills | `python -m max skills list --json` | 1 skill, 0 flagged |

Observed runs: **12 total, all `success`, zero failures in the
repository's run history** — most recently #10 (`main` @ `e88267f`),
#11 and #12 (`v2/capabilities` @ `1215745` and `9827173`), 25–40 s
each. CI uses the mock provider, so it needs no secrets and cannot
spend provider credits.

CI proves the code is sound. It does **not** prove a deployment works:
there is no deploy job, and none should be added without approval.

## 2. Option A — always-on Linux laptop (recommended)

Best fit for the stated constraints (~8 GB RAM, always-on, privacy, no
public exposure). MAX runs as a systemd *user* unit; no root, no
inbound ports.

```ini
# ~/.config/systemd/user/max-discord.service  (you create this)
[Unit]
Description=MAX Discord text bot
After=network-online.target

[Service]
WorkingDirectory=%h/Max
ExecStart=%h/Max/.venv/bin/python -m max discord run
Restart=on-failure
RestartSec=5
# EnvironmentFile must be chmod 600 and outside the repo
EnvironmentFile=%h/Max/.env

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now max-discord.service
systemctl --user status max-discord.service
journalctl --user -u max-discord.service -f      # logs (no secrets printed)
.venv/bin/python -m max health                    # readiness check
```

Resource expectation: the measured idle RSS for the core is ~25 MB and
peak ~28 MB under full evaluation (see `docs/BENCHMARKS.md`); discord.py
adds a modest constant. This is well inside 8 GB alongside everything
else. Stop with `systemctl --user stop max-discord.service`.

## 3. Option B — Railway (optional, requires your action first)

`railway.toml` now defines a `DOCKERFILE` build, so the repository is
ready for a Railway service — but whether such a service exists is
unknown from here: the connected Railway integration could not enumerate
your projects (it requires a workspace id that is not available to this
session, and its GitHub-repo listing returns a GraphQL validation error),
therefore **no Railway resource was read, created or changed.**

If you want this option, the shape would be:

1. Create (or point me at) a Railway service connected to
   `Flanxiyum001/Max`; Railway picks up `railway.toml` + `Dockerfile`.
2. Build/deploy definition: Dockerfile `CMD ["python", "-m", "max",
   "discord run"]` (mirrored by `startCommand` in `railway.toml`).
3. Set variables in Railway's UI (never in Git): `MAX_DISCORD_TOKEN`,
   `MAX_DISCORD_ENABLE=1`, `MAX_DISCORD_ALLOWED_CHANNELS`,
   `MAX_DISCORD_ADMINS`, plus provider variables (`MAX_PROVIDER`,
   `MAX_BASE_URL`, `MAX_MODEL`, `FREELLM_API_KEY`) if not using the mock.
4. Add a persistent volume only if you want `var/` (memory, audit log,
   allowlist) to survive redeploys — that is a storage change you must
   approve; without it, memory resets on every deploy.
5. Deploy, then run the health check and the live checklist in
   `docs/DISCORD.md` §8 against the running instance.

Trade-offs: a public host makes the bot reachable from anywhere (the
router still enforces allowlist + admin checks, and there is no inbound
port beyond Discord's outbound WebSocket), provider secrets live in a
third party, and `var/` durability costs a volume. The laptop keeps all
of that local.

## 4. Approval gate (what I will wait for)

Before any deployment of any kind:

- [ ] You name the target (laptop systemd / Railway / other) and say "deploy".
- [ ] Secrets are supplied through the target's secret store — never
      committed, never pasted into chat.
- [ ] The intended branch is `main` at the commit you expect.
- [ ] You confirm whether `var/` persistence is wanted (volume decision).
- [ ] Rollback step is understood (below).

Until every box is ticked, nothing is deployed.

## 5. Rollback

```bash
# code: revert a single change, or check out the last good commit
git revert <sha>            # or: git switch <last-good-sha> -b hotfix
git push origin <branch>    # CI re-runs; only after your approval

# skills: staged skill changes roll back without touching Git
.venv/bin/python -m max skills rollback <skill>

# runtime config: delete the overlay to fall back to .env values
rm var/discord/allowlist.json

# laptop service
systemctl --user stop max-discord.service
```

If a deployment is ever live, its platform rollback (previous deployment
revision) is the first lever; Git revert is the durable one.
