"""Native app in-process backend uses actual owner-scoped Core API contracts."""

from __future__ import annotations

import pytest

from max_core.native_desktop.backend import (
    NativeDesktopCore,
    NativeDesktopError,
    local_database_url,
)
from max_core.store import TaskStore


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


def test_default_launch_is_self_contained_and_cannot_create_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("MAX_API_BEARER_TOKEN", "DO-NOT-REUSE-EXTERNAL-SECRET")
    backend = NativeDesktopCore.from_env()
    try:
        snapshot = backend.snapshot()
        assert snapshot.core_status == "Core online (in-process)"
        assert snapshot.storage_status == "Database not configured"
        assert snapshot.tasks == () and snapshot.approvals == ()
        with pytest.raises(NativeDesktopError, match="task_store_not_ready"):
            backend.create_task("This cannot execute anything")
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


def test_replay_idempotency_key_is_preserved_on_ambiguous_failure(store: TaskStore) -> None:
    backend = NativeDesktopCore(store=store)
    try:
        original = backend._call
        calls: list[str | None] = []

        def flaky(
            method: str,
            path: str,
            *,
            payload: dict[str, str] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[dict[str, object]]:
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
