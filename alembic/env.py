"""Migrations require an explicit PostgreSQL connection string in the environment."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from max_core.models import Base


target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = os.environ.get("MAX_DATABASE_URL", "")
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("MAX_DATABASE_URL must be a PostgreSQL+psycopg URL")
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()
