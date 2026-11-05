# MAX memory architecture

**Status:** Design only.

Separate session context, owner-approved profile facts, project/task records, sourced knowledge, and operational audit evidence. Every durable memory needs provenance, subject/scope, timestamps, confidence, retention, version and correction history.

The authoritative operational store must support transactional writes, indexes, migrations, backup and restore. Selected user documents live in a separately encrypted repository. Temporary caches are not authoritative.

Retrieval is scoped by identity, project, sensitivity and token budget. Model inference is not an owner-confirmed fact. Do not automatically memorize sensitive document bodies, credentials or third-party private conversations.

Owner controls: inspect, correct, export, delete, set retention and revoke access. Deletion must include caches and indexes, with defined backup expiry and restore behavior.

**Tests:** duplicate memories, contradictory facts, stale provenance, authorization leakage, deletion and backup restore, concurrent updates, failed migration and recovery.
