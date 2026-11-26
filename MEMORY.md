# MEMORY — Policy and Lifecycle

## Memory categories

- **Profile memory:** owner-approved preferences and stable context.
- **Project memory:** goals, requirements, decisions, and project state.
- **Task memory:** plans, checkpoints, tool outcomes, and pending actions.
- **Knowledge library:** sourced documents, notes, and research with provenance.
- **Operational records:** audit events, errors, and verification evidence.

## Storage responsibilities

PostgreSQL is the proposed authoritative store for structured application state, task state, and memory indexes. Google Drive is the preferred user-controlled document repository and may hold selected exports and backups. Local storage is for caches and temporary working data. GitHub stores source code and non-secret documentation.

Google Drive is not the sole database or sole backup. Use versioning, retention, encryption where appropriate, and tested restore procedures.

## Memory rules

- Record provenance, timestamp, confidence, and scope.
- Separate user-confirmed facts from model inference.
- Do not store passwords, API keys, OTPs, UPI PINs, CVVs, or unnecessary sensitive identifiers.
- Sensitive document contents should not be copied into ordinary conversational memory.
- Support correction, export, retention controls, and deletion workflows.
- Do not learn permanent preferences from a single ambiguous event without adequate evidence.

## Implementation boundary — Milestone 12

An owner-scoped, review-first PostgreSQL memory **library** is proposed on a separate review branch. It stores pending candidates and explicitly approved statements with source digests, timestamps, confidence decay, owner corrections and hard deletion. It has **no model-driven ingestion, connected identity/UI, pgvector, graph retrieval, background review digest or production deployment**. Implementation and controls: [Milestone 12](docs/M12_MEMORY_FOUNDATION.md).

## Long-term memory repo

MAX's durable memory lives in the private **Max-memory** repo (`Flanxiyum001/Max-memory`): `MEMORY.md` (owner-approved facts) and `notes/*.txt` (longer knowledge). Clone it to `%USERPROFILE%\MAX\memory` and point `MAX_MEMORY_MD_PATH`, `MAX_NOTES_DIR` and `MAX_MEMORY_INDEX_PATH` at it (see `.env.example`). Run `scripts/sync.ps1` there to pull before work and push after. Skills live in **Claude-Skills** (`Flanxiyum001/Claude-Skills`). Never put secrets in either repo.
