# MAX data, storage, retention and recovery architecture

**Status:** Proposed design, no existing database or connected Google Drive is implied. Owner preferences: PostgreSQL for operational state and Google Drive for selected documents/exports, with a separate protected personal vault.

## Data plane and authority

| Data | Authority | Secondary storage / limits |
| --- | --- | --- |
| Task state, checkpoints, approvals, worker assignments | PostgreSQL in transactional MAX Core domain | Encrypted versioned backups; never just process RAM |
| Outbox and tool-effect records | PostgreSQL | Audit exports and reconciliation results |
| Memory metadata, source references, retention and deletion tombstones | PostgreSQL | Vector index rebuildable from approved content; do not treat vector DB as sole authority |
| Research documents/selected personal files | Explicitly granted Google Drive folder or encrypted object store | Versioned export; Drive not mandatory or trusted as a sole backup |
| Encrypted vault contents | Separate encrypted store with per-object data keys | Owner-selected recovery path; plaintext not in ordinary memory |
| Secrets/OAuth refresh tokens | OS keychain/managed secret store | Never in project Git, prompts, logs or agent-controlled memory |
| Local temporary jobs, captures and browser downloads | Isolated scratch directories | Expire and delete automatically after audited retention window |

## Candidate relational schema

Use UUID primary keys with indexed owner/session scope and explicit timestamps:
- `tasks(id, owner_id, objective_class, state, deadline, created_at, updated_at, current_checkpoint_id, cancellation_token)`
- `task_steps(id, task_id, step_key, state, retries_used, evidence_ref, verifier_version)`
- `approvals(id, owner_id, task_id, action_digest, risk_tier, status, expires_at, consumed_at, approver_session_id)`
- `tool_calls(id, task_id, tool_version, args_digest, idempotency_key, auth_decision_id, effect_state, evidence_ref)`
- `outbox(id, aggregate_id, kind, payload_ref, idempotency_key, state, attempts, next_attempt_at)`
- `memories(id, owner_id, type, value_ref, source_ref, classification, confidence, consent_state, expires_at, superseded_by)`
- `documents(id, owner_id, storage_ref, checksum, encryption_version, access_policy, retention_policy, deleted_at)`
- `audit_events(id, actor, subject, action, decision, tool_call_id, context_digest, observed_at, prev_hash)` (hash chaining optional; do not assume tamper-proof without external append-only sink).
Store normalized indexes and retention details, but minimize raw sensitive transcript and model prompt storage.

## Consistency and idempotency

One transaction records the task transition + outbox event. A separate worker delivers with bounded retries and reconciles outcomes. Do not claim exactly-once delivery; target **at-least-once processing with deduplication, idempotency keys and authoritative reconciliation**. For irreversible external effects, uncertainty triggers owner review, not blind retries. APIs require optimistic version checks on mutation. Expired approvals cannot be reused or escalated.

## Classification and encryption

- Public / project-internal / confidential / restricted-identity-financial-credential. Inheritance from [SECURITY.md](../SECURITY.md).
- In transit TLS; at rest DB/disk encryption plus application-level envelope encryption for restricted objects. Rotation plans, per-object nonce and authenticated encryption, key separation, identity-aware access logs.
- Restricted plaintext is available only to a narrow trusted operation, not general model context. Vault sharing requires recipient and content preview, fresh strong auth and an auditable delivery method.
- No UPI PIN, OTP, CVV, banking authentication factor, or unnecessary secret in any database, backup, tool output, telemetry or vector index.

## Google Drive adapter

Start with owner-granted folder/files and narrow `drive.file` OAuth scope where feasible; full-drive scopes are **not default**. Google classifies wide Drive access as restricted and may impose verification/security assessment (see [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)). Refresh tokens must be stored outside agent-readable files.

Drive file IDs, versions, checksums, MIME type, owner, permission grants and access expiry belong in PostgreSQL. Encrypt exports before upload if required; document encryption key recovery separately. On sharing, verify destination and actual Drive ACL after write. Deletions may need retention/tombstone reconciliation and access revocation.

Google [Drive revisions](https://developers.google.com/workspace/drive/api/guides/manage-revisions) can be pruned; never equate revision history with immutable backup. [Usage quotas](https://developers.google.com/workspace/drive/api/guides/limits) depend on project/account and have changed in 2026; capacity/cost quoted by owner must be verified. Do not mirror or search whole personal Drive without explicit scope.

## Backups and recoverability

**Candidate owner-review goals, not accepted SLAs:** encrypted logical daily backup + independently stored weekly snapshot; candidate RPO 24 hours and RTO 4 hours for a noncritical personal MVP. Pick smaller targets only after throughput/storage cost benchmark. For more demanding recovery use tested PostgreSQL base backups + WAL archive/PITR; [PostgreSQL docs](https://www.postgresql.org/docs/current/continuous-archiving.html) explicitly distinguish WAL backups from `pg_dump`.

Backup inventory: manifest with source commit/schema version, DB version, file counts, SHA-256 hashes, encryption key ID, created_at, retention_until. Apply 3-2-1 principles where feasible; do not pretend a second folder on the same disk is offsite. Use a clean isolated environment to restore schema + tasks + attachments + ACL metadata, verify checksums and orphan handling, and record actual restore time. Test compromised key/Drive-down and backup-missing scenarios.

## Memory correction, retention and deletion

Memory creation requires type/scope/provenance; inferred facts are marked as such, corrections create superseding records. Deletion is a workflow: deny future retrieval, tombstone authoritative record, purge caches/search indexes, delete approved external objects, respect immutable/legal retention if applicable, schedule backup expiration, and provide owner-visible evidence. Historical backups are not rewritten unless supported safely; document delayed erasure window. Minimize sensitive log retention.

## Migration and failure tests (future)

- Crash immediately before/after outbox commit -> no lost state or duplicate irreversible effect.
- Conflict/replay on approval -> reject second consumption.
- Corrupt file/checksum or partial Drive upload -> quarantine, not silent success.
- DB failover, Drive unavailable, backup restore, deleted source reference and key rotation -> bounded, auditable behavior.
- Restricted prompt/attachment never appears in model provider logs.
- Failed deletion across backups -> surfaced as pending with retention horizon.

## Ownership gates

No database provisioning, Drive OAuth connection, real document ingestion or key generation during Phase 0. Owner later approves data location, recovery targets, retention and vault key-recovery strategy.
