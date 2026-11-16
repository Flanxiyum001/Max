"""Offline validation of the optional pgvector adapter."""

from __future__ import annotations

import math

import pytest
from sqlalchemy import create_engine

from max_core.semantic_memory import SemanticMemoryError, SemanticMemoryIndex, vector_literal


def values() -> list[float]:
    return [0.5] * 384


def test_vector_literal_is_bounded_and_canonical() -> None:
    value = vector_literal(values())
    assert value.startswith("[0.5,0.5")
    assert value.endswith("]")
    assert len(value.split(",")) == 384


@pytest.mark.parametrize(
    "value",
    [
        [0.5] * 383,
        [0.5] * 385,
        [0.0] * 384,
        [math.inf] * 384,
        [math.nan] * 384,
        [True] * 384,
        ["0.5"] * 384,
    ],
)
def test_invalid_vectors_rejected(value: list[object]) -> None:
    with pytest.raises(SemanticMemoryError, match="invalid_embedding"):
        vector_literal(value)  # type: ignore[arg-type]


def test_non_postgres_backend_never_executes_semantic_queries() -> None:
    index = SemanticMemoryIndex(create_engine("sqlite+pysqlite:///:memory:"))
    with pytest.raises(SemanticMemoryError, match="postgresql_required"):
        index.search(
            owner_id="owner",
            scope="personal",
            embedding_model="local-384",
            query_embedding=values(),
        )
    with pytest.raises(SemanticMemoryError, match="postgresql_required"):
        index.upsert(
            owner_id="owner",
            scope="personal",
            memory_id="id",
            expected_revision=2,
            embedding_model="local-384",
            embedding=values(),
        )


def test_invalid_owner_and_limits_fail_before_db_io() -> None:
    index = SemanticMemoryIndex(create_engine("sqlite+pysqlite:///:memory:"))
    with pytest.raises(SemanticMemoryError, match="invalid_memory_scope"):
        index.search(
            owner_id="../owner",
            scope="personal",
            embedding_model="local-384",
            query_embedding=values(),
        )
    with pytest.raises(SemanticMemoryError, match="invalid_search_limit"):
        index.search(
            owner_id="owner",
            scope="personal",
            embedding_model="local-384",
            query_embedding=values(),
            limit=1000,
        )
