M37: Persistent Semantic Memory (Hybrid Recall + Owner-Approved MEMORY.md)

Status: implemented (this milestone)

## Goal

Give MAX persistent recall across months of sessions without giving up the
core invariant: nothing enters the long-term memory record without a
single-use owner approval. Two capabilities land together:

1. **Hybrid vector + FTS5 recall index** — a derived, rebuildable SQLite
   index over three bounded sources (notes from `MAX_NOTES_DIR`, completed
   task journals, `MEMORY.md` entries) queried through `sqlite-vec` kNN and
   SQLite FTS5 full-text search, fused with Reciprocal Rank Fusion.
2. **Trust-gated memory proposals** — `memory.propose_update` (`R1`) is the
   only way `MEMORY.md` is ever written; each append/remove lands in the
   **Trust Inbox** as a single-use approval the owner inspects and approves
   in one click. After a long chat or a completed `task.plan`, the assistant
   proposes concrete memory updates through this exact path — the owner gate
   is the point, not the authoring actor.

## What is authoritative vs derived

- `MEMORY.md` is an **owner-approved fact journal**, bounded at 512 entries
  of ≤ 280 normalized characters each (whitespace-collapsed, control-char
  free, `#`/``` fencing rejected). Its default location is the parent of
  `MAX_NOTES_DIR`; `MAX_MEMORY_MD_PATH` overrides it.
- The recall index (`max-memory-index.sqlite3`, sibling of `MEMORY.md`,
  `MAX_MEMORY_INDEX_PATH` override) is **derived and rebuildable** — it is
  never authoritative and can be deleted at any time; the next `memory.search`
  call rebuilds it from source. It carries no migration, no alembic revision,
  and no row in any PostgreSQL store.

## Deterministic local embeddings (no model, no network)

`embed(text)` is 256-dim signed feature hashing over unigrams and bigrams:

- tokens are `[a-z0-9]+` (lowercased, first 512);
- each token hashes via blake2b (digest_size=8) to `(slot, sign)`:
  `slot = value % 256`, sign taken from the top bit;
- unigrams add ±0.5, bigrams add ±1.0, and the vector is L2-normalized
  (all-zero text returns the zero vector).

The same text always yields the same vector — deterministic, unit-tested,
no model download, no API key, no cloud round-trip. Vector recall runs
entirely inside the local SQLite file.

## Hybrid channels and fusion

`memory.search` (`R0`, auto-executed, audited) accepts `{query: 1..300,
limit?: 1..8}`:

- **FTS5 channel**: up to 16 unique query tokens (length ≥ 2) become an
  OR-quoted `MATCH` expression ordered by `bm25(memory_fts)`.
- **Vector channel**: when the `sqlite-vec` extension loads into the
  connection, a kNN match over `float[256]` embeddings via
  `sqlite_vec.serialize_float32`. `sqlite-vec` is an optional dependency
  (the `semantic` extra; also included in `dev` for CI). If the extension is
  missing or fails to load, MAX degrades to FTS-only recall and the tool
  output reports `"vector": false` — availability is observable, never
  silently assumed.
- **Fusion**: Reciprocal Rank Fusion (`1/(60 + rank)` per channel,
  deduplicated by document rowid); snippets are ≤ 240 characters, taken from
  the FTS5 `snippet()` around a matched term when available, otherwise a
  bounded token-window over the body.

Known limitation, by design: vector kNN returns the nearest documents even
for queries with no true lexical or semantic overlap, so the fused results
always fill up to `limit` when documents exist. Recall is intentionally
"best effort, ranked" — the owner judges from snippets with source, ref and
title; nothing in the result is state-changing.

## Bounded sources

| Source | Ref | Bound |
| --- | --- | --- |
| Notes | `*.txt` stems in `MAX_NOTES_DIR` | ≤ 500 notes × 4000 chars |
| Tasks | terminal (`completed`/`cancelled`/`failed`) journal ids, via `TaskStore.list_terminal(owner_id)` | ≤ 200 tasks × 2000 chars |
| Memory | sequential `MEMORY.md` entry positions | ≤ 512 entries × 280 chars |

Sync is idempotent: a sha256 fingerprint over the collected
`source|ref|body` stream is stored in the index; when unchanged, `sync`
returns without touching any table. Only terminal task objectives are
indexed — pending and awaiting-approval states are never recalled.

## Trust boundary and approval flow

- `memory.search` is **R0** — read-only, executes immediately, every call is
  audited in `max_trust_audit` like any other local tool.
- `memory.propose_update` is **R1** (`requires_approval=True`) with exactly
  `{action: "append"|"remove", entry: 1..280}`. Execution is the approved
  edit itself: a deterministic, atomic (temp-file + `os.replace`, fsync)
  rewrite of `MEMORY.md`'s bounded entry list — never an in-place append, so
  partial writes cannot occur. Case-insensitive dedupe returns
  `already_present` instead of duplicating facts; `memory_md_full` (409)
  surfaces the 512-entry cap.
- Because MEMORY.md entries themselves are a recall source, an approved
  append becomes searchable immediately on the next `memory.search` sync.

## Operational error codes

`invalid_memory_document`, `invalid_memory_entry`, `memory_md_full` (409),
`entry_not_found` (404), `memory_md_unreadable` (500),
`memory_md_write_failed` (500), `invalid_memory_index_path`,
`memory_unavailable` (500), `invalid_memory_query`. Codes are fixed
identifiers — never SQL, paths, or memory contents.

## Validation

- 12 new tests in `tests/test_memory_recall.py`: embedding determinism/norm,
  entry validation, dedupe/caps, atomic removal, heading-safe reads,
  end-to-end `memory.search` R0 execution, and the full R1 approve flow for
  both append and remove.
- ruff + `ruff format --check` + mypy `--strict` clean; full suite green
  apart from the 4 pre-existing subprocess e2e failures (environment only,
  pass in CI).

## Rollback

Delete `max-memory-index.sqlite3` (derived), remove the `sqlite-vec` extra,
and revert the `local_tools.py` registry entries for the two `memory.*`
tools. `MEMORY.md` is a plain Markdown file the owner can edit or delete
directly at any time — the file format carries no lock-in.
