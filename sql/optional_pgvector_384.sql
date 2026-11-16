-- OWNER-OPERATED, OPT-IN schema; not part of Alembic auto-upgrade.
-- Require an approved backup/restore test, a dedicated embedding-model policy,
-- extension installation privileges, and a reviewed rollback BEFORE applying.
-- Embeddings are sensitive metadata. Do not export them to unreviewed providers.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE max_memory_embeddings (
    memory_id varchar(36) PRIMARY KEY REFERENCES max_memories(id) ON DELETE CASCADE,
    owner_id varchar(64) NOT NULL,
    scope varchar(64) NOT NULL,
    embedding_model varchar(100) NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    embedding vector(384) NOT NULL
);

CREATE INDEX ix_max_memory_embeddings_scope
    ON max_memory_embeddings (owner_id, scope, embedding_model);
CREATE INDEX ix_max_memory_embeddings_hnsw
    ON max_memory_embeddings USING hnsw (embedding vector_cosine_ops);

-- Remove only with explicit owner-approved rollback:
-- DROP TABLE max_memory_embeddings;
-- Do not automatically drop the shared vector extension.
