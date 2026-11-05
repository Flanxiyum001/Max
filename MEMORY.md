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
