# Stage 2 — Candidate Repository Evaluation

Researched 2026-10-26 against live repositories, READMEs, docs and package
metadata. Sources are linked per entry. Nothing below is taken on the
projects' advertising alone: where I verified something hands-on in this
workspace it is marked **verified here**, where I relied on documentation it
is marked **docs-only**.

## Summary table

| Repository | License | Release status | Stack | Verdict for MAX |
|---|---|---|---|---|
| `openclaw/openclaw` | MIT | active fast train (2026.10.7 latest; 2026.9.34 extended-stable) | Node ≥24.22.1/≥26.12.1 | **Optional gateway**, integrated and tested (Phase 3) |
| `NousResearch/hermes-agent` | MIT | active, v0.22.5 (2026-10-22) | Python 3.11–3.14, large pinned dep tree | **Design donor only** — skills/memory boundaries reused, not installed |
| `NousResearch/hermes-agent-self-evolution` | MIT (separate Darwinian Evolver is AGPL-3.0) | no releases; active PRs | Python ≥3.10, `dspy`, `openai` | **Method donor only** — evaluation approach reused with executable gates |
| `agentskills/agentskills` | Apache-2.0 code, CC-BY-4.0 docs | spec + reference lib | Python reference CLI (`skills-ref`) | **Adopted as the format standard** for MAX skills |
| `HKUDS/nanobot` | MIT (package self-classified *Alpha*) | v0.3.5 | Python ≥3.11, `uv tool install nanobot-ai` | **Benchmarked as configuration C**; kept as fallback |

---

## 1. OpenClaw — `github.com/openclaw/openclaw`

Sources: [README](https://github.com/openclaw/openclaw/blob/main/README.md),
[Install](https://docs.openclaw.ai/install),
[Node compatibility](https://docs.openclaw.ai/install/node-compatibility),
[Protocol](https://docs.openclaw.ai/gateway/protocol),
[Network exposure](https://docs.openclaw.ai/gateway/security/network-exposure),
[Sandboxing](https://docs.openclaw.ai/gateway/sandboxing),
[Tools invoke API](https://docs.openclaw.ai/gateway/tools-invoke-http-api),
[advisories](https://github.com/openclaw/openclaw/security/advisories).

* **Capabilities:** self-hosted assistant *gateway* — sessions, tools, events,
  channel pairing (Telegram/WhatsApp/Discord/…), plugins, bundled skills,
  hosted or local model providers. State/credentials stay on the host.
* **Install:** `curl -fsSL https://openclaw.ai/install.sh | bash` or
  `npm install -g openclaw@latest --allow-scripts=openclaw`.
  **verified here:** `npm install --prefix ./install openclaw` → OpenClaw
  2026.10.5, **619 MB** on disk; npm's script allow-list had to be satisfied
  by running the package's `postinstall-bundled-plugins.mjs` manually.
  Note our host Node is v24.21.0, slightly below the documented `>=24.22.1`;
  the CLI ran correctly anyway (warning-level engines mismatch, not enforced).
* **Resource requirements:** docs give **no** gateway minimum; the smallest
  managed local-model recipe has an 8 GiB host floor (docs-only).
  **verified here:** gateway RSS **862 MB** right after start → **1.03 GB**
  at 37 s → **1.25 GB** after one agent turn, on top of a 619 MB install.
* **Custom model provider:** JSON config with `models.providers.<id>` +
  `baseUrl` + `apiKey: ${ENV}` + `api: "openai-completions"` — **verified
  here** against a local OpenAI-compatible mock (config `validate` passed and
  the agent turn reached the mock provider).
* **Integration surfaces:** Gateway WebSocket protocol v4; HTTP
  `POST /tools/invoke` (always on, bearer token); OpenAI-compatible
  `/v1/chat/completions` + `/v1/models` (**disabled by default** — 404
  verified); `openclaw mcp serve` stdio MCP server; `openclaw agent -m`
  CLI. All except the OpenAI surface were **verified here**.
* **Security:** loopback bind by default, WS auth required by default,
  channel pairing with 1-hour codes, **tool sandboxing off by default**
  (Docker/Podman backend when enabled), HTTP hard-deny list for
  `exec/spawn/shell/fs_write/...`, bearer token treated as full operator
  credential. Numerous advisories exist — examples: plugin-install RCE
  (fixed 2026.3.24), gateway URL-override SSRF (fixed 2026.2.14). Check the
  live advisory list before pinning.
* **Overlap with MAX:** agent loop, tool dispatch, skills, session storage,
  provider abstraction — OpenClaw implements all of them natively.
* **Incompatibilities:** Node ≥24.22.1 requirement vs host 24.21.0; huge
  memory footprint for an always-on 8 GB laptop; its skills are its own
  bundle, not the Agent Skills directory standard MAX uses.
* **Decision:** kept **optional** behind `MAX_OPENCLAW_URL`; MAX Core was
  not replaced. Integration results in `docs/ARCHITECTURE.md`.

## 2. Hermes Agent — `github.com/NousResearch/hermes-agent`

Sources: [README](https://github.com/NousResearch/hermes-agent),
[pyproject.toml](https://github.com/NousResearch/hermes-agent/blob/main/pyproject.toml),
[Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills),
[Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory),
[Agent loop](https://hermes-agent.nousresearch.com/docs/developer-guide/agent-loop),
[SECURITY.md](https://github.com/NousResearch/hermes-agent/blob/main/SECURITY.md).

* **Capabilities:** on-demand skills (`SKILL.md` + `references/`), curated
  memory (`MEMORY.md`/`USER.md`, hard char caps, frozen session snapshot) vs
  searchable episodic history (SQLite + FTS5), subagents, scheduled tasks,
  provider adapters around a canonical internal message model.
* **Deps:** core deliberately pinned, extensive; contributor setup wants
  Python 3.14, Node, git-lfs, a managed dev environment. Copying it in would
  dwarf MAX's dependency surface (MAX: `pyyaml` + `pytest`).
* **Overlap:** MAX's agent loop, tool registry, session store and skill
  index are the same architectural ideas — implemented independently here.
* **Resource needs:** no published footprint; a Python app of this size is
  plausibly hundreds of MB resident (docs-only, unverified).
* **Integration choice:** **do not copy source.** Reuse documented design:
  (a) skills as Markdown + progressive disclosure, (b) small curated memory
  separate from full history, (c) provider adapters behind one internal
  representation. All three are implemented in MAX.
* **Security:** their own policy says skills may execute Python at import
  time and that scanners are not a boundary — OS isolation is. MAX mirrors
  this: policy gates live outside the model, exec/net are default-deny.
* **Decision:** design donor, not installed. (Not on the required test list;
  installing it would add a large dep tree for no measured gain.)

## 3. Hermes Self-Evolution — `github.com/NousResearch/hermes-agent-self-evolution`

Sources: [README](https://github.com/NousResearch/hermes-agent-self-evolution),
[PLAN.md](https://github.com/NousResearch/hermes-agent-self-evolution/blob/main/PLAN.md),
[audit issue #33](https://github.com/NousResearch/hermes-agent-self-evolution/issues/33).

* **Approach:** baseline → candidate mutation (GEPA/DSPy) → held-out eval →
  gates (tests, size, semantic preservation, benchmark regressions) →
  human-reviewed branch/PR → rollback via `git revert`.
* **Critical caveat (their own audit, Apr 2026):** the skill-fitness path
  used keyword overlap instead of an LLM rubric, pytest/benchmark flags did
  not actually enforce gates, and PR generation was unimplemented — i.e.
  metrics looked credible while gates were dead.
* **Reuse:** the *method* (baseline-vs-candidate, structured report, explicit
  promote/reject/rollback, human approval for risky classes) — implemented in
  `src/max/improve/` with **executable** gates: every gate is a check entry
  in the JSON report, a test in `tests/test_improve.py` proves each gate can
  fail, and a policy hash prevents silent gate tampering.
* **Not reused:** DSPy/GEPA dependency (needs a real LLM + paid tokens),
  their unproven PR automation. The Darwinian Evolver engine is AGPL-3.0 —
  deliberately avoided (license contamination) — and marked deferred.
* **Decision:** method donor, not installed.

## 4. Agent Skills — `github.com/agentskills/agentskills`

Sources: [Specification](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx),
[README](https://github.com/agentskills/agentskills),
[skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref).

* **Format:** a directory with `SKILL.md` = YAML frontmatter + Markdown.
  Frontmatter fields: required `name` (1–64, lowercase/digits/hyphens, no
  leading/trailing/consecutive hyphens, must equal the directory name) and
  `description` (1–1024 chars); optional `license`, `compatibility` (≤500),
  `metadata` (string→string), `allowed-tools` (space-separated,
  experimental). **`version` is not a top-level field** — the spec puts it
  in `metadata` (e.g. `version: "1.0"`).
* **Discovery:** three stages — name+description for discovery, full
  `SKILL.md` on activation, other files on execution. Keep bodies <500 lines
  / ~5k tokens.
* **Tooling:** `skills-ref validate ./my-skill`, but the library self-labels
  "demonstration purposes, not for production".
* **Decision:** **adopted as the standard**; MAX implements its own
  validator (`src/max/skills/registry.py`, full field rules tested in
  `tests/test_skills.py`) instead of depending on the demo library, then
  layers provenance (sha256), staging, evaluation history, promote and
  rollback on top. `skills/csv-summary` conforms to the spec.

## 5. nanobot — `github.com/HKUDS/nanobot`

Sources: [README](https://github.com/HKUDS/nanobot),
[pyproject.toml](https://github.com/HKUDS/nanobot/blob/main/pyproject.toml),
[Architecture](https://github.com/HKUDS/nanobot/blob/main/docs/architecture.md),
[Providers](https://github.com/HKUDS/nanobot/blob/main/docs/providers.md).

* **Capabilities:** Python personal-agent framework — browser/terminal/chat
  UI, memory, scheduled tasks, shell + web tools, MCP integrations,
  multi-agent delegation, an OpenAI-compatible `/v1` server of its own, and
  an `onboard`/`provider`/`agent` CLI.
* **Install:** `uv tool install nanobot-ai` — **verified here**: v0.3.5,
  **311 MB**, `nanobot --version` in 731 ms. Python 3.11+ satisfied; no Node
  needed.
* **Provider:** `providers.<name>.apiKey`/`apiBase` + `modelPresets` in
  `config.json` — **verified here**: a one-shot non-interactive conversation
  against the local mock endpoint succeeded (exit 0, 2.09 s, **145 MB**
  peak RSS).
* **Skills:** its own mechanism (`nanobot-document` etc. in docs); the
  reviewed sources do **not** establish Agent-Skills-standard compatibility,
  so MAX skills would need porting.
* **Overlap:** conversation loop, tools, MCP, sessions — competes with MAX
  Core but offers no skill-evaluation or self-improvement pipeline.
* **Decision:** measured as **configuration C**. It is the credible
  lightweight fallback if MAX Core had to be replaced, but it lacks the
  Agent Skills format and the gated self-improvement loop, so MAX remains
  primary (see `docs/BENCHMARKS.md` for the numbers).

---

## Cross-cutting conclusions

1. **Nothing was cloned and adopted wholesale.** Only OpenClaw (required by
   the brief) and nanobot (required for comparison C) were installed, both
   into isolated, gitignored directories; hermes-agent and
   self-evolution were studied via documentation only.
2. **Integration beats copying** for all five: OpenClaw over
   HTTP+token/MCP/CLI, nanobot via its CLI, Agent Skills via file format,
   hermes/self-evolution via documented patterns.
3. **License posture:** MIT everywhere except Agent Skills (Apache-2.0/CC-BY)
   and the self-evolution project's *separate* AGPL-3.0 evolver, which is
   not used. MAX itself has no license file yet (owner decision pending).
4. **Biggest incompatibility found:** OpenClaw's memory footprint
   (≈1 GB idle) versus an always-on 8 GB laptop; nanobot is ~7× lighter per
   conversation but changes the skill ecosystem.
