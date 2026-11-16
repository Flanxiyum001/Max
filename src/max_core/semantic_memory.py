"""Optional, owner-scoped pgvector search over approved MAX memory records.

No model download, embedding generation, cloud egress, or automatic indexing.
Host must supply embeddings from an explicitly reviewed local model only.
Requires the separately owner-installed SQL schema in sql/optional_pgvector_384.sql.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

_OWNER = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_MODEL = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
_DIM = 384


class SemanticMemoryError(ValueError):
    """Fixed operational code; never exposes SQL, memory contents or embeddings."""


@dataclass(frozen=True, slots=True, repr=False)
class SemanticMemoryHit:
    memory_id: str
    subject_key: str
    content: str
    similarity: float


def vector_literal(values: Sequence[float]) -> str:
    """Validate before a parameterized PG vector cast. No dynamic SQL."""
    if (
        not isinstance(values, (list, tuple))
        or len(values) != _DIM
        or any(type(value) not in (int, float) for value in values)
    ):
        raise SemanticMemoryError("invalid_embedding")
    floats = [float(value) for value in values]
    if not all(math.isfinite(value) for value in floats) or not any(floats):
        raise SemanticMemoryError("invalid_embedding")
    return json.dumps(floats, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _identity(owner_id: str, scope: str, embedding_model: str) -> None:
    if (
        not isinstance(owner_id, str)
        or _OWNER.fullmatch(owner_id) is None
        or not isinstance(scope, str)
        or _OWNER.fullmatch(scope) is None
        or not isinstance(embedding_model, str)
        or _MODEL.fullmatch(embedding_model) is None
    ):
        raise SemanticMemoryError("invalid_memory_scope")


class SemanticMemoryIndex:
    """Explicit PostgreSQL-only adapter; all content still gated by M12 review."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _postgres(self) -> None:
        if self.engine.dialect.name != "postgresql":
            raise SemanticMemoryError("postgresql_required")

    def upsert(
        self,
        *,
        owner_id: str,
        scope: str,
        memory_id: str,
        expected_revision: int,
        embedding_model: str,
        embedding: Sequence[float],
    ) -> None:
        """Index a current approved record, never arbitrary candidate text."""
        _identity(owner_id, scope, embedding_model)
        if type(expected_revision) is not int or expected_revision < 1:
            raise SemanticMemoryError("invalid_memory_revision")
        vector = vector_literal(embedding)
        self._postgres()
        try:
            with self.engine.begin() as connection:
                row = connection.execute(
                    text(
                        "SELECT revision FROM max_memories "
                        "WHERE id=:id AND owner_id=:owner AND scope=:scope "
                        "AND state='approved' "
                        "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) "
                        "FOR UPDATE"
                    ),
                    {"id": memory_id, "owner": owner_id, "scope": scope},
                ).mappings().first()
                if row is None or row["revision"] != expected_revision:
                    raise SemanticMemoryError("memory_not_approved_or_stale")
                connection.execute(
                    text(
                        "INSERT INTO max_memory_embeddings "
                        "(memory_id, owner_id, scope, embedding_model, revision, embedding) "
                        "VALUES (:id, :owner, :scope, :model, :revision, "
                        "CAST(:vector AS vector(384))) "
                        "ON CONFLICT (memory_id) DO UPDATE SET "
                        "owner_id=EXCLUDED.owner_id, scope=EXCLUDED.scope, "
                        "embedding_model=EXCLUDED.embedding_model, "
                        "revision=EXCLUDED.revision, embedding=EXCLUDED.embedding"
                    ),
                    {
                        "id": memory_id,
                        "owner": owner_id,
                        "scope": scope,
                        "model": embedding_model,
                        "revision": expected_revision,
                        "vector": vector,
                    },
                )
        except SQLAlchemyError:
            raise SemanticMemoryError("semantic_backend_unavailable") from None

    def search(
        self,
        *,
        owner_id: str,
        scope: str,
        embedding_model: str,
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> tuple[SemanticMemoryHit, ...]:
        """Revision-bound retrieval: rejected, stale and deleted facts cannot match."""
        _identity(owner_id, scope, embedding_model)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise SemanticMemoryError("invalid_search_limit")
        vector = vector_literal(query_embedding)
        self._postgres()
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT m.id, m.subject_key, m.content, "
                        "(1 - (e.embedding <=> CAST(:vector AS vector(384)))) AS similarity "
                        "FROM max_memory_embeddings e "
                        "JOIN max_memories m ON m.id=e.memory_id "
                        "WHERE m.owner_id=:owner AND m.scope=:scope "
                        "AND e.owner_id=m.owner_id AND e.scope=m.scope "
                        "AND e.embedding_model=:model AND e.revision=m.revision "
                        "AND m.state='approved' "
                        "AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP) "
                        "ORDER BY e.embedding <=> CAST(:vector AS vector(384)), m.id "
                        "LIMIT :limit"
                    ),
                    {
                        "owner": owner_id,
                        "scope": scope,
                        "model": embedding_model,
                        "vector": vector,
                        "limit": limit,
                    },
                ).mappings().all()
                return tuple(
                    SemanticMemoryHit(
                        memory_id=row["id"],
                        subject_key=row["subject_key"],
                        content=row["content"],
                        similarity=float(row["similarity"]),
                    )
                    for row in rows
                )
        except SQLAlchemyError:
            raise SemanticMemoryError("semantic_backend_unavailable") from None
