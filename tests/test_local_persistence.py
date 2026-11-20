"""Local desktop config and SQLite Task Journal / Trust Inbox persistence tests."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
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
from max_core.trust import ActorContext, TrustService


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
