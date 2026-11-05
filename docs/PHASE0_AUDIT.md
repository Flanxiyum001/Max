# MAX Phase 0 requirements audit — post-PR #7

**Date:** 2026-11-05. **Baseline:** `max` at `72f67b1ec6a737c813f4ca633457a9ca39223b94`; branch `docs/phase0-requirements-audit`. Documentation-only audit; no deployments, credentials, account connections, real database changes or merges.

## Executive decision

**Architecture is substantially documented, but not unconditionally closed.** It is adequate to scope another **local, read-only** implementation PR after owner review. It is **not** ready for public remote access, autonomous tools, production provider traffic, sensitive documents, payments, calls or device control. Use [the 53-row owner-requirements matrix](REQUIREMENT_TRACEABILITY.md) and [exit criteria](PHASE0_EXIT_CRITERIA.md) instead of declaring all design documents "implemented."

## Methods, evidence and limitations

- Verified GitHub default branch `max` and recursive Git tree (**79 tracked files**) at the baseline SHA. The GitHub connection does not expose a checked-out working tree: **local staged/untracked changes were not inspected**. All changes on this branch use conditional blob-SHA updates, never commits directly to `max`.
- Read actual per-file GitHub PR patches for **#1 (16 files), #2 (14 files), #3 (23 files)**, their merge metadata, and their current resulting files. Inspected root identity/agent documents, docs, application modules, configurations, migrations, CI and regression tests. A PR merge alone is not a security test.
- [Historical successful PR #7 CI](https://github.com/Flanxiyum001/Max/actions/runs/41995096088): **125 passed on Python 3.11 and 125 on 3.13**, with ephemeral PostgreSQL 17, Ruff, strict mypy and pip-audit. One non-fatal upstream warning on each. **This audit did not rerun runtime tests**, test devices, run a MAX process or access an owner's account. Do not extrapolate CI to real-world provider/production guarantees.
- Cross-checked the uploaded **CODEX MASTER ASSIGNMENT — MAX PHASE 0**, current audit brief, and [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md). Other private owner specs/conversations are not confirmed line-by-line within this repository and remain provenance limitations.

## PR #1–#3: what the actual diffs contain

| PR | Merged scope from per-file patches | Proven outcome | Not established |
| --- | --- | --- | --- |
| [#1](https://github.com/Flanxiyum001/Max/pull/1) Foundation baseline | 16 changes: ten MAX persona/operating handbook documents, README and initial requirements/architecture | First-party identity, safety, evolution, finance and skills principles | Runtime enforcement |
| [#2](https://github.com/Flanxiyum001/Max/pull/2) Personal foundation | 14 changes: `AGENTS.md`, `ARCHITECTURE.md`, gateway/skills/agent/device/voice concepts and PR template | Codex-vs-MAX separation, modular design and owner manual-merge gate | Live clients, runtime and device proof |
| [#3](https://github.com/Flanxiyum001/Max/pull/3) Phase 0B research | 23 changes: threat and data models, OpenClaw, FreeLLMAPI research, India communications, diagrams, traceability and validation report | 19-document architecture coverage and specific unresolved choices | Exact FreeLLMAPI identity, live accounts, external security tests, rendered Mermaid diagrams |

**Subsequent implementation #4–#7 is merged.** This is not documentation-only MAX anymore: see src/max_core, Alembic revisions and tests. The old [REPO_INVENTORY.md](REPO_INVENTORY.md) code-absence finding describes an earlier snapshot, not the present tree.

## Current implementation stage

| Milestone | Designed | Implemented in source | Automated evidence | Real environment |
| --- | --- | --- | --- | --- |
| Identity, specialist agents | Yes | Handbook; **no planner/specialist runtime** | Document review, not agent test | No |
| Local Core/auth | Yes | src/max_core/api.py, security.py, config.py | Unit/negative HTTP tests, historical CI | No physical owner PC verification |
| Task state, outbox | Yes | store.py, models.py, migrations 0001/0002 | SQLite unit + ephemeral PostgreSQL concurrent CI | No running worker, owner database or backup restore |
| OpenAI/Anthropic gateway | Yes | gateway library, SSE parsers, privacy/cost gates | HTTPX mocked tests | **No owner FreeLLMAPI/proxy or cloud account smoke** |
| Trust | Yes | Single `task.status` capability, scoped owner approvals, basic audit | Replay, expiry, cross-owner and PG race tests | No production authentication/MFA |
| Independent verification | Yes | Injected evidence-reader hash comparison | Fixture evidence and concurrency tests | **No real external observer** |
| Skills/memory/evolution | Yes | Design only | No lifecycle integration tests | No |
| Desktop/iPhone/voice/nodes | Yes | Design only | No hardware or UX measurements | No |
| Drive/vault/comms/finance | Yes | Design only; consequential tools denied | No live adapters | No |

## Severity-ranked findings

Severity is **conditional on exposing the relevant capability**, not a claim that the loopback-only prototype currently performs it.

| ID | Severity | Evidence | Impact | Recommended correction | Blocks |
| --- | --- | --- | --- | --- | --- |
| A-01 | **Critical** for remote use | `security.py` only bootstrap bearer; [Trust notes](TRUST_IMPLEMENTATION.md) disclaim step-up | A stolen local token could approve the fixed read-only action; no proof of owner presence/session | Keep loopback-only; require authentic owner sessions, revocation, origin/CSRF/rate and strong per-effect confirmation **before any remote/high-impact tool** | Remote or sensitive release |
| A-02 | **High** | `MODEL_GATEWAY.md` and ADR-005; public same-name FreeLLMAPI repositories | Wrong project/endpoint or unverified dialect could misroute keys, outputs or billed model | Confirm owner-selected service/version, endpoint, terms, privacy, provider identity and live sandbox fixtures first | Live provider integration |
| A-03 | **High** | `trust.py` only fixed read; `verification.py` injected fixture observer; no tool worker | No actual Core→Trust→external effect→authoritative verifier path | Add only individually reviewed capabilities and source-specific readback; no generic executor | Side-effectful automation |
| A-04 | **High** | `store.py` plaintext objectives; encryption in [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md) is a proposal | Confidential inputs can persist unencrypted | Keep sensitive task entry prohibited, later add classification, vault separation, retention, encryption and verified deletion | Vault/personal data |
| A-05 | **High** | Gateway validates hostname and HTTPS, but [gateway notes](GATEWAY_IMPLEMENTATION.md) acknowledge no DNS/SSRF egress layer | An approved-looking remote hostname alone does not rule out private-network resolution | Fixed verified provider allowlist, network isolation and re-resolve/private/metadata IP denial | Cloud/browser integrations |
| A-06 | **High** | `gateway/router.py` uses declared price/token ceilings; no reservation or shared ledger | Admission estimate is **not** an absolute spending cap; upstream proxy may select a different billed model | Trusted tokenizer, authoritative account costs/quotas, usage ledger and actual provider/model audit | Metered remote inference |
| A-07 | **High** | Store has leased outbox but no dispatcher; worker reconciliation is storage-only | Cannot guarantee exactly-once external effects or real interruption | Idempotent consumer, external reconciliation and backup/restore drill before side effects | Worker/external effects/production |
| A-08 | **High** | `core-ci.yml` has Ruff, mypy, pytest, pip-audit, not secret/license/SAST/DR scanning | Automated QA is not a security audit or supply-chain attestation | Add relevant scanning/test gates before skills, external tooling or production | Skills/external release |
| A-09 | **Medium — next slice** | `api.py` exposes task get/list but no event timeline or status client | Owner cannot see a persistent chronological task activity stream | Add bounded owner-only read-only timeline and minimal terminal client | **Next local milestone** |
| A-10 | **Medium** | README calls project bootstrap and claims model gateway absent; Roadmap/Plan say earlier milestones are pending | Merged capabilities are understated/misordered, encouraging redundant work | Update status and ordering; retain historical PR descriptions as history | This audit |
| A-11 | **Medium** | [AGENT_RUNTIME.md](AGENT_RUNTIME.md) future `planning/awaiting approval` states differ from DB's `queued/running/awaiting_verification/completed/needs_reconciliation` | Future proposal may be read as implemented state machine | State exact current transitions, label planning states future | Agent milestone |
| A-12 | **Medium** | [REPO_INVENTORY.md](REPO_INVENTORY.md) records missing src/pyproject before PR #4 | Old source inventory could be misread as current | Append current dated tree snapshot, preserve historical test | This audit |
| A-13 | **Medium** | [PHASE0B_VALIDATION_REPORT.md](PHASE0B_VALIDATION_REPORT.md) says eight Mermaid graphs were not rendered | Presence is not renderer proof | Render/inspect diagrams before visual acceptance | Presentation/architecture sign-off |
| A-14 | **Medium** | Desktop, voice and telephony docs explicitly say no physical test/selected India provider | Cannot assert Wayland/iPhone behavior, voice licensing or domestic PSTN route | Dedicated real-device/regional feasibility tests with consent | UI/voice/phone milestones |
| A-15 | **Medium** | Skills, Evolution and Memory documents are marked design-only | No audited dynamic skill installation, learning, rollback or deletion | Sandbox evaluation, versioned registry, memory controls and permission-denial tests | Skills/learning/memory milestones |

**Trust outcome:** Payments, deployments, shell, vault sharing, external communications and file writes are **not registered**; they are absent, **not safely implemented**. Current negative tests prove the one fixed local read cannot silently grow into those actions. Evidence-reader fixtures do not prove external effects.

## FreeLLMAPI — source and contract verification on 2026-11-05

The original owner assignment explicitly names **FreeLLMAPI** with **both API formats**. Preserve this product requirement. A MAX-owned neutral interface is compatible with it, not a substitute for it.

- Active public [tashfeenahmed/freellmapi](https://github.com/tashfeenahmed/freellmapi) and its [API reference](https://github.com/tashfeenahmed/freellmapi/blob/main/docs/en/api/01-rest-api.md) **document** OpenAI-style `POST /v1/chat/completions` and Anthropic-style `POST /v1/messages`. They describe unified authentication (Messages supports `x-api-key` and Bearer), streamed responses and tool-call translation. **Documented upstream behavior is not a MAX test result**.
- Earlier MAX docs linked [lov0u/freellmapi](https://github.com/lov0u/freellmapi/blob/main/docs/api.md), a **different repository/account**. A name match is not authorization to trust a fork, domain or credential destination. The precise owner-selected product/deployment/version is **unconfirmed**.
- Reference wire formats are documented by [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) and [Anthropic streaming](https://platform.claude.com/docs/en/build-with-claude/streaming). MAX only mock-tests a text and inert-tool-proposal subset of each, **not** FreeLLMAPI's model discovery, structured outputs, tool-result loop, all streaming variants or actual quotas.
- No connection or credential inspection was authorized. Required later: owner confirms trusted source/URL/release, checks per-model auth/privacy/stream/tool/usage behavior, checks actual routed and billed model, and explicitly approves a **non-production** smoke test. Owner-reported token allocations (~1.2 billion) are not interchangeable, verified spend balances.

## Consistency, security and next dependency

- Current adopted **Python/FastAPI + SQLAlchemy/Alembic** implementation is real; Tauri/Playwright/Ollama/Go/SearXNG are **candidates**, not contradictory active stacks. OpenClaw is a considered reference rather than MAX's runtime; any future reuse requires a pinned license/security review.
- Authority at design level is coherent: PostgreSQL for task/approval/outbox; Google Drive only for owner-approved documents/exports; GitHub source/docs; local scratch disposable; separately encrypted vault. Only the operational DB slice is implemented.
- Security design preserves model-proposal/untrusted-input boundaries, but production identity, network controls, tamper-evident audit, actual device enrollment, encrypted backups and source-specific verifier remain missing.
- The next safe, dependency-light milestone is **a local, owner-only, read-only task timeline and terminal client**, not an unverified FreeLLMAPI launch or computer-control agent.

### Recommended Milestone 5 — do not begin in this PR

**User-visible outcome:** Owner runs a local terminal command to view a task's current state and an ordered, replayable timeline of metadata events (queued, checkpointed, cancelled, verified/reconciliation), without implying external work was performed.

**Components:** owner-scoped bounded `GET /v1/tasks/{task_id}/events`, stable pagination/cursor querying existing PostgreSQL `max_outbox` event metadata, and minimal local CLI using an environment-injected **development** bearer token. No direct database writes by CLI; no approval execution; no additional SQL migration unless an index is proven necessary.

**Dependencies:** owner-merged audit PR; local PostgreSQL at migration 0002 and existing Core API loopback. No FreeLLMAPI account needed.

**Security and tests:** exact authenticated owner isolation (401/404), fixed metadata fields only (never objective/secret), cursor tampering, ordering across restarts, empty/large pagination, bounded read and concurrent PostgreSQL event insertion; Ruff, strict mypy, unit/PG matrix and pip-audit. CLI must print observed state faithfully, not transform an internal outbox event into evidence of real-world success.

**Rollback:** revert the scoped API/client PR; no external effects. Review index migration only if needed. **Excluded:** model credentials, worker/agent runner, external tool actions, cloud/web/mobile clients, finance, vault, communications, remote devices, deployment or live account access.

## Audit PR acceptance

Only documentation files may change. Verify all changed file paths, local Markdown links and named documents, sensitive-data patterns, current-branch PR diff and GitHub CI outcome. Missing test/tool evidence is **unverified**, never a pass. The owner alone reviews and merges.
