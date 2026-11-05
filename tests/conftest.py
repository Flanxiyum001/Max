from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from max_core.api import create_app
from max_core.config import Settings
from max_core.models import Base
from max_core.store import TaskStore

# Public deterministic fixture, NOT a credential for any real environment.
TEST_TOKEN = "test-token-" + "0123456789abcdef" * 3


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(bearer_token=TEST_TOKEN)), base_url="http://localhost")


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    """Shared isolated SQLite test schema; API production runtime still requires PostgreSQL."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'max-trust-tests.sqlite3'}")
    Base.metadata.create_all(engine)
    return TaskStore(engine)


@pytest.fixture
def postgres_store() -> TaskStore:
    """Real PostgreSQL fixture used by GitHub CI; local-only test runs skip it."""
    url = os.environ.get("MAX_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL test URL is not configured")
    instance = TaskStore(create_engine(url, pool_pre_ping=True))
    assert instance.ready(), "Apply Alembic migrations before PostgreSQL tests"
    return instance
