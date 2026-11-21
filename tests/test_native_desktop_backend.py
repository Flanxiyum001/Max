"""Native app in-process backend uses actual owner-scoped Core API contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.gateway.contracts import ToolProposal
from max_core.local_config import load_desktop_config
from max_core.models import ApprovalGrant
from max_core.native_desktop.backend import (
    ApprovalResolution,
    NativeDesktopCore,
    NativeDesktopError,
    _create_local_sqlite_store,
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


def test_database_url_validators_reject_whitespace_and_control_characters(
    tmp_path: Path,
) -> None:
    sqlite_base = f"sqlite+pysqlite:///{(tmp_path / 'desk.sqlite3').as_posix()}"
    pg_base = "postgresql+psycopg://user:pw@127.0.0.1:5432/max"
    shared_bad_urls = [
        "   ",
        "\t",
        "\n",
        f" {sqlite_base}",
        f"{sqlite_base}   ",
        f" {pg_base}",
        f"{pg_base}   ",
    ]
    for control in ("\x00", "\t", "\n", "\r", "\r\n", "\x1f", "\x7f"):
        shared_bad_urls.extend(
            [
                f"{sqlite_base}{control}",
                f"{control}{sqlite_base}",
                f"sqlite+pysqlite:///{(tmp_path / f'bad{control}desk.sqlite3').as_posix()}",
                f"{pg_base}{control}",
                f"{control}{pg_base}",
                f"postgresql+psycopg://user:pw@127.0.0.1:5432/max{control}db",
            ]
        )

    for bad_url in shared_bad_urls:
        with pytest.raises(NativeDesktopError) as desk_exc:
            desktop_database_url(bad_url)
        assert desk_exc.value.code == "invalid_local_database_configuration"

        with pytest.raises(NativeDesktopError) as pg_exc:
            local_database_url(bad_url)
        assert pg_exc.value.code == "invalid_local_database_configuration"

        with pytest.raises(NativeDesktopError) as core_exc:
            NativeDesktopCore(database_url=bad_url)
        assert core_exc.value.code == "invalid_local_database_configuration"

    sqlite_only_bad_urls = [
        "sqlite+pysqlite:///   " + (tmp_path / "desk.sqlite3").as_posix(),
        f"{sqlite_base}?",
        f"{sqlite_base}?bad",
    ]
    for bad_sqlite in sqlite_only_bad_urls:
        with pytest.raises(NativeDesktopError) as sqlite_exc:
            desktop_database_url(bad_sqlite)
        assert sqlite_exc.value.code == "invalid_local_database_configuration"

        with pytest.raises(NativeDesktopError) as core_sqlite_exc:
            NativeDesktopCore(database_url=bad_sqlite)
        assert core_sqlite_exc.value.code == "invalid_local_database_configuration"

        with pytest.raises(NativeDesktopError) as pg_sqlite_exc:
            local_database_url(bad_sqlite)
        assert pg_sqlite_exc.value.code == "local_postgresql_required"


def test_create_local_sqlite_store_rejects_control_chars_and_wraps_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sqlite_base = f"sqlite+pysqlite:///{(tmp_path / 'store.sqlite3').as_posix()}"
    for bad in (f"{sqlite_base}\x00", f"{sqlite_base}\n", f"{sqlite_base} "):
        with pytest.raises(NativeDesktopError) as exc_info:
            _create_local_sqlite_store(bad)
        assert exc_info.value.code == "invalid_local_database_configuration"

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("simulated value error")

    monkeypatch.setattr("max_core.native_desktop.backend.create_engine", _boom)
    with pytest.raises(NativeDesktopError) as wrap_exc:
        _create_local_sqlite_store(sqlite_base)
    assert wrap_exc.value.code == "task_store_not_ready"


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


def _openai_mock_transport(responses: list[dict[str, Any]]) -> httpx.MockTransport:
    queue = list(responses)

    def _handler(_request: httpx.Request) -> httpx.Response:
        if not queue:
            return httpx.Response(500, json={"error": {"message": "no queued mock response"}})
        payload = queue.pop(0)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(_handler)


def _openai_payload(
    content: str = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-backend-test",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def test_backend_chat_bridge_executes_r0_tool_and_gating_continuation(
    tmp_path: Path,
) -> None:
    notes_dir = tmp_path / "notes"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "bridge_chat.sqlite3"),
            "MAX_WORKSPACE_ROOT": str(ws_dir),
            "MAX_NOTES_DIR": str(notes_dir),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-backend-bridge-123456",
            "MAX_OPENAI_MODEL": "gpt-4o-mini",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    transport = _openai_mock_transport(
        [
            _openai_payload(
                tool_calls=[
                    {
                        "id": "call_add",
                        "type": "function",
                        "function": {
                            "name": "dev_add",
                            "arguments": json.dumps({"a": 14, "b": 28}),
                        },
                    }
                ]
            ),
            _openai_payload("The sum is 42."),
            _openai_payload(
                tool_calls=[
                    {
                        "id": "call_note",
                        "type": "function",
                        "function": {
                            "name": "notes_save",
                            "arguments": json.dumps({"title": "bridge-plan", "content": "Ship M5"}),
                        },
                    }
                ]
            ),
            _openai_payload("Saved bridge-plan after owner approval."),
        ]
    )
    core = NativeDesktopCore.from_env(config=cfg, chat_transport=transport)
    try:
        assert core.provider_status().configured is True
        assert len(core.list_tools()) == 10

        r0_turn = core.send_chat_message("Add 14 and 28")
        assert r0_turn.ok is True
        assert "42" in r0_turn.reply
        assert len(r0_turn.tool_executions) == 1
        assert r0_turn.tool_executions[0].status == "completed"
        assert r0_turn.tool_executions[0].output == {"operation": "dev.add", "result": 42}

        gated_turn = core.send_chat_message("Save note bridge-plan")
        assert gated_turn.ok is True
        assert len(gated_turn.tool_executions) == 1
        gated = gated_turn.tool_executions[0]
        assert gated.status == "awaiting_approval"
        assert gated.approval_id is not None
        assert not notes_dir.exists()

        resolution = core.decide_approval_with_continuation(gated.approval_id, True)
        assert isinstance(resolution, ApprovalResolution)
        assert resolution.approval["state"] == "approved"
        assert resolution.chat_result is not None
        assert resolution.chat_result.ok is True
        assert "Saved bridge-plan" in resolution.chat_result.reply
        assert (notes_dir / "bridge-plan.md").read_text(encoding="utf-8") == "Ship M5"

        assert core.engine is not None
        with Session(core.engine) as session:
            grant = session.scalar(
                select(ApprovalGrant).where(ApprovalGrant.id == gated.approval_id)
            )
            assert grant is not None and grant.state == "consumed"
    finally:
        core.close()


def test_backend_external_trust_grant_is_not_consumed_and_denial_skips_execution(
    tmp_path: Path,
) -> None:
    notes_dir = tmp_path / "notes"
    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "external_grant.sqlite3"),
            "MAX_WORKSPACE_ROOT": str(tmp_path),
            "MAX_NOTES_DIR": str(notes_dir),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-backend-external-123456",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    transport = _openai_mock_transport(
        [
            _openai_payload(
                tool_calls=[
                    {
                        "id": "call_deny",
                        "type": "function",
                        "function": {
                            "name": "notes_save",
                            "arguments": json.dumps({"title": "denied-note", "content": "Nope"}),
                        },
                    }
                ]
            ),
        ]
    )
    core = NativeDesktopCore.from_env(config=cfg, chat_transport=transport)
    try:
        # 1. External TrustService grant remains "approved" (not consumed).
        task = core.create_task("External grant preservation test")
        assert core.engine is not None
        ext_grant = TrustService(core.engine).request(
            ActorContext("owner", task["id"], "desktop_owner", "sess-ext", "dev-1"),
            ToolProposal(call_id="ext-1", name="task.status", arguments={"task_id": task["id"]}),
        )
        ext_res = core.decide_approval_with_continuation(ext_grant.id, True)
        assert ext_res.approval["state"] == "approved"
        assert ext_res.chat_result is None
        assert ext_res.tool_summary is None
        with Session(core.engine) as session:
            row = session.scalar(select(ApprovalGrant).where(ApprovalGrant.id == ext_grant.id))
            assert row is not None and row.state == "approved"

        # 2. Denied chat approval does not execute notes.save.
        gated_turn = core.send_chat_message("Save denied-note")
        gated_id = gated_turn.tool_executions[0].approval_id
        assert gated_id is not None
        deny_res = core.decide_approval_with_continuation(gated_id, False)
        assert deny_res.approval["state"] == "denied"
        assert deny_res.chat_result is not None
        assert deny_res.chat_result.ok is True
        assert "denied" in deny_res.chat_result.reply.lower()
        assert not (notes_dir / "denied-note.md").exists()

        # 3. Direct execute_tool gating + continuation works.
        direct = core.execute_tool("notes.save", {"title": "direct-note", "content": "Direct OK"})
        assert direct.status == "awaiting_approval" and direct.approval_id is not None
        direct_res = core.decide_approval_with_continuation(direct.approval_id, True)
        assert direct_res.tool_summary is not None
        assert direct_res.tool_summary.status == "completed"
        assert (notes_dir / "direct-note.md").read_text(encoding="utf-8") == "Direct OK"
    finally:
        core.close()


def test_backend_call_guard_rejects_bridge_calls_after_close(tmp_path: Path) -> None:
    no_db = NativeDesktopCore()
    try:
        res = no_db.execute_tool("dev.health", {})
        assert res.status == "failed"
        assert res.output == "task_store_not_ready"
    finally:
        no_db.close()

    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "guard.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = NativeDesktopCore.from_env(config=cfg)
    core.close()
    for call in (
        lambda: core.send_chat_message("hello"),
        lambda: core.execute_tool("dev.health", {}),
        lambda: core.decide_approval_with_continuation(str(uuid4()), True),
        lambda: core.resume_chat_approval(str(uuid4())),
    ):
        with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
            call()
