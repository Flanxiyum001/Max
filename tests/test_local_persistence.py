"""Local desktop config and SQLite Task Journal / Trust Inbox persistence tests."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import max_core.local_config as local_config
from max_core.gateway.contracts import ToolProposal
from max_core.local_config import (
    ProviderConfig,
    load_desktop_config,
    load_dotenv_into_mapping,
)
from max_core.models import ApprovalGrant, TrustAudit
from max_core.native_desktop.backend import (
    NativeDesktopCore,
    NativeDesktopError,
    desktop_database_url,
)
from max_core.trust import ActorContext, SafeTaskStatus, TrustError, TrustService


def test_task_journal_persists_across_core_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "max_desktop.sqlite3"
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(db_path)},
        data_dir=tmp_path / "data",
        config_file=tmp_path / "missing.json",
    )

    core1 = NativeDesktopCore.from_env(config=cfg)
    try:
        snap1 = core1.snapshot()
        assert snap1.storage_status == "Local SQLite ready"
        assert snap1.tasks == ()
        created = core1.create_task("Persist this task across restarts")
        task_id = created["id"]
    finally:
        core1.close()

    assert db_path.is_file()

    core2 = NativeDesktopCore.from_env(config=cfg)
    try:
        snap2 = core2.snapshot()
        assert snap2.storage_status == "Local SQLite ready"
        assert len(snap2.tasks) == 1
        assert snap2.tasks[0]["id"] == task_id
        assert snap2.tasks[0]["objective"] == "Persist this task across restarts"
        events = core2.timeline(task_id)
        assert len(events) >= 1
        assert events[0]["kind"] == "task.queued"
    finally:
        core2.close()


def test_trust_inbox_and_audit_persist_across_restarts(tmp_path: Path) -> None:
    db_path = tmp_path / "trust_persist.sqlite3"
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(db_path)},
        data_dir=tmp_path / "data",
        config_file=tmp_path / "missing.json",
    )

    core1 = NativeDesktopCore.from_env(config=cfg)
    try:
        task = core1.create_task("Create two pending trust approvals")
        assert core1.engine is not None
        trust = TrustService(core1.engine)
        ctx = ActorContext(
            owner_id="owner",
            task_id=task["id"],
            actor_id="desktop_owner",
            session_id="sess-persist",
            device_id="local-desktop",
        )
        grant1 = trust.request(
            ctx,
            ToolProposal(call_id="c1", name="task.status", arguments={"task_id": task["id"]}),
        )
        grant2 = trust.request(
            ctx,
            ToolProposal(call_id="c2", name="task.status", arguments={"task_id": task["id"]}),
        )
        snap1 = core1.snapshot()
        assert {item["id"] for item in snap1.approvals} == {grant1.id, grant2.id}
    finally:
        core1.close()

    core2 = NativeDesktopCore.from_env(config=cfg)
    try:
        snap2 = core2.snapshot()
        assert {item["id"] for item in snap2.approvals} == {grant1.id, grant2.id}

        approved = core2.decide_approval(grant1.id, True)
        denied = core2.decide_approval(grant2.id, False)
        assert approved["state"] == "approved"
        assert denied["state"] == "denied"
        assert core2.snapshot().approvals == ()
    finally:
        core2.close()

    core3 = NativeDesktopCore.from_env(config=cfg)
    try:
        assert core3.snapshot().approvals == ()
        assert core3.engine is not None
        with Session(core3.engine) as session:
            stored_1 = session.scalar(select(ApprovalGrant).where(ApprovalGrant.id == grant1.id))
            stored_2 = session.scalar(select(ApprovalGrant).where(ApprovalGrant.id == grant2.id))
            audits = session.scalars(select(TrustAudit)).all()
            assert stored_1 is not None and stored_1.state == "approved"
            assert stored_2 is not None and stored_2.state == "denied"
            assert len(audits) == 4
            assert {a.decision for a in audits} == {"pending", "approved", "denied"}
    finally:
        core3.close()


def test_decide_approval_unknown_id_rejected(tmp_path: Path) -> None:
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "unknown_approval.sqlite3")},
        data_dir=tmp_path,
        config_file=tmp_path / "missing.json",
    )
    core = NativeDesktopCore.from_env(config=cfg)
    try:
        with pytest.raises(NativeDesktopError, match="local_core_request_rejected"):
            core.decide_approval(str(uuid4()), True)
    finally:
        core.close()


def test_sqlite_file_handles_are_released_on_close(tmp_path: Path) -> None:
    db_path = tmp_path / "lock_release.sqlite3"
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(db_path)},
        data_dir=tmp_path,
        config_file=tmp_path / "missing.json",
    )
    core = NativeDesktopCore.from_env(config=cfg)
    core.create_task("Verify SQLite handles release cleanly")
    core.close()

    # Verify DB is unlocked on Windows by renaming and deleting DB + any WAL/SHM sidecars.
    moved = tmp_path / "moved_lock_release.sqlite3"
    db_path.rename(moved)
    moved.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    # Also ensure independent engine connection opens cleanly when closed.
    reopened = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'fresh.sqlite3').as_posix()}")
    reopened.dispose()


def test_dotenv_parsing_edge_cases(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.env"
    assert load_dotenv_into_mapping(missing) == {}
    assert load_dotenv_into_mapping(tmp_path) == {}

    env_file = tmp_path / ".env"
    env_file.write_bytes(
        (
            "\ufeff"
            "# Leading comment\n"
            "\n"
            "   \n"
            "export OPENAI_API_KEY=sk-exported # inline comment\n"
            "PLAIN_HASH=pass#word\n"
            "COMMENT_ONLY=#ignored\n"
            "EMPTY_KEY=\n"
            'DOUBLE_QUOTED="hello \\"max\\"\\nline\\t2\\\\end" # trailing comment\n'
            "SINGLE_QUOTED='literal \\n # not a comment' # trailing\n"
            "INVALID KEY=ignored\n"
            "NO_EQUALS_LINE\n"
        ).encode("utf-8")
    )
    parsed = load_dotenv_into_mapping(env_file)
    assert parsed == {
        "OPENAI_API_KEY": "sk-exported",
        "PLAIN_HASH": "pass#word",
        "COMMENT_ONLY": "",
        "EMPTY_KEY": "",
        "DOUBLE_QUOTED": 'hello "max"\nline\t2\\end',
        "SINGLE_QUOTED": "literal \\n # not a comment",
    }


def test_malformed_and_non_dict_json_config_degrades_cleanly(tmp_path: Path) -> None:
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not valid json", encoding="utf-8")
    cfg_bad = load_desktop_config(env={}, config_file=malformed, data_dir=tmp_path)
    assert cfg_bad.is_sqlite_local is True
    assert cfg_bad.active_provider is None

    non_dict = tmp_path / "list.json"
    non_dict.write_text('["not", "a", "dict"]', encoding="utf-8")
    cfg_list = load_desktop_config(env={}, config_file=non_dict, data_dir=tmp_path)
    assert cfg_list.is_sqlite_local is True
    assert cfg_list.active_provider is None


def test_config_precedence_env_over_json_over_dotenv_over_defaults(tmp_path: Path) -> None:
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=dotenv-openai-key",
                "MAX_OPENAI_MODEL=gpt-4o-mini-dotenv",
                "ANTHROPIC_API_KEY=dotenv-anthropic-key",
                "MAX_LLM_PROVIDER=anthropic",
                "MAX_COST_USD=0.02",
                f"MAX_LOCAL_DB_PATH={tmp_path / 'dotenv.sqlite3'}",
                "MAX_VOICE_LOCALE=en-US",
                "MAX_TTS_ENABLED=true",
            ]
        ),
        encoding="utf-8",
    )
    config_json = tmp_path / "config.json"
    config_json.write_text(
        json.dumps(
            {
                "active_provider": "openai",
                "max_cost_usd": 0.08,
                "local_db_path": str(tmp_path / "json.sqlite3"),
                "providers": {
                    "openai": {
                        "api_key": "json-openai-key",
                        "model_id": "gpt-4.1-mini",
                    }
                },
                "voice": {"locale": "en-AU", "tts_enabled": False},
            }
        ),
        encoding="utf-8",
    )

    # Without env overrides: JSON overrides .env, while anthropic key comes from .env.
    from_json_and_dotenv = load_desktop_config(
        env={},
        dotenv_path=dotenv_file,
        config_file=config_json,
        data_dir=tmp_path,
    )
    assert from_json_and_dotenv.active_provider is not None
    assert from_json_and_dotenv.active_provider.provider == "openai"
    assert from_json_and_dotenv.active_provider.api_key == "json-openai-key"
    assert from_json_and_dotenv.active_provider.model_id == "gpt-4.1-mini"
    assert from_json_and_dotenv.active_provider.max_cost_usd == 0.08
    assert from_json_and_dotenv.anthropic is not None
    assert from_json_and_dotenv.anthropic.api_key == "dotenv-anthropic-key"
    assert from_json_and_dotenv.database_url.endswith("json.sqlite3")
    assert from_json_and_dotenv.voice_locale == "en-AU"
    assert from_json_and_dotenv.tts_enabled_default is False

    # Env overrides JSON and .env.
    from_env = load_desktop_config(
        env={
            "OPENAI_API_KEY": "env-openai-key",
            "MAX_LLM_PROVIDER": "anthropic",
            "MAX_COST_USD": "0.15",
            "MAX_DATABASE_URL": "postgresql+psycopg://user:pw@127.0.0.1:5432/max",
            "MAX_VOICE_LOCALE": "en-GB",
            "MAX_TTS_ENABLED": "yes",
        },
        dotenv_path=dotenv_file,
        config_file=config_json,
        data_dir=tmp_path,
    )
    assert from_env.openai is not None and from_env.openai.api_key == "env-openai-key"
    assert from_env.active_provider is not None
    assert from_env.active_provider.provider == "anthropic"
    assert from_env.active_provider.max_cost_usd == 0.15
    assert from_env.is_sqlite_local is False
    assert from_env.database_url == "postgresql+psycopg://user:pw@127.0.0.1:5432/max"
    assert from_env.voice_locale == "en-GB"
    assert from_env.tts_enabled_default is True


def test_explicit_env_mapping_does_not_auto_read_cwd_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=cwd-leak-key\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    isolated = load_desktop_config(
        env={},
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert isolated.openai is None
    assert isolated.active_provider is None


def test_missing_and_invalid_keys_never_raise_and_secrets_redacted_in_repr(tmp_path: Path) -> None:
    cfg_missing = load_desktop_config(
        env={"MAX_LLM_PROVIDER": "anthropic", "OPENAI_API_KEY": "line1\nline2"},
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg_missing.openai is None
    assert cfg_missing.anthropic is None
    assert cfg_missing.active_provider is None

    # Explicit MAX_LLM_PROVIDER="anthropic" stays None even if OpenAI is configured.
    cfg_explicit_missing = load_desktop_config(
        env={"MAX_LLM_PROVIDER": "anthropic", "OPENAI_API_KEY": "sk-secret-openai-999"},
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg_explicit_missing.openai is not None
    assert cfg_explicit_missing.active_provider is None

    secret_openai = "sk-very-secret-openai-token-12345"
    secret_anthropic = "sk-ant-very-secret-token-67890"
    cfg = load_desktop_config(
        env={
            "OPENAI_API_KEY": secret_openai,
            "ANTHROPIC_API_KEY": secret_anthropic,
            "MAX_LOCAL_DB_PATH": str(tmp_path / "secret_db_name.sqlite3"),
        },
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert secret_openai not in repr(cfg)
    assert secret_anthropic not in repr(cfg)
    assert "secret_db_name.sqlite3" not in repr(cfg)
    assert cfg.openai is not None and secret_openai not in repr(cfg.openai)
    assert cfg.anthropic is not None and secret_anthropic not in repr(cfg.anthropic)
    api_key_field = next(f for f in dataclasses.fields(ProviderConfig) if f.name == "api_key")
    assert api_key_field.repr is False


def test_default_sqlite_path_is_outside_git_repo_and_creates_no_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fake_localappdata = tmp_path / "NonExistentAppData"
    monkeypatch.setattr(local_config, "_is_windows", lambda: True)
    cfg_win = load_desktop_config(
        env={"LOCALAPPDATA": str(fake_localappdata)},
        config_file=tmp_path / "missing.json",
    )
    assert cfg_win.is_sqlite_local is True
    assert cfg_win.database_url.startswith("sqlite+pysqlite:///")
    assert cfg_win.database_url.endswith("/MAX/max_desktop.sqlite3")
    assert not fake_localappdata.exists()
    db_path_win = Path(cfg_win.database_url.removeprefix("sqlite+pysqlite:///"))
    assert not db_path_win.is_relative_to(repo_root)

    fake_xdg = (tmp_path / "xdg_data").resolve()
    monkeypatch.setattr(local_config, "_is_windows", lambda: False)
    cfg_posix = load_desktop_config(
        env={"XDG_DATA_HOME": str(fake_xdg)},
        config_file=tmp_path / "missing.json",
    )
    assert cfg_posix.is_sqlite_local is True
    assert cfg_posix.database_url.endswith("/max/max_desktop.sqlite3")
    assert not fake_xdg.exists()


def test_env_local_db_path_overrides_dotenv_database_url(tmp_path: Path) -> None:
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "MAX_DATABASE_URL=postgresql+psycopg://u:p@127.0.0.1:5432/max\n",
        encoding="utf-8",
    )
    local_db = tmp_path / "override.sqlite3"
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(local_db)},
        dotenv_path=dotenv_file,
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg.is_sqlite_local is True
    assert cfg.database_url == f"sqlite+pysqlite:///{local_db.resolve().as_posix()}"


def test_postgresql_override_preserved_and_remote_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_pg = "postgresql+psycopg://u:p@127.0.0.1:5432/max"
    cfg_pg = load_desktop_config(
        env={"MAX_DATABASE_URL": local_pg, "MAX_LOCAL_DB_PATH": str(tmp_path / "ignored.sqlite3")},
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg_pg.is_sqlite_local is False
    assert cfg_pg.database_url == local_pg
    assert desktop_database_url(cfg_pg) == local_pg

    monkeypatch.setenv("MAX_DATABASE_URL", "postgresql+psycopg://u:p@remote.example.com:5432/max")
    with pytest.raises(NativeDesktopError, match="local_postgresql_required"):
        NativeDesktopCore.from_env()


def test_clean_secret_rejects_all_ascii_control_characters(tmp_path: Path) -> None:
    for control in ("\x00", "\t", "\n", "\r", "\x1f", "\x7f"):
        cfg = load_desktop_config(
            env={
                "OPENAI_API_KEY": f"sk-bad{control}key",
                "ANTHROPIC_API_KEY": f"sk-ant{control}key",
            },
            config_file=tmp_path / "missing.json",
            data_dir=tmp_path,
        )
        assert cfg.openai is None
        assert cfg.anthropic is None
        assert cfg.active_provider is None


def test_concurrent_decide_approval_allows_exactly_one_decision(tmp_path: Path) -> None:
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "concurrent_decide.sqlite3")},
        data_dir=tmp_path,
        config_file=tmp_path / "missing.json",
    )
    core = NativeDesktopCore.from_env(config=cfg)
    try:
        core.snapshot()
        task = core.create_task("Concurrent trust decision test")
        assert core.engine is not None
        trust = TrustService(core.engine)
        ctx = ActorContext(
            owner_id="owner",
            task_id=task["id"],
            actor_id="desktop_owner",
            session_id="sess-race-decide",
            device_id="local-desktop",
        )

        for trial in range(15):
            grant = trust.request(
                ctx,
                ToolProposal(
                    call_id=f"decide-call-{trial}",
                    name="task.status",
                    arguments={"task_id": task["id"]},
                ),
            )
            barrier = threading.Barrier(4)

            def _attempt(
                idx: int,
                grant_id: str = grant.id,
                sync: threading.Barrier = barrier,
            ) -> tuple[str, str]:
                sync.wait(timeout=5.0)
                try:
                    decided = core.decide_approval(grant_id, idx % 2 == 0)
                    return ("ok", str(decided["state"]))
                except NativeDesktopError as exc:
                    return ("err", exc.code)

            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(_attempt, range(4)))

            successes = [item for item in outcomes if item[0] == "ok"]
            failures = [item for item in outcomes if item[0] == "err"]
            assert len(successes) == 1
            assert successes[0][1] in {"approved", "denied"}
            assert failures == [("err", "local_core_request_rejected")] * 3

            with Session(core.engine) as session:
                stored = session.scalar(select(ApprovalGrant).where(ApprovalGrant.id == grant.id))
                assert stored is not None
                assert stored.state == successes[0][1]
                decision_audits = session.scalars(
                    select(TrustAudit).where(
                        TrustAudit.approval_id == grant.id,
                        TrustAudit.action == "decision",
                    )
                ).all()
                owner_decisions = [a for a in decision_audits if a.reason == "owner_decision"]
                rejected_decisions = [
                    a for a in decision_audits if a.reason == "approval_not_pending"
                ]
                assert len(owner_decisions) == 1
                assert owner_decisions[0].decision == successes[0][1]
                assert len(rejected_decisions) == 3
                assert all(a.decision == "deny" for a in rejected_decisions)
    finally:
        core.close()


def test_concurrent_trust_execute_consumes_single_use_grant_once(tmp_path: Path) -> None:
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "concurrent_execute.sqlite3")},
        data_dir=tmp_path,
        config_file=tmp_path / "missing.json",
    )
    core = NativeDesktopCore.from_env(config=cfg)
    try:
        core.snapshot()
        task = core.create_task("Concurrent trust execute test")
        assert core.engine is not None
        trust = TrustService(core.engine)
        ctx = ActorContext(
            owner_id="owner",
            task_id=task["id"],
            actor_id="desktop_owner",
            session_id="sess-race-exec",
            device_id="local-desktop",
        )

        for trial in range(10):
            proposal = ToolProposal(
                call_id=f"exec-call-{trial}",
                name="task.status",
                arguments={"task_id": task["id"]},
            )
            grant = trust.request(ctx, proposal)
            decided = core.decide_approval(grant.id, True)
            assert decided["state"] == "approved"

            barrier = threading.Barrier(4)

            def _consume(
                _idx: int,
                grant_id: str = grant.id,
                prop: ToolProposal = proposal,
                sync: threading.Barrier = barrier,
            ) -> tuple[str, str]:
                sync.wait(timeout=5.0)
                try:
                    status = trust.execute(ctx, grant_id, prop)
                    assert isinstance(status, SafeTaskStatus)
                    return ("allow", status.task_id)
                except TrustError as exc:
                    return ("deny", exc.code)

            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(_consume, range(4)))

            allows = [item for item in outcomes if item[0] == "allow"]
            denies = [item for item in outcomes if item[0] == "deny"]
            assert allows == [("allow", task["id"])]
            assert denies == [("deny", "approval_not_active")] * 3

            with Session(core.engine) as session:
                stored = session.scalar(select(ApprovalGrant).where(ApprovalGrant.id == grant.id))
                assert stored is not None
                assert stored.state == "consumed"
                exec_audits = session.scalars(
                    select(TrustAudit).where(
                        TrustAudit.approval_id == grant.id,
                        TrustAudit.action == "execute",
                    )
                ).all()
                allowed_audits = [a for a in exec_audits if a.decision == "allow"]
                denied_audits = [a for a in exec_audits if a.decision == "deny"]
                assert len(allowed_audits) == 1
                assert allowed_audits[0].reason == "single_use_read"
                assert len(denied_audits) == 3
                assert all(a.reason == "approval_not_active" for a in denied_audits)
    finally:
        core.close()


def test_concurrent_create_task_is_thread_safe_and_isolated(tmp_path: Path) -> None:
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "concurrent_tasks.sqlite3")},
        data_dir=tmp_path,
        config_file=tmp_path / "missing.json",
    )
    core = NativeDesktopCore.from_env(config=cfg)
    try:
        barrier = threading.Barrier(8)

        def _create(idx: int) -> tuple[str, str]:
            barrier.wait(timeout=5.0)
            created = core.create_task(f"Concurrent objective {idx}")
            return str(created["id"]), str(created["objective"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            created_pairs = list(pool.map(_create, range(8)))

        assert len({task_id for task_id, _ in created_pairs}) == 8
        assert {obj for _, obj in created_pairs} == {f"Concurrent objective {i}" for i in range(8)}
        assert len(core.snapshot().tasks) == 8
    finally:
        core.close()


def test_close_drains_in_flight_call_and_rejects_new_calls_deterministically(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "deterministic_drain.sqlite3"
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(db_path)},
        data_dir=tmp_path,
        config_file=tmp_path / "missing.json",
    )
    core = NativeDesktopCore.from_env(config=cfg)
    seed = core.create_task("In-flight drain seed")

    original_call = core._call
    in_flight_entered = threading.Event()
    release_in_flight = threading.Event()
    pause_lock = threading.Lock()
    paused_once = False

    def gated_call(
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        nonlocal paused_once
        should_pause = False
        if method == "GET" and path == "/v1/tasks":
            with pause_lock:
                if not paused_once:
                    paused_once = True
                    should_pause = True
        if should_pause:
            in_flight_entered.set()
            assert release_in_flight.wait(timeout=5.0)
        return original_call(method, path, payload=payload, idempotency_key=idempotency_key)

    core._call = gated_call  # type: ignore[method-assign]

    close_finished = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshot_future = pool.submit(core.snapshot)
        assert in_flight_entered.wait(timeout=5.0)

        close_future = pool.submit(lambda: (core.close(), close_finished.set()))

        # Give close() a brief moment to mark _closed=True and enter wait().
        deadline = time.monotonic() + 2.0
        rejected = False
        while time.monotonic() < deadline:
            try:
                core.timeline(seed["id"])
            except NativeDesktopError as exc:
                assert exc.code == "local_core_unavailable"
                rejected = True
                break
            time.sleep(0.005)

        assert rejected is True
        assert not close_finished.is_set()

        release_in_flight.set()
        snap = snapshot_future.result(timeout=5.0)
        assert len(snap.tasks) == 1
        close_future.result(timeout=5.0)
        assert close_finished.is_set()

    for call in (
        lambda: core.snapshot(),
        lambda: core.create_task("after close"),
        lambda: core.timeline(seed["id"]),
        lambda: core.decide_approval(str(uuid4()), True),
    ):
        with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
            call()

    moved = tmp_path / "moved_deterministic_drain.sqlite3"
    db_path.rename(moved)
    moved.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def test_close_under_concurrent_load_releases_sqlite_handles_on_windows(
    tmp_path: Path,
) -> None:
    for trial in range(10):
        db_path = tmp_path / f"close_load_{trial}.sqlite3"
        cfg = load_desktop_config(
            env={"MAX_LOCAL_DB_PATH": str(db_path)},
            data_dir=tmp_path,
            config_file=tmp_path / "missing.json",
        )
        core = NativeDesktopCore.from_env(config=cfg)
        seed = core.create_task(f"Seed task {trial}")
        stop_event = threading.Event()
        workers_active = threading.Barrier(5)

        def _worker(
            worker_id: int,
            c: NativeDesktopCore = core,
            seed_id: str = seed["id"],
            stop: threading.Event = stop_event,
            ready: threading.Barrier = workers_active,
        ) -> int:
            ready.wait(timeout=5.0)
            iterations = 0
            while not stop.is_set():
                try:
                    c.snapshot()
                    created = c.create_task(f"Load task w{worker_id}-{iterations}")
                    c.timeline(str(created["id"]))
                    c.timeline(seed_id)
                    iterations += 1
                except NativeDesktopError as exc:
                    assert exc.code == "local_core_unavailable"
                    break
            return iterations

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_worker, idx) for idx in range(4)]
            workers_active.wait(timeout=5.0)
            time.sleep(0.02)

            close_futs = [pool.submit(core.close) for _ in range(2)]
            stop_event.set()
            for close_fut in close_futs:
                close_fut.result(timeout=15.0)
            for fut in futures:
                fut.result(timeout=15.0)

        with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
            core.snapshot()
        with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
            core.create_task("Post close rejected")
        with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
            core.timeline(seed["id"])
        with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
            core.decide_approval(str(uuid4()), True)

        moved = tmp_path / f"moved_close_load_{trial}.sqlite3"
        db_path.rename(moved)
        moved.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                sidecar.unlink()


def test_from_env_rejects_control_chars_and_null_bytes_without_raw_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sqlite_base = f"sqlite+pysqlite:///{(tmp_path / 'bad.sqlite3').as_posix()}"
    pg_base = "postgresql+psycopg://u:p@127.0.0.1:5432/max"

    # 1. config.json database_url and local_db_path containing NUL and ASCII control chars.
    for idx, control in enumerate(("\x00", "\n", "\r", "\r\n", "\t", "\x1f", "\x7f")):
        cfg_file = tmp_path / f"bad_url_{idx}.json"
        cfg_file.write_text(
            json.dumps({"database_url": f"{sqlite_base}{control}"}),
            encoding="utf-8",
        )
        cfg = load_desktop_config(env={}, config_file=cfg_file, data_dir=tmp_path)
        with pytest.raises(NativeDesktopError) as exc_info:
            NativeDesktopCore.from_env(config=cfg)
        assert exc_info.value.code == "invalid_local_database_configuration"

        cfg_pg_file = tmp_path / f"bad_pg_{idx}.json"
        cfg_pg_file.write_text(
            json.dumps({"database_url": f"{pg_base}{control}"}),
            encoding="utf-8",
        )
        cfg_pg = load_desktop_config(env={}, config_file=cfg_pg_file, data_dir=tmp_path)
        with pytest.raises(NativeDesktopError) as pg_exc:
            NativeDesktopCore.from_env(config=cfg_pg)
        assert pg_exc.value.code == "invalid_local_database_configuration"

        cfg_path_file = tmp_path / f"bad_path_{idx}.json"
        cfg_path_file.write_text(
            json.dumps({"local_db_path": str(tmp_path / f"bad{control}.sqlite3")}),
            encoding="utf-8",
        )
        cfg_path = load_desktop_config(env={}, config_file=cfg_path_file, data_dir=tmp_path)
        with pytest.raises(NativeDesktopError) as path_exc:
            NativeDesktopCore.from_env(config=cfg_path)
        assert path_exc.value.code == "invalid_local_database_configuration"

    # 2. Explicit env mapping with NUL / control characters in MAX_DATABASE_URL / MAX_LOCAL_DB_PATH.
    for control in ("\x00", "\n", "\r\n", "\t", "\x1f", "\x7f"):
        cfg_env_url = load_desktop_config(
            env={"MAX_DATABASE_URL": f"{sqlite_base}{control}"},
            config_file=tmp_path / "missing.json",
            data_dir=tmp_path,
        )
        with pytest.raises(NativeDesktopError) as env_exc:
            NativeDesktopCore.from_env(config=cfg_env_url)
        assert env_exc.value.code == "invalid_local_database_configuration"

        cfg_env_path = load_desktop_config(
            env={"MAX_LOCAL_DB_PATH": str(tmp_path / f"env{control}.sqlite3")},
            config_file=tmp_path / "missing.json",
            data_dir=tmp_path,
        )
        with pytest.raises(NativeDesktopError) as env_path_exc:
            NativeDesktopCore.from_env(config=cfg_env_path)
        assert env_path_exc.value.code == "invalid_local_database_configuration"

    # 3. Direct os.environ via monkeypatch + NativeDesktopCore.from_env().
    monkeypatch.delenv("MAX_DESKTOP_DB_URL", raising=False)
    monkeypatch.delenv("MAX_LOCAL_DB_PATH", raising=False)
    for control in ("\n", "\r\n", "\t", "\x1f", "\x7f"):
        monkeypatch.setenv("MAX_DATABASE_URL", f"{sqlite_base}{control}")
        with pytest.raises(NativeDesktopError) as direct_exc:
            NativeDesktopCore.from_env()
        assert direct_exc.value.code == "invalid_local_database_configuration"


def test_whitespace_only_env_api_key_falls_through_while_control_chars_fail_closed(
    tmp_path: Path,
) -> None:
    cfg_file = tmp_path / "keys.json"
    cfg_file.write_text(
        json.dumps({"openai": {"api_key": "sk-from-json-fallback"}}),
        encoding="utf-8",
    )

    cfg_spaces = load_desktop_config(
        env={"OPENAI_API_KEY": "   "},
        config_file=cfg_file,
        data_dir=tmp_path,
    )
    assert cfg_spaces.openai is not None
    assert cfg_spaces.openai.api_key == "sk-from-json-fallback"

    cfg_newline = load_desktop_config(
        env={"OPENAI_API_KEY": "\n"},
        config_file=cfg_file,
        data_dir=tmp_path,
    )
    assert cfg_newline.openai is None
    assert cfg_newline.active_provider is None


def test_sqlite_journal_is_private_and_rejects_insecure_existing_files(
    tmp_path: Path,
) -> None:
    """Task, Trust and memory records must not default to world-readable files."""
    if os.name != "posix":
        pytest.skip("Unix owner and filesystem mode checks")
    path = tmp_path / "secure_storage" / "journal.sqlite3"
    url = f"sqlite+pysqlite:///{path.as_posix()}"
    core = NativeDesktopCore(database_url=url)
    try:
        core.create_task("Personal task record")
    finally:
        core.close()
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0

    path.chmod(0o644)
    with pytest.raises(NativeDesktopError, match="unsafe_sqlite_permissions"):
        NativeDesktopCore(database_url=url)

    path.chmod(0o600)
    recovered = NativeDesktopCore(database_url=url)
    try:
        assert len(recovered.snapshot().tasks) == 1
    finally:
        recovered.close()


def test_sqlite_db_symlink_is_rejected_before_schema_access(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX symlink tests")
    outside = tmp_path / "outside.json"
    outside.write_text('{"important":"private"}', encoding="utf-8")
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(outside)
    with pytest.raises(NativeDesktopError, match="unsafe_sqlite_path"):
        NativeDesktopCore(database_url=f"sqlite+pysqlite:///{linked.as_posix()}")
    assert outside.read_text(encoding="utf-8") == '{"important":"private"}'
