# Milestone 12 — Provenance-first memory foundation

**Status:** Review-only library and PostgreSQL schema, not deployed or connected to inference.

## Observable scope

- A single-owner-scoped SQLAlchemy `MemoryStore` over the existing PostgreSQL-backed Core.
- Separate candidate (`pending`) and retrieval (`approved`) states.
- Explicit provenance categories: `explicit_user_correction`, `inferred_from_task`, `external_doc`, plus a required SHA-256 source digest, observation time, scope and base confidence (0–1000).
- Deterministic confidence decay for inferred facts (30-day half-life) and sourced documents (90-day half-life), never for owner-confirmed corrections; expired candidates or facts cannot enter retrieval.
- Last-seven-days review queue, limited to the five newest pending proposals per owner and scope. Library methods support approve, reject, explicit correction and hard deletion with optimistic revision checks.
- Correction events retain **metadata only**, not previous raw statements; hard deletion erases both content and associated event metadata.
- Fixed error codes, parameterized SQL, owner/scope filters and PostgreSQL row locks.
- Tests for contradictory slots, unapproved retrieval, stale review, cross-owner access, decay, expiry, hard erase and PostgreSQL migration integration.

## Hard boundaries

This does **not** install or call pgvector, produce embeddings, infer facts automatically, read files, enable a vector or graph search index, schedule a weekly digest, expose a UI/CLI/API, or attach memory to model prompts. Retrieval here is simple approved SQL-scoped selection and remains capped at 1,000 recent rows per scope. It is not yet production-scale or relevance-ranked hybrid retrieval.

**There is no memory-writing permission for AI models or MCP tools.** The host that eventually invokes this library must authenticate the real owner, enforce `public`/`internal` data classification, filter secrets and other sensitive personal information, verify provenance and rate-limit writes. Passing an `owner_id` string into a library is *not* authentication. Never expose these methods as callable model tools.

All proposals are pending — even those tagged `explicit_user_correction`. Such a tag is **not proof** that the owner spoke. A trusted owner action is required to approve or correct. Do not promote untrusted model claims.

Source hashes preserve a comparison handle, **not** reconstructable source evidence. Before richer retrieval, add a trusted source registry, subject/entity graph, approved text embedding pipeline, and pgvector search with capability and deletion checks. No database extension is provisioned in this milestone.

## Migration and validation

The only schema change is additive revision `0004_memory_foundation`, following `0003_independent_observer`. The CI pipeline applies all migrations to ephemeral PostgreSQL and runs Ruff lint/format, mypy, unit/security/integration tests and pip-audit on Python 3.11 and 3.13. A passed CI does not prove live memory sync, retention on backups, data classification, or real devices.

Do not run migrations against production until there is an owner-approved backup, restore drill and rollback plan. **Downgrade drops all memory tables and data.**

## Next scoped milestones

1. Add a trusted owner-authenticated review interface and scheduled weekly five-fact digest. Preserve pending state and explicit delete/correct operations; no notification without consent.
2. Add source-registry IDs and graph edges with owner/scope constraints and revocation.
3. Enable pgvector **only** after extension privileges, embedding-provider egress classification, retention, recomputation and deletion cascades have been independently verified.
4. Implement hybrid scoring and adversarial tests (stale/contradictory facts, cross-owner leakage, poisoned documents and recalled deletions).
5. Separately bridge cloud/local inference and MCP execution through MAX Trust; a memory record must never grant tool permissions.
