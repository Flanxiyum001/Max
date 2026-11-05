# MAX repository audit — verified scope and limitations

**Date:** 2026-11-05. **Source:** connected GitHub repository `Flanxiyum001/Max`, default branch `max`, checked after owner merged PR #2 at commit `9ac2178348cd2e675da86957ea98c70faccdeed9`.

## Confirmed project documents

Found and read: root `MAX.md`, `SOUL.md`, `CORE.md`, `MEMORY.md`, `SKILLS.md`, `TOOLS.md`, `SECURITY.md`, `EVOLUTION.md`, `IDENTITY.md`, `OPERATIONS.md`; `README.md`, `AGENTS.md`, `ARCHITECTURE.md`; `docs/PRODUCT_SPECIFICATION.md`, `docs/TECHNICAL_DESIGN.md`, `docs/DECISIONS.md`, `docs/ROADMAP.md`, `docs/DEVELOPMENT_WORKFLOW.md` and the 0A module proposals.

MAX identity documents are **requirements and intended behavior**, not evidence of runtime enforcement.

## Code evidence

The following exact paths were probed on `max` and returned NOT_FOUND: `pyproject.toml`, `requirements.txt`, `src/max/core/agent.py`, `src/max/agent.py`, `src/max/__init__.py`, `bot.py`, `main.py`, `package.json`, `Dockerfile`, `.github/workflows/ci.yml`. `src` as a directory did not exist at that path in the checked branch. Searches for `FastAPI`, `Discord`, `OpenHands`, `PCBridge` and these candidate source paths returned no indexed matches.

**Limitations:** these checks do **not** establish a full recursive repository inventory or prove there is no runnable code anywhere. GitHub code search indexing/permissions and alternate paths may hide content. Do not promote prior conversational descriptions of code into present-tense repository facts. A complete tracked-file tree, CI workflow listing, dependency manifests, licenses and secret scan must be collected before marking code-inventory acceptance complete.

## Reconciliation needed before Phase 1

1. Compare source tree with architecture assumptions and identify active entry points, tests and deployment links.
2. Fix outdated `main` branch reference in [DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md); default branch is `max`.
3. Confirm whether any MAX runtime from older branches belongs to this repository or another. Avoid overwriting it.
4. Resolve FreeLLMAPI description conflict between owner assignment and existing requirements.
5. Keep changes documentation-only; no Discord bot, database, deployment, or account changes.

## Current MAX tree — additive post-PR #7 audit (2026-11-05)

**Do not use the 0B historical missing-code probes above as present-tense facts.** The GitHub recursive tree of default branch `max` at commit `72f67b1ec6a737c813f4ca633457a9ca39223b94` returned **79 tracked blobs**, not a candidate-path search. Current tracked runtime includes `pyproject.toml`, `src/max_core/` with API/auth/gateway/task store/Trust/verifier, two Alembic revisions, `.github/workflows/core-ci.yml`, and nine test modules. Local untracked/staged state is **not visible through GitHub**. This audit does not prove a running app, deployed database, owner PC/device test, real provider, or production service.

[Current implementation and blockers](PHASE0_AUDIT.md); [owner requirement matrix](REQUIREMENT_TRACEABILITY.md). The original snapshot above remains useful only as historical baseline evidence.
