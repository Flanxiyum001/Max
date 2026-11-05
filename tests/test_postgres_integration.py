"""Opt-in real PostgreSQL acceptance tests; CI supplies MAX_TEST_DATABASE_URL."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from max_core.store import TaskStore


@pytest.fixture
def postgres_store() -> TaskStore:
    url = os.environ.get("MAX_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL test URL is not configured")
    store = TaskStore(create_engine(url, pool_pre_ping=True))
    assert store.ready(), "Apply Alembic migrations before PostgreSQL tests"
    return store


def test_real_postgresql_migrations_and_restart(postgres_store: TaskStore) -> None:
    task, created = postgres_store.create("owner", "Postgres restart", "pg-" + uuid4().hex)
    assert created
    assert TaskStore(postgres_store.engine).get("owner", task.id).state == "queued"


def test_postgresql_concurrent_claim_and_outbox_lease(postgres_store: TaskStore) -> None:
    task, _ = postgres_store.create("owner", "PG contention", "pg-" + uuid4().hex)

    def claim(worker: str) -> bool:
        from max_core.store import StoreError

        try:
            postgres_store.claim("owner", task.id, worker)
            return True
        except StoreError:
            return False

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(claim, ["worker-a", "worker-b", "worker-c", "worker-d", "worker-e"]))
    assert sum(results) == 1

    # All pending events are leased by only one claim token; no duplicate claim per event.
    with ThreadPoolExecutor(max_workers=3) as executor:
        leases = list(executor.map(lambda _: postgres_store.lease_outbox(), range(3)))
    event_ids = [event.id for batch in leases for event in batch]
    assert len(event_ids) == len(set(event_ids))
