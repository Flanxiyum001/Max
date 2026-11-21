"""Active M1 E2E tests for local config (F1), SQLite persistence & Trust (F2), and Desktop UI/CLI (F3)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
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
from tests.e2e.conftest import REPO_ROOT, build_clean_desktop_env


def _pump_until(
    app: Any,
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    assert predicate(), "Timed out waiting for Qt UI state update"


# ---------------------------------------------------------------------------
# Tier 1 — F1: Local Config & Cloud Provider Setup
# ---------------------------------------------------------------------------


def test_t1_f1_01_default_config_without_keys_creates_no_dirs(tmp_path: Path) -> None:
    """T1-F1-01: Default config without API keys or files degrades cleanly and creates no dirs."""
    data_dir = tmp_path / "nonexistent_data_dir"
    cfg = load_desktop_config(
        env={},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=data_dir,
    )
    assert cfg.is_sqlite_local is True
    assert cfg.database_url.startswith("sqlite+pysqlite:///")
    assert cfg.database_url.endswith("max_desktop.sqlite3")
    assert cfg.active_provider is None
    assert cfg.openai is None
    assert cfg.anthropic is None
    assert cfg.voice_locale == "en-GB"
    assert cfg.voice_presentation == "feminine"
    assert cfg.tts_enabled_default is False
    assert not data_dir.exists()


def test_t1_f1_02_dotenv_configures_openai_without_source_edits(tmp_path: Path) -> None:
    """T1-F1-02: .env file configures OpenAI provider, model, and cost limit without code edits."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=sk-e2e-dotenv-openai-12345",
                "MAX_OPENAI_MODEL=gpt-4.1-mini",
                "MAX_COST_USD=0.04",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_desktop_config(
        env={},
        dotenv_path=dotenv_file,
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path / "data",
    )
    assert cfg.openai is not None
    assert cfg.active_provider is not None
    assert cfg.active_provider.provider == "openai"
    assert cfg.active_provider.model_id == "gpt-4.1-mini"
    assert cfg.active_provider.api_key == "sk-e2e-dotenv-openai-12345"
    assert cfg.active_provider.max_cost_usd == 0.04


def test_t1_f1_03_json_config_configures_anthropic_provider(tmp_path: Path) -> None:
    """T1-F1-03: config.json configures Anthropic as active provider."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "active_provider": "anthropic",
                "providers": {
                    "anthropic": {
                        "api_key": "sk-ant-e2e-json-99999",
                        "model_id": "claude-sonnet-4-5",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_desktop_config(
        env={},
        dotenv_path=tmp_path / "missing.env",
        config_file=cfg_file,
        data_dir=tmp_path / "data",
    )
    assert cfg.anthropic is not None
    assert cfg.active_provider is not None
    assert cfg.active_provider.provider == "anthropic"
    assert cfg.active_provider.model_id == "claude-sonnet-4-5"
    assert cfg.active_provider.api_key == "sk-ant-e2e-json-99999"


def test_t1_f1_04_config_precedence_env_over_json_over_dotenv_in_subprocess(
    tmp_path: Path,
    run_desktop_subprocess: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """T1-F1-04: Fresh subprocess enforces env > config.json > .env > defaults precedence."""
    (tmp_path / ".env").write_text(
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
    json_path = tmp_path / "config.json"
    json_path.write_text(
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

    script = (
        "import json; "
        "from max_core.local_config import load_desktop_config; "
        "c = load_desktop_config(); "
        "print(json.dumps({"
        "'active': c.active_provider.provider if c.active_provider else None, "
        "'model': c.active_provider.model_id if c.active_provider else None, "
        "'cost': c.active_provider.max_cost_usd if c.active_provider else None, "
        "'key': c.active_provider.api_key if c.active_provider else None, "
        "'db': c.database_url, "
        "'locale': c.voice_locale, "
        "'tts': c.tts_enabled_default}))"
    )

    # Layer 1: config.json overrides .env when env vars are absent.
    env_json = build_clean_desktop_env(tmp_path, overrides={"MAX_CONFIG_PATH": str(json_path)})
    res_json = run_desktop_subprocess(["-c", script], env=env_json, cwd=tmp_path)
    assert res_json.returncode == 0, res_json.stderr
    parsed_json = json.loads(res_json.stdout.strip())
    assert parsed_json["active"] == "openai"
    assert parsed_json["model"] == "gpt-4.1-mini"
    assert parsed_json["cost"] == 0.08
    assert parsed_json["key"] == "json-openai-key"
    assert parsed_json["db"].endswith("json.sqlite3")
    assert parsed_json["locale"] == "en-AU"
    assert parsed_json["tts"] is False

    # Layer 2: environment variables override config.json and .env.
    env_override = build_clean_desktop_env(
        tmp_path,
        overrides={
            "MAX_CONFIG_PATH": str(json_path),
            "MAX_LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "env-anthropic-key",
            "MAX_COST_USD": "0.15",
            "MAX_LOCAL_DB_PATH": str(tmp_path / "env.sqlite3"),
            "MAX_VOICE_LOCALE": "en-GB",
            "MAX_TTS_ENABLED": "true",
        },
    )
    res_env = run_desktop_subprocess(["-c", script], env=env_override, cwd=tmp_path)
    assert res_env.returncode == 0, res_env.stderr
    parsed_env = json.loads(res_env.stdout.strip())
    assert parsed_env["active"] == "anthropic"
    assert parsed_env["key"] == "env-anthropic-key"
    assert parsed_env["cost"] == 0.15
    assert parsed_env["db"].endswith("env.sqlite3")
    assert parsed_env["locale"] == "en-GB"
    assert parsed_env["tts"] is True


def test_t1_f1_05_provider_selection_auto_and_explicit_anthropic(tmp_path: Path) -> None:
    """T1-F1-05: Auto mode selects OpenAI when both keys exist; explicit selects Anthropic."""
    both_keys = {
        "OPENAI_API_KEY": "sk-openai-both",
        "ANTHROPIC_API_KEY": "sk-ant-both",
    }
    cfg_auto = load_desktop_config(
        env=both_keys,
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg_auto.active_provider is not None
    assert cfg_auto.active_provider.provider == "openai"

    cfg_anth = load_desktop_config(
        env={**both_keys, "MAX_LLM_PROVIDER": "anthropic"},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg_anth.active_provider is not None
    assert cfg_anth.active_provider.provider == "anthropic"


# ---------------------------------------------------------------------------
# Tier 2 — F1: Local Config Boundary & Negative Cases
# ---------------------------------------------------------------------------


def test_t2_f1_01_missing_keys_and_explicit_provider_mismatch_never_raise(tmp_path: Path) -> None:
    """T2-F1-01: Explicit provider without matching key yields active_provider=None cleanly."""
    cfg = load_desktop_config(
        env={"MAX_LLM_PROVIDER": "anthropic", "OPENAI_API_KEY": "sk-only-openai"},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg.openai is not None
    assert cfg.anthropic is None
    assert cfg.active_provider is None


def test_t2_f1_02_malformed_json_and_missing_dotenv_degrade_to_defaults(tmp_path: Path) -> None:
    """T2-F1-02: Malformed JSON, non-dict JSON, and directory .env degrade safely."""
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{bad json", encoding="utf-8")
    cfg_bad = load_desktop_config(
        env={},
        dotenv_path=tmp_path,
        config_file=malformed,
        data_dir=tmp_path,
    )
    assert cfg_bad.is_sqlite_local is True
    assert cfg_bad.active_provider is None

    list_json = tmp_path / "list.json"
    list_json.write_text('["not", "a", "dict"]', encoding="utf-8")
    cfg_list = load_desktop_config(
        env={},
        dotenv_path=tmp_path / "absent.env",
        config_file=list_json,
        data_dir=tmp_path,
    )
    assert cfg_list.is_sqlite_local is True
    assert cfg_list.active_provider is None


def test_t2_f1_03_control_chars_in_api_keys_rejected_fail_closed(tmp_path: Path) -> None:
    """T2-F1-03: ASCII control chars in API keys fail closed to None without falling through."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("OPENAI_API_KEY=sk-valid-fallback\n", encoding="utf-8")
    for ctrl in ("\x00", "\n", "\r", "\t", "\x1f", "\x7f"):
        cfg = load_desktop_config(
            env={
                "OPENAI_API_KEY": f"sk-bad{ctrl}key",
                "ANTHROPIC_API_KEY": f"sk-ant{ctrl}key",
            },
            dotenv_path=dotenv_file,
            config_file=tmp_path / "missing.json",
            data_dir=tmp_path,
        )
        assert cfg.openai is None
        assert cfg.anthropic is None
        assert cfg.active_provider is None


def test_t2_f1_04_secrets_and_db_paths_redacted_from_repr(tmp_path: Path) -> None:
    """T2-F1-04: API keys and DB filename never appear in repr() outputs."""
    secret_openai = "sk-e2e-secret-openai-token-987654"
    secret_anthropic = "sk-ant-e2e-secret-token-123456"
    cfg = load_desktop_config(
        env={
            "OPENAI_API_KEY": secret_openai,
            "ANTHROPIC_API_KEY": secret_anthropic,
            "MAX_LOCAL_DB_PATH": str(tmp_path / "private_vault_db.sqlite3"),
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert secret_openai not in repr(cfg)
    assert secret_anthropic not in repr(cfg)
    assert "private_vault_db.sqlite3" not in repr(cfg)
    assert cfg.openai is not None and secret_openai not in repr(cfg.openai)
    assert cfg.anthropic is not None and secret_anthropic not in repr(cfg.anthropic)
    api_key_field = next(f for f in dataclasses.fields(ProviderConfig) if f.name == "api_key")
    assert api_key_field.repr is False


def test_t2_f1_05_dotenv_bom_comments_escapes_and_explicit_env_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """T2-F1-05: Dotenv parser handles BOM/comments/escapes; explicit env skips CWD .env."""
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        (
            "\ufeff"
            "# Header\n"
            "export OPENAI_API_KEY=sk-bom-export # inline comment\n"
            "PLAIN_HASH=a#b\n"
            'DOUBLE_QUOTED="line1\\n\\t\\"ok\\"" # comment\n'
            "SINGLE_QUOTED='raw \\n # keep'\n"
            "BAD KEY=ignored\n"
        ).encode("utf-8")
    )
    parsed = load_dotenv_into_mapping(env_file)
    assert parsed == {
        "OPENAI_API_KEY": "sk-bom-export",
        "PLAIN_HASH": "a#b",
        "DOUBLE_QUOTED": 'line1\n\t"ok"',
        "SINGLE_QUOTED": "raw \\n # keep",
    }

    monkeypatch.chdir(tmp_path)
    cfg = load_desktop_config(env={}, config_file=tmp_path / "missing.json", data_dir=tmp_path)
    assert cfg.openai is None
    assert cfg.active_provider is None


def test_t2_f1_06_invalid_cost_and_voice_prefs_fallback_safely(tmp_path: Path) -> None:
    """T2-F1-06: Invalid MAX_COST_USD, MAX_VOICE_LOCALE, and MAX_VOICE_PRESENTATION fall back."""
    for bad_cost in ("not-a-number", "-1.0", "nan", "inf", "-inf"):
        cfg = load_desktop_config(
            env={
                "OPENAI_API_KEY": "sk-valid-12345",
                "MAX_COST_USD": bad_cost,
                "MAX_VOICE_LOCALE": "bad_loc",
                "MAX_VOICE_PRESENTATION": "masculine",
            },
            dotenv_path=tmp_path / "missing.env",
            config_file=tmp_path / "missing.json",
            data_dir=tmp_path,
        )
        assert cfg.active_provider is not None
        assert cfg.active_provider.max_cost_usd == 0.05
        assert cfg.voice_locale == "en-GB"
        assert cfg.voice_presentation == "feminine"

    cfg_zero = load_desktop_config(
        env={"OPENAI_API_KEY": "sk-valid-12345", "MAX_COST_USD": "0"},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg_zero.active_provider is not None
    assert cfg_zero.active_provider.max_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Tier 1 — F2: Local Persistence & Restart Continuity
# ---------------------------------------------------------------------------


def test_t1_f2_01_default_localappdata_sqlite_initializes_and_persists_across_subprocesses(
    tmp_path: Path,
    clean_desktop_env: dict[str, str],
    run_desktop_subprocess: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """T1-F2-01: Default local data dir SQLite DB initializes outside repo & persists tasks."""
    sub_a = (
        "import json; "
        "from max_core.native_desktop.backend import NativeDesktopCore; "
        "c = NativeDesktopCore.from_env(); "
        "s = c.snapshot(); "
        "t = c.create_task('Subprocess default DB task'); "
        "c.close(); "
        "print(json.dumps({'status': s.storage_status, 'core': s.core_status, 'id': t['id']}))"
    )
    res_a = run_desktop_subprocess(["-c", sub_a], env=clean_desktop_env, cwd=tmp_path)
    assert res_a.returncode == 0, res_a.stderr
    out_a = json.loads(res_a.stdout.strip())
    assert out_a["core"] == "Core online (in-process)"
    assert out_a["status"] == "Local SQLite ready"

    expected_db = (
        Path(clean_desktop_env["LOCALAPPDATA"]) / "MAX" / "max_desktop.sqlite3"
        if local_config._is_windows()
        else Path(clean_desktop_env["XDG_DATA_HOME"]) / "max" / "max_desktop.sqlite3"
    )
    assert expected_db.is_file()
    assert not expected_db.resolve().is_relative_to(REPO_ROOT)

    sub_b = (
        "import json; "
        "from max_core.native_desktop.backend import NativeDesktopCore; "
        "c = NativeDesktopCore.from_env(); "
        "s = c.snapshot(); "
        "ev = c.timeline(s.tasks[0]['id']); "
        "c.close(); "
        "print(json.dumps({'tasks': s.tasks, 'events': ev}))"
    )
    res_b = run_desktop_subprocess(["-c", sub_b], env=clean_desktop_env, cwd=tmp_path)
    assert res_b.returncode == 0, res_b.stderr
    out_b = json.loads(res_b.stdout.strip())
    assert len(out_b["tasks"]) == 1
    assert out_b["tasks"][0]["id"] == out_a["id"]
    assert out_b["tasks"][0]["objective"] == "Subprocess default DB task"
    assert out_b["events"][0]["kind"] == "task.queued"


def test_t1_f2_02_task_journal_and_timeline_persist_across_core_and_process_restarts(
    tmp_path: Path,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T1-F2-02: Task journal and timeline persist across NativeDesktopCore close and reopen."""
    db_path = tmp_path / "nested" / "journal.sqlite3"
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(db_path)},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core1 = desktop_core_factory(config=cfg)
    created = core1.create_task("Review weekly notes")
    core1.close()

    core2 = desktop_core_factory(config=cfg)
    snap2 = core2.snapshot()
    assert snap2.storage_status == "Local SQLite ready"
    assert len(snap2.tasks) == 1
    assert snap2.tasks[0]["id"] == created["id"]
    assert snap2.tasks[0]["objective"] == "Review weekly notes"
    events = core2.timeline(created["id"])
    assert len(events) >= 1
    assert events[0]["kind"] == "task.queued"


def test_t1_f2_03_trust_inbox_approve_persists_across_three_subprocesses(
    tmp_path: Path,
    clean_desktop_env: dict[str, str],
    run_desktop_subprocess: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """T1-F2-03: Pending Trust grant survives restart, approves across subprocess, and audits."""
    db_path = tmp_path / "trust_3proc.sqlite3"
    env = {**clean_desktop_env, "MAX_LOCAL_DB_PATH": str(db_path)}

    proc1 = (
        "import json; "
        "from max_core.gateway.contracts import ToolProposal; "
        "from max_core.native_desktop.backend import NativeDesktopCore; "
        "from max_core.trust import ActorContext, TrustService; "
        "c = NativeDesktopCore.from_env(); "
        "t = c.create_task('Trust approval subprocess check'); "
        "ts = TrustService(c.engine); "
        "ctx = ActorContext('owner', t['id'], 'desktop_owner', 's1', 'd1'); "
        "g = ts.request(ctx, ToolProposal('call-1', 'task.status', {'task_id': t['id']})); "
        "snap = c.snapshot(); "
        "c.close(); "
        "print(json.dumps({'task_id': t['id'], 'grant_id': g.id, 'pending': len(snap.approvals)}))"
    )
    r1 = run_desktop_subprocess(["-c", proc1], env=env, cwd=tmp_path)
    assert r1.returncode == 0, r1.stderr
    d1 = json.loads(r1.stdout.strip())
    assert d1["pending"] == 1

    proc2 = (
        "import json; "
        "from max_core.native_desktop.backend import NativeDesktopCore; "
        "c = NativeDesktopCore.from_env(); "
        "s_before = c.snapshot(); "
        f"decided = c.decide_approval({d1['grant_id']!r}, True); "
        "s_after = c.snapshot(); "
        "c.close(); "
        "print(json.dumps({'before': [a['id'] for a in s_before.approvals], "
        "'state': decided['state'], 'after': len(s_after.approvals)}))"
    )
    r2 = run_desktop_subprocess(["-c", proc2], env=env, cwd=tmp_path)
    assert r2.returncode == 0, r2.stderr
    d2 = json.loads(r2.stdout.strip())
    assert d2["before"] == [d1["grant_id"]]
    assert d2["state"] == "approved"
    assert d2["after"] == 0

    proc3 = "\n".join(
        [
            "import json",
            "from sqlalchemy import select",
            "from sqlalchemy.orm import Session",
            "from max_core.models import ApprovalGrant, TrustAudit",
            "from max_core.native_desktop.backend import NativeDesktopCore",
            "c = NativeDesktopCore.from_env()",
            "s = c.snapshot()",
            "with Session(c.engine) as sess:",
            f"    g = sess.scalar(select(ApprovalGrant).where(ApprovalGrant.id == {d1['grant_id']!r}))",
            "    audits = [a.decision for a in sess.scalars(select(TrustAudit)).all()]",
            "c.close()",
            "print(json.dumps({'pending': len(s.approvals), 'state': g.state, 'audits': audits}))",
        ]
    )
    r3 = run_desktop_subprocess(["-c", proc3], env=env, cwd=tmp_path)
    assert r3.returncode == 0, r3.stderr
    d3 = json.loads(r3.stdout.strip())
    assert d3["pending"] == 0
    assert d3["state"] == "approved"
    assert sorted(d3["audits"]) == ["approved", "pending"]


def test_t1_f2_04_trust_inbox_deny_persists_across_restarts(
    tmp_path: Path,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T1-F2-04: Denying a Trust grant removes it from inbox and persists denied state."""
    db_path = tmp_path / "trust_deny.sqlite3"
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(db_path)},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core1 = desktop_core_factory(config=cfg)
    task = core1.create_task("Trust denial restart test")
    assert core1.engine is not None
    grant = TrustService(core1.engine).request(
        ActorContext("owner", task["id"], "desktop_owner", "sess-deny", "dev-1"),
        ToolProposal(call_id="c-deny", name="task.status", arguments={"task_id": task["id"]}),
    )
    core1.close()

    core2 = desktop_core_factory(config=cfg)
    assert [a["id"] for a in core2.snapshot().approvals] == [grant.id]
    decided = core2.decide_approval(grant.id, False)
    assert decided["state"] == "denied"
    assert core2.snapshot().approvals == ()
    core2.close()

    core3 = desktop_core_factory(config=cfg)
    assert core3.snapshot().approvals == ()
    assert core3.engine is not None
    with Session(core3.engine) as session:
        row = session.scalar(select(ApprovalGrant).where(ApprovalGrant.id == grant.id))
        audits = session.scalars(select(TrustAudit).where(TrustAudit.approval_id == grant.id)).all()
        assert row is not None and row.state == "denied"
        assert {a.decision for a in audits} == {"pending", "denied"}


def test_t1_f2_05_multiple_tasks_wal_mode_and_windows_handle_release(
    tmp_path: Path,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T1-F2-05: Multi-task persistence uses WAL mode and releases Windows file handles on close."""
    db_path = tmp_path / "wal_release.sqlite3"
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(db_path)},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    ids = [core.create_task(f"Task item {i}")["id"] for i in range(3)]
    assert core.engine is not None
    with core.engine.connect() as conn:
        mode = str(conn.execute(text("PRAGMA journal_mode")).scalar_one()).lower()
    assert mode == "wal"
    core.close()

    moved = tmp_path / "moved_wal_release.sqlite3"
    db_path.rename(moved)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    moved.rename(db_path)

    reopened = desktop_core_factory(
        config=load_desktop_config(
            env={"MAX_LOCAL_DB_PATH": str(db_path)},
            dotenv_path=tmp_path / "missing.env",
            config_file=tmp_path / "missing.json",
            data_dir=tmp_path,
        )
    )
    snap = reopened.snapshot()
    assert {item["id"] for item in snap.tasks} == set(ids)


# ---------------------------------------------------------------------------
# Tier 2 — F2: Persistence Boundaries, Validation & Concurrency
# ---------------------------------------------------------------------------


def test_t2_f2_01_remote_postgres_and_memory_or_control_char_sqlite_rejected(
    tmp_path: Path,
) -> None:
    """T2-F2-01: Remote PostgreSQL, SQLite :memory:/URI, and control chars fail closed."""
    remote = "postgresql+psycopg://secret_u:secret_p@db.example.com:5432/max"
    with pytest.raises(NativeDesktopError) as pg_exc:
        desktop_database_url(remote)
    assert pg_exc.value.code == "local_postgresql_required"
    assert "secret_u" not in str(pg_exc.value)
    assert "db.example.com" not in str(pg_exc.value)

    bad_sqlite = [
        "sqlite+pysqlite:///:memory:",
        "sqlite+pysqlite:///file:mem.sqlite3",
        f"sqlite+pysqlite:///{(tmp_path / 'db.sqlite3').as_posix()}?mode=memory",
        f"sqlite+pysqlite:///{(tmp_path / 'db.sqlite3').as_posix()}\x00",
        f"sqlite+pysqlite:///{(tmp_path / 'db.sqlite3').as_posix()}\n",
    ]
    for bad in bad_sqlite:
        with pytest.raises(NativeDesktopError) as exc_info:
            NativeDesktopCore(database_url=bad)
        assert exc_info.value.code == "invalid_local_database_configuration"


def test_t2_f2_02_create_task_empty_whitespace_and_overlong_rejected(
    tmp_path: Path,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T2-F2-02: Empty, whitespace-only, and >500-char task descriptions are rejected cleanly."""
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "task_bounds.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    for bad_obj in ("", "   \t\n  ", "x" * 501):
        with pytest.raises(NativeDesktopError, match="invalid_task_description"):
            core.create_task(bad_obj)
    assert core.snapshot().tasks == ()


def test_t2_f2_03_invalid_task_and_approval_ids_and_unknown_uuid_rejected(
    tmp_path: Path,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T2-F2-03: Malformed IDs, path traversals, and unknown UUIDs are rejected cleanly."""
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "id_bounds.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    for bad_id in ("../etc/passwd", "not-a-uuid", str(uuid4()).upper()):
        with pytest.raises(NativeDesktopError, match="invalid_task_identifier"):
            core.timeline(bad_id)
        with pytest.raises(NativeDesktopError, match="invalid_approval_identifier"):
            core.decide_approval(bad_id, True)
    with pytest.raises(NativeDesktopError, match="local_core_request_rejected"):
        core.decide_approval(str(uuid4()), True)


def test_t2_f2_04_create_task_preserves_idempotency_key_across_retry(
    tmp_path: Path,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T2-F2-04: Ambiguous error after DB commit preserves Idempotency-Key and avoids duplicates."""
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "idempotent.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    original_call = core._call
    seen_keys: list[str | None] = []

    def flaky_call(
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if method == "POST" and path == "/v1/tasks":
            seen_keys.append(idempotency_key)
            res = original_call(method, path, payload=payload, idempotency_key=idempotency_key)
            if len(seen_keys) == 1:
                raise NativeDesktopError("local_core_unavailable")
            return res
        return original_call(method, path, payload=payload, idempotency_key=idempotency_key)

    core._call = flaky_call  # type: ignore[method-assign]
    with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
        core.create_task("Idempotent task retry")
    retried = core.create_task("Idempotent task retry")
    assert retried["objective"] == "Idempotent task retry"
    assert len(seen_keys) == 2 and seen_keys[0] == seen_keys[1]
    assert len(core.snapshot().tasks) == 1


def test_t2_f2_05_concurrent_task_creation_and_approval_decision_race(
    tmp_path: Path,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T2-F2-05: 8 concurrent task threads & 4 racing approval decision threads behave atomically."""
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "concurrency.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    task_barrier = threading.Barrier(8)

    def _create(idx: int) -> str:
        task_barrier.wait(timeout=5.0)
        return str(core.create_task(f"Concurrent task {idx}")["id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        task_ids = list(pool.map(_create, range(8)))

    assert len(set(task_ids)) == 8
    assert len(core.snapshot().tasks) == 8

    assert core.engine is not None
    grant = TrustService(core.engine).request(
        ActorContext("owner", task_ids[0], "desktop_owner", "sess-race", "dev-1"),
        ToolProposal(call_id="race-1", name="task.status", arguments={"task_id": task_ids[0]}),
    )

    decide_barrier = threading.Barrier(4)

    def _decide(idx: int) -> tuple[str, str]:
        decide_barrier.wait(timeout=5.0)
        try:
            res = core.decide_approval(grant.id, idx % 2 == 0)
            return ("ok", str(res["state"]))
        except NativeDesktopError as exc:
            return ("err", exc.code)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_decide, range(4)))

    oks = [o for o in outcomes if o[0] == "ok"]
    errs = [o for o in outcomes if o[0] == "err"]
    assert len(oks) == 1
    assert errs == [("err", "local_core_request_rejected")] * 3
    with Session(core.engine) as session:
        decision_audits = session.scalars(
            select(TrustAudit).where(
                TrustAudit.approval_id == grant.id,
                TrustAudit.action == "decision",
            )
        ).all()
        assert len([a for a in decision_audits if a.reason == "owner_decision"]) == 1
        assert len([a for a in decision_audits if a.decision == "deny"]) == 3


def test_t2_f2_06_close_drains_inflight_call_and_rejects_subsequent_calls(
    tmp_path: Path,
) -> None:
    """T2-F2-06: close() drains an in-flight call and rejects subsequent operations."""
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "drain.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = NativeDesktopCore.from_env(config=cfg)
    seed = core.create_task("Seed task")
    original_call = core._call
    entered = threading.Event()
    release = threading.Event()

    def gated_call(
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if method == "GET" and path == "/v1/tasks":
            entered.set()
            assert release.wait(timeout=5.0)
        return original_call(method, path, payload=payload, idempotency_key=idempotency_key)

    core._call = gated_call  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as pool:
        snap_fut = pool.submit(core.snapshot)
        assert entered.wait(timeout=5.0)
        close_fut = pool.submit(core.close)

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
        assert not close_fut.done()

        release.set()
        assert len(snap_fut.result(timeout=5.0).tasks) == 1
        close_fut.result(timeout=5.0)

    for op in (
        core.snapshot,
        lambda: core.create_task("after"),
        lambda: core.timeline(seed["id"]),
        lambda: core.decide_approval(str(uuid4()), True),
    ):
        with pytest.raises(NativeDesktopError, match="local_core_unavailable"):
            op()


# ---------------------------------------------------------------------------
# Tier 1 — F3: Desktop Launch, Smoke Check & Native UI Shell
# ---------------------------------------------------------------------------


def test_t1_f3_01_desktop_smoke_test_cli_offscreen_initializes_sqlite_db(
    tmp_path: Path,
    clean_desktop_env: dict[str, str],
    run_desktop_subprocess: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """T1-F3-01: python -m max_core.native_desktop --smoke-test exits 0 & initializes SQLite DB."""
    pytest.importorskip("PySide6")
    db_path = tmp_path / "smoke.sqlite3"
    env = {**clean_desktop_env, "MAX_LOCAL_DB_PATH": str(db_path)}
    res = run_desktop_subprocess(["-m", "max_core.native_desktop", "--smoke-test"], env=env)
    assert res.returncode == 0, res.stderr
    assert "Traceback" not in res.stderr
    assert db_path.is_file()
    # Verify file is unlocked after smoke-test subprocess exits.
    moved = tmp_path / "smoke_moved.sqlite3"
    db_path.rename(moved)
    moved.unlink()


def test_t1_f3_02_max_window_offscreen_shows_local_core_and_sqlite_storage_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T1-F3-02: MaxWindow launches offscreen and renders Local Core & Local SQLite status."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "ui_status.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    window = MaxWindow(core)
    try:
        window.show()
        _pump_until(app, lambda: window.system_storage.text() != "Storage: checking…")
        assert window.isVisible()
        assert window.stack.count() == 4
        assert window.online.text() == "●  LOCAL CORE ACTIVE"
        assert window.system_core.text() == "Core: Core online (in-process)"
        assert window.system_storage.text() == "Storage: Local SQLite ready"
    finally:
        window.close()
        app.processEvents()


def test_t1_f3_03_max_window_navigation_tabs_and_breadcrumbs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T1-F3-03: MaxWindow.switch() navigates across Overview, Tasks, Trust, and System."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "ui_nav.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    window = MaxWindow(core)
    try:
        window.show()
        _pump_until(app, lambda: window.system_storage.text() != "Storage: checking…")
        for idx, name in enumerate(("Overview", "Tasks", "Trust", "System")):
            window.switch(idx)
            app.processEvents()
            assert window.stack.currentIndex() == idx
            assert window.crumb.text() == f"MAX  /  {name}"
            assert window.nav[idx].property("active") == "true"
    finally:
        window.close()
        app.processEvents()


def test_t1_f3_04_max_window_command_bar_slash_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T1-F3-04: MaxWindow command bar handles /help, /status, /tasks, /trust, and /task."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "ui_cmd.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    window = MaxWindow(core)
    try:
        window.show()
        _pump_until(app, lambda: window.system_storage.text() != "Storage: checking…")

        window.command.setPlainText("/help")
        window.run_command()
        assert "Commands:" in window.notice.text()

        window.command.setPlainText("/status")
        window.run_command()
        assert window.stack.currentIndex() == 3

        window.command.setPlainText("/trust")
        window.run_command()
        assert window.stack.currentIndex() == 2

        window.command.setPlainText("/tasks")
        window.run_command()
        assert window.stack.currentIndex() == 1

        window.command.setPlainText("/task Draft release checklist")
        window.run_command()
        assert window.stack.currentIndex() == 1
        assert window.objective.toPlainText() == "Draft release checklist"
    finally:
        window.close()
        app.processEvents()


def test_t1_f3_05_max_window_quiet_mode_and_start_max_cmd_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T1-F3-05: Quiet mode toggles Orb timer; start-max.cmd matches desktop launch contract."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "ui_quiet.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    window = MaxWindow(core)
    try:
        window.show()
        _pump_until(app, lambda: window.system_storage.text() != "Storage: checking…")
        assert window.focus_button.text() == "Quiet mode: off"
        assert window.orb.quiet is False

        window.toggle_quiet()
        assert window.focus_button.text() == "Quiet mode: on"
        assert window.orb.quiet is True
        assert not window.orb._timer.isActive()

        window.toggle_quiet()
        assert window.focus_button.text() == "Quiet mode: off"
        assert window.orb.quiet is False
        assert window.orb._timer.isActive()
    finally:
        window.close()
        app.processEvents()

    launcher = (REPO_ROOT / "start-max.cmd").read_text(encoding="utf-8")
    assert '.venv\\Scripts\\python.exe" -m max_core.native_desktop' in launcher
    assert 'pip install -e ".[desktop]"' in launcher
    assert "exit /b %exitCode%" in launcher


# ---------------------------------------------------------------------------
# Tier 2 — F3: CLI & UI Negative / Boundary Cases + Secret Non-Leakage
# ---------------------------------------------------------------------------


def test_t2_f3_01_cli_rejects_unsupported_flags_with_exit_code_2(
    clean_desktop_env: dict[str, str],
    run_desktop_subprocess: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """T2-F3-01: CLI rejects unsupported flags (--server-only, extra args) with exit code 2."""
    pytest.importorskip("PySide6")
    for argv in (["--server-only"], ["--smoke-test", "--extra"]):
        res = run_desktop_subprocess(
            ["-m", "max_core.native_desktop", *argv],
            env=clean_desktop_env,
        )
        assert res.returncode == 2
        assert "Usage: python -m max_core.native_desktop [--smoke-test]" in res.stderr


def test_t2_f3_02_cli_smoke_test_rejects_remote_postgres_without_leaking_secrets(
    clean_desktop_env: dict[str, str],
    run_desktop_subprocess: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """T2-F3-02: Invalid remote DB URL exits 2 with sanitized error and zero credential echo."""
    pytest.importorskip("PySide6")
    env = {
        **clean_desktop_env,
        "MAX_DATABASE_URL": "postgresql+psycopg://secret_user:secret_pw@remote.example.com:5432/max",
    }
    res = run_desktop_subprocess(["-m", "max_core.native_desktop", "--smoke-test"], env=env)
    assert res.returncode == 2
    assert "local_postgresql_required" in res.stderr
    assert "Traceback" not in res.stderr
    for secret in ("secret_user", "secret_pw", "remote.example.com"):
        assert secret not in res.stderr
        assert secret not in res.stdout


def test_t2_f3_03_max_window_switch_out_of_bounds_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T2-F3-03: Out-of-bounds switch(-1) and switch(99) leave current page untouched."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "ui_oob.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    window = MaxWindow(core)
    try:
        window.switch(2)
        window.switch(-1)
        window.switch(99)
        assert window.stack.currentIndex() == 2
        assert window.crumb.text() == "MAX  /  Trust"
    finally:
        window.close()
        app.processEvents()


def test_t2_f3_04_max_window_command_bar_rejects_empty_and_overlong_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T2-F3-04: Command bar rejects overlong /task and handles whitespace-only input safely."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "ui_cmd_bad.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    window = MaxWindow(core)
    try:
        window.switch(0)
        window.command.setPlainText("   \n  ")
        window.run_command()
        assert window.stack.currentIndex() == 0

        window.command.setPlainText("/task " + ("a" * 501))
        window.run_command()
        assert window.stack.currentIndex() == 0
        assert "1–500 characters" in window.notice.text()
    finally:
        window.close()
        app.processEvents()


def test_t2_f3_05_max_window_no_database_mode_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T2-F3-05: MaxWindow with unconfigured database disables task creation without crashing."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    core = desktop_core_factory()
    window = MaxWindow(core)
    try:
        window.show()
        _pump_until(app, lambda: window.system_storage.text() != "Storage: checking…")
        assert window.system_storage.text() == "Storage: Database not configured"
        assert not window.queue_button.isEnabled()
        window.queue_task()
        assert "PostgreSQL" in window.notice.text() or "not ready" in window.notice.text().lower()
    finally:
        window.close()
        app.processEvents()


# ---------------------------------------------------------------------------
# Tier 3 — M1 Cross-Feature Combinations (T3-01, T3-02) + Secret DB Check
# ---------------------------------------------------------------------------


def test_t3_01_config_precedence_and_sqlite_restart_continuity(
    tmp_path: Path,
    clean_desktop_env: dict[str, str],
    run_desktop_subprocess: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """T3-01: Config precedence + SQLite journal/Trust Inbox survive restart and leak no secrets."""
    db_from_json = tmp_path / "json_db.sqlite3"
    db_from_env = tmp_path / "env_db.sqlite3"
    secret_openai = "sk-e2e-never-persist-openai-77777"
    secret_anthropic = "sk-ant-never-persist-anthropic-88888"

    (tmp_path / ".env").write_text(
        f"OPENAI_API_KEY={secret_openai}\nMAX_LLM_PROVIDER=openai\n",
        encoding="utf-8",
    )
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "active_provider": "anthropic",
                "local_db_path": str(db_from_json),
                "providers": {"anthropic": {"api_key": secret_anthropic}},
            }
        ),
        encoding="utf-8",
    )

    env_run = {
        **clean_desktop_env,
        "MAX_CONFIG_PATH": str(cfg_file),
        "MAX_LOCAL_DB_PATH": str(db_from_env),
        "MAX_LLM_PROVIDER": "openai",
    }

    step1 = (
        "import json; "
        "from max_core.gateway.contracts import ToolProposal; "
        "from max_core.local_config import load_desktop_config; "
        "from max_core.native_desktop.backend import NativeDesktopCore; "
        "from max_core.trust import ActorContext, TrustService; "
        "cfg = load_desktop_config(); "
        "c = NativeDesktopCore.from_env(config=cfg); "
        "t = c.create_task('Precedence persistence task'); "
        "g = TrustService(c.engine).request("
        "ActorContext('owner', t['id'], 'desktop_owner', 's-t3', 'd-t3'), "
        "ToolProposal('c-t3', 'task.status', {'task_id': t['id']})); "
        "c.close(); "
        "print(json.dumps({'provider': cfg.active_provider.provider, 'task_id': t['id'], 'grant_id': g.id}))"
    )
    r1 = run_desktop_subprocess(["-c", step1], env=env_run, cwd=tmp_path)
    assert r1.returncode == 0, r1.stderr
    d1 = json.loads(r1.stdout.strip())
    assert d1["provider"] == "openai"
    assert db_from_env.is_file()
    assert not db_from_json.exists()

    step2 = (
        "import json; "
        "from max_core.native_desktop.backend import NativeDesktopCore; "
        "c = NativeDesktopCore.from_env(); "
        "s = c.snapshot(); "
        f"decided = c.decide_approval({d1['grant_id']!r}, True); "
        "c.close(); "
        "print(json.dumps({'tasks': len(s.tasks), 'approvals': len(s.approvals), 'state': decided['state']}))"
    )
    r2 = run_desktop_subprocess(["-c", step2], env=env_run, cwd=tmp_path)
    assert r2.returncode == 0, r2.stderr
    d2 = json.loads(r2.stdout.strip())
    assert d2 == {"tasks": 1, "approvals": 1, "state": "approved"}

    # Verify raw SQLite tables never store configured API keys.
    conn = sqlite3.connect(db_from_env)
    try:
        for table in ("max_tasks", "max_outbox", "max_approval_grants", "max_trust_audit"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            serialized = repr(rows)
            assert secret_openai not in serialized
            assert secret_anthropic not in serialized
    finally:
        conn.close()


def test_t3_02_desktop_launch_offscreen_window_and_sqlite_task_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_desktop_env: dict[str, str],
    run_desktop_subprocess: Callable[..., subprocess.CompletedProcess[str]],
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T3-02: Offscreen --smoke-test, core task creation, and MaxWindow relaunch see SQLite DB."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    db_path = tmp_path / "t3_desktop.sqlite3"
    env = {**clean_desktop_env, "MAX_LOCAL_DB_PATH": str(db_path)}
    smoke = run_desktop_subprocess(["-m", "max_core.native_desktop", "--smoke-test"], env=env)
    assert smoke.returncode == 0, smoke.stderr

    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(db_path)},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core1 = desktop_core_factory(config=cfg)
    created = core1.create_task("Persisted desktop UI task")
    core1.close()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    core2 = desktop_core_factory(config=cfg)
    window = MaxWindow(core2)
    try:
        window.show()
        _pump_until(app, lambda: window.system_storage.text() != "Storage: checking…")
        assert window.online.text() == "●  LOCAL CORE ACTIVE"
        assert window.system_storage.text() == "Storage: Local SQLite ready"
        persisted = core2.snapshot().tasks
        assert len(persisted) == 1
        assert persisted[0]["id"] == created["id"]
        assert window._store_ready is True
        assert window.stat_tasks.text() == "1"
        assert window.queue_button.isEnabled() is True
    finally:
        window.close()
        app.processEvents()
