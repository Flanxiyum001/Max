"""Native app in-process backend uses actual owner-scoped Core API contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from max_core.gateway.contracts import ToolProposal
from max_core.native_desktop.backend import (
    NativeDesktopCore,
    NativeDesktopError,
    desktop_database_url,
    local_database_url,
)
from max_core.store import TaskStore
from max_core.trust import ActorContext, TrustService


def test_local_database_guard_never_accepts_remote_database() -> None:
    assert local_database_url(None) is None
    assert local_database_url("") is None
    assert (
        local_database_url("postgresql+psycopg://user:pw@127.0.0.1:5432/max")
        == "postgresql+psycopg://user:pw@127.0.0.1:5432/max"
    )
    for url in (
        "postgresql+psycopg://u:p@db.example.com:5432/max",
        "postgresql+psycopg://u:p@192.168.1.10:5432/max",
        "postgresql+psycopg://u:p@localhost/max",
        "sqlite:///tmp/max.db",
        "postgresql+psycopg://u:p@localhost:5432/",
        "not a url",
    ):
        with pytest.raises(NativeDesktopError):
            local_database_url(url)


def test_desktop_database_url_supports_local_sqlite_and_preserves_pg_guard(
    tmp_path: Path,
) -> None:
    sqlite_url = f"sqlite+pysqlite:///{(tmp_path / 'desk.sqlite3').as_posix()}"
    assert desktop_database_url(sqlite_url) == sqlite_url
    assert (
        desktop_database_url("postgresql+psycopg://user:pw@127.0.0.1:5432/max")
        == "postgresql+psycopg://user:pw@127.0.0.1:5432/max"
    )
    assert desktop_database_url(None) is None
    assert desktop_database_url("") is None

    for bad_url in (
        "sqlite+pysqlite:///:memory:",
        "sqlite+pysqlite:///   ",
        "sqlite+pysqlite:///file::memory:?cache=shared",
        "sqlite+pysqlite:///file:mem.sqlite3",
        f"sqlite+pysqlite:///{(tmp_path / 'desk.sqlite3').as_posix()}?mode=memory&uri=true",
        f"sqlite+pysqlite:///{(tmp_path / 'desk.sqlite3').as_posix()}?cache=shared",
        "sqlite+pysqlite://localhost/tmp/max.db",
        "sqlite+pysqlite://",
        "sqlite:///tmp/max.db",
        "postgresql+psycopg://u:p@db.example.com:5432/max",
        "not a url",
    ):
        with pytest.raises(
            NativeDesktopError,
            match="invalid_local_database_configuration|local_postgresql_required",
        ):
            desktop_database_url(bad_url)


def test_bootstrap_core_without_database_cannot_create_tasks() -> None:
    backend = NativeDesktopCore()
    try:
        snapshot = backend.snapshot()
        assert snapshot.core_status == "Core online (in-process)"
        assert snapshot.storage_status == "Database not configured"
        assert snapshot.tasks == () and snapshot.approvals == ()
        with pytest.raises(NativeDesktopError, match="task_store_not_ready"):
            backend.create_task("This cannot execute anything")
        with pytest.raises(NativeDesktopError, match="task_store_not_ready"):
            backend.decide_approval(str(uuid4()), True)
    finally:
        backend.close()


def test_default_from_env_initializes_local_sqlite_and_never_reuses_external_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_file = tmp_path / "local_desktop.sqlite3"
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAX_DESKTOP_DB_URL", raising=False)
    monkeypatch.setenv("MAX_LOCAL_DB_PATH", str(db_file))
    monkeypatch.setenv("MAX_API_BEARER_TOKEN", "DO-NOT-REUSE-EXTERNAL-SECRET")
    backend = NativeDesktopCore.from_env()
    try:
        assert backend._owner_token != "DO-NOT-REUSE-EXTERNAL-SECRET"
        snapshot = backend.snapshot()
        assert snapshot.core_status == "Core online (in-process)"
        assert snapshot.storage_status == "Local SQLite ready"
        assert snapshot.tasks == () and snapshot.approvals == ()
        record = backend.create_task("Local SQLite desktop task")
        assert record["objective"] == "Local SQLite desktop task"
        events = backend.timeline(record["id"])
        assert events and events[0]["kind"] == "task.queued"
    finally:
        backend.close()


def test_native_task_and_timeline_via_real_core_contracts(store: TaskStore) -> None:
    backend = NativeDesktopCore(store=store)
    try:
        # SQLite test schema has no Alembic version, so it is not a ready production store.
        with pytest.raises(NativeDesktopError, match="task_store_not_ready"):
            backend.snapshot()
        record = backend.create_task("Native UI integration smoke task")
        assert record["objective"] == "Native UI integration smoke task"
        assert record["state"] == "queued"
        assert backend._call("GET", "/v1/tasks")[0]["id"] == record["id"]
        events = backend.timeline(record["id"])
        assert events and events[0]["kind"] == "task.queued"
        with pytest.raises(NativeDesktopError, match="invalid_task_identifier"):
            backend.timeline("../etc/passwd")
    finally:
        backend.close()


def test_decide_approval_via_real_core_contracts(store: TaskStore) -> None:
    backend = NativeDesktopCore(store=store)
    try:
        task = backend.create_task("Task requiring trust approval")
        trust = TrustService(store.engine)
        grant = trust.request(
            ActorContext(
                owner_id="owner",
                task_id=task["id"],
                actor_id="desktop_owner",
                session_id="sess-1",
                device_id="local-desktop",
            ),
            ToolProposal(
                call_id="call-1",
                name="task.status",
                arguments={"task_id": task["id"]},
            ),
        )

        with pytest.raises(NativeDesktopError, match="invalid_approval_identifier"):
            backend.decide_approval("../bad-id", True)
        with pytest.raises(NativeDesktopError, match="invalid_approval_decision"):
            backend.decide_approval(grant.id, "yes")  # type: ignore[arg-type]
        with pytest.raises(NativeDesktopError, match="invalid_approval_decision"):
            backend.decide_approval(grant.id, 1)  # type: ignore[arg-type]

        decided = backend.decide_approval(grant.id, True)
        assert decided["id"] == grant.id
        assert decided["state"] == "approved"

        # Single-use decision cannot be replayed once no longer pending.
        with pytest.raises(NativeDesktopError, match="local_core_request_rejected"):
            backend.decide_approval(grant.id, False)
    finally:
        backend.close()


def test_replay_idempotency_key_is_preserved_on_ambiguous_failure(store: TaskStore) -> None:
    backend = NativeDesktopCore(store=store)
    try:
        original = backend._call
        calls: list[str | None] = []

        def flaky(
            method: str,
            path: str,
            *,
            payload: dict[str, Any] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any] | list[dict[str, Any]]:
            calls.append(idempotency_key)
            result = original(method, path, payload=payload, idempotency_key=idempotency_key)
            if len(calls) == 1:
                raise NativeDesktopError("local_core_unavailable")
            return result

        backend._call = flaky  # type: ignore[method-assign]
        with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
            backend.create_task("Retry-safe work item")
        record = backend.create_task("Retry-safe work item")
        assert record["objective"] == "Retry-safe work item"
        assert len(calls) == 2 and calls[0] == calls[1]
        assert len(store.list_recent("owner")) == 1
    finally:
        backend.close()


def test_no_network_listener_or_automatic_actions_in_backend_source() -> None:
    from inspect import getsource

    import max_core.native_desktop.backend as backend

    source = getsource(backend)
    for unexpected in (
        "uvicorn.run",
        "webbrowser.open",
        "subprocess.run",
        "os.system",
        "eval(",
        "exec(",
    ):
        assert unexpected not in source
