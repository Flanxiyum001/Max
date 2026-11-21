"""Unit, security, persistence, concurrency, and chat-loop tests for MAX local tools."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from max_core.assistant_chat import AssistantChatService
from max_core.gateway.contracts import ToolProposal
from max_core.local_config import DesktopConfig, load_desktop_config
from max_core.local_tools import (
    LocalToolError,
    LocalToolExecutor,
    LocalToolRegistry,
    ToolExecutor,
)
from max_core.models import ApprovalGrant, TrustAudit
from max_core.native_desktop.backend import NativeDesktopCore, NativeDesktopError
from max_core.trust import RiskTier, TrustService

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_config(
    tmp_path: Path,
    *,
    ws: Path | None = None,
    notes: Path | None = None,
    with_openai: bool = False,
) -> DesktopConfig:
    env: dict[str, str] = {
        "MAX_LOCAL_DB_PATH": str(tmp_path / "local_tools.sqlite3"),
        "MAX_WORKSPACE_ROOT": str(ws if ws is not None else tmp_path / "ws"),
        "MAX_NOTES_DIR": str(notes if notes is not None else tmp_path / "notes"),
    }
    if with_openai:
        env["MAX_LLM_PROVIDER"] = "openai"
        env["OPENAI_API_KEY"] = "sk-test-local-tools-1234567890abcdef"
        env["MAX_OPENAI_INPUT_USD_PER_MILLION"] = "0.15"
        env["MAX_OPENAI_OUTPUT_USD_PER_MILLION"] = "0.60"
    return load_desktop_config(
        env=env,
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )


def _openai_json(
    content: str = "Done.",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-local-tools",
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 90, "completion_tokens": 20, "total_tokens": 110},
    }


def test_registry_definitions_risk_tiers_and_static_safety(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    registry = LocalToolRegistry(cfg)
    assert ToolExecutor is LocalToolExecutor
    assert not hasattr(registry, "register_from_model")

    defs = registry.tool_definitions()
    assert len(defs) == 10
    names = {d.name for d in defs}
    assert names == {
        "dev.health",
        "dev.add",
        "system.status",
        "task.status",
        "task.list",
        "workspace.list_files",
        "workspace.read_file",
        "notes.list",
        "task.create",
        "notes.save",
    }
    for tool_def in defs:
        assert tool_def.parameters.get("additionalProperties") is False
        json.dumps(tool_def.parameters, allow_nan=False)

    for r0_name in (
        "dev.health",
        "dev.add",
        "system.status",
        "task.status",
        "task.list",
        "workspace.list_files",
        "workspace.read_file",
        "notes.list",
    ):
        assert registry.risk_tier(r0_name) == RiskTier.R0
        spec = registry.get(r0_name)
        assert spec is not None and spec.requires_approval is False

    assert registry.risk_tier("task.create") == RiskTier.R1
    assert registry.risk_tier("notes.save") == RiskTier.R2
    assert registry.get("task.create") is not None and registry.get("task.create").requires_approval
    assert registry.get("notes.save") is not None and registry.get("notes.save").requires_approval

    with pytest.raises(LocalToolError, match="capability_denied"):
        registry.risk_tier("shell.run")

    for rel in ("src/max_core/local_tools.py", "src/max_core/trust.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for forbidden in ("subprocess.run", "os.system", "eval(", "exec("):
            assert forbidden not in src


def test_dev_tools_and_system_status_happy_and_invalid(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _make_config(tmp_path, ws=ws, with_openai=True)
    core = NativeDesktopCore(config=cfg)
    try:
        core.snapshot()
        task = core.create_task("Private task objective")
        registry = LocalToolRegistry(cfg, core=core)
        executor = ToolExecutor(registry=registry, core=core)

        health = executor.execute(ToolProposal("c1", "dev.health", {}), task_id=task["id"])
        assert health.status == "completed"
        assert health.output == {"operation": "dev.health", "status": "ok"}

        bad_health = executor.execute(
            ToolProposal("c2", "dev.health", {"extra": 1}), task_id=task["id"]
        )
        assert bad_health.status == "failed"

        edge_add = executor.execute(
            ToolProposal("c3", "dev.add", {"a": -1_000_000, "b": 1_000_000}), task_id=task["id"]
        )
        assert edge_add.status == "completed"
        assert edge_add.output == {"operation": "dev.add", "result": 0}

        for bad_add in (
            {"a": True, "b": 1},
            {"a": 1, "b": False},
            {"a": 1.5, "b": 2},
            {"a": 1_000_001, "b": 0},
            {"a": -1_000_001, "b": 0},
            {"a": 1},
            {"a": 1, "b": 2, "c": 3},
        ):
            res = executor.execute(ToolProposal("c-bad", "dev.add", bad_add), task_id=task["id"])
            assert res.status == "failed"

        sys_res = executor.execute(ToolProposal("c4", "system.status", {}), task_id=task["id"])
        assert sys_res.status == "completed"
        assert isinstance(sys_res.output, dict)
        assert sys_res.output["storage_status"] == "Local SQLite ready"
        assert sys_res.output["provider_configured"] is True
        assert sys_res.output["active_provider"] == "openai"
        serialized_sys = json.dumps(sys_res.output)
        assert "sk-test-local-tools" not in serialized_sys
        assert str(tmp_path) not in serialized_sys
        assert "sqlite+pysqlite" not in serialized_sys
    finally:
        core.close()


def test_task_status_and_task_list_boundaries(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    core = NativeDesktopCore(config=cfg)
    try:
        core.snapshot()
        executor = ToolExecutor(core=core, config=cfg)

        empty_list = executor.execute(ToolProposal("tl-0", "task.list", {}))
        assert empty_list.status == "completed"
        assert isinstance(empty_list.output, dict)

        secret_obj = "Confidential user objective"
        t1 = core.create_task(secret_obj)
        t2 = core.create_task("Second task")

        status_res = executor.execute(
            ToolProposal("ts-1", "task.status", {"task_id": t1["id"]}),
            task_id=t2["id"],
        )
        assert status_res.status == "completed"
        assert isinstance(status_res.output, dict)
        assert status_res.output["task_id"] == t1["id"]
        assert "objective" not in status_res.output
        assert secret_obj not in json.dumps(status_res.output)

        missing_status = executor.execute(
            ToolProposal("ts-2", "task.status", {"task_id": "00000000-0000-4000-8000-000000000000"})
        )
        assert missing_status.status == "failed"
        assert missing_status.output == "task_not_found"

        for bad_id in ("not-a-uuid", t1["id"].upper(), f"{t1['id']}\x00"):
            bad = executor.execute(ToolProposal("ts-bad", "task.status", {"task_id": bad_id}))
            assert bad.status == "failed"

        list_res = executor.execute(
            ToolProposal("tl-1", "task.list", {"limit": 2}), task_id=t1["id"]
        )
        assert list_res.status == "completed"
        assert isinstance(list_res.output, dict)
        assert list_res.output["count"] == 2

        for bad_limit in (0, 51, True, "5"):
            bad = executor.execute(
                ToolProposal("tl-bad", "task.list", {"limit": bad_limit}), task_id=t1["id"]
            )
            assert bad.status == "failed"
    finally:
        core.close()


def test_workspace_list_read_exclusions_traversal_and_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / ".git").mkdir()
    (ws / ".GIT").mkdir(exist_ok=True)
    (ws / ".venv").mkdir()
    (ws / "__pycache__").mkdir()
    (ws / "secrets").mkdir()
    (ws / "PRIVATE").mkdir()

    (ws / "readme.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (ws / "sub" / "notes.md").write_text("Sub note", encoding="utf-8")
    (ws / "long.txt").write_text(("A" * 500 + "\n") * 50, encoding="utf-8")
    (ws / "binary.dat").write_bytes(b"hello\x00world")
    (ws / "badutf8.txt").write_bytes(b"\xff\xfe\xfd")
    (ws / "huge.bin").write_bytes(b"X" * 300_000)

    # Sensitive files that must never be listed or read (case-insensitive)
    secret_files = [
        ws / ".env",
        ws / ".env.local",
        ws / "sub" / ".Env.prod",
        ws / ".git" / "config",
        ws / ".venv" / "pyvenv.cfg",
        ws / "__pycache__" / "mod.pyc",
        ws / "secrets" / "k.txt",
        ws / "PRIVATE" / "p.txt",
        ws / "secret.pem",
        ws / "priv.KEY",
        ws / "cert.p12",
        ws / "local.db",
        ws / "local.sqlite3",
        ws / ".server.log",
        ws / "id_rsa",
        ws / "credentials.json",
    ]
    for sf in secret_files:
        sf.write_text("TOP_SECRET_VALUE", encoding="utf-8")

    outside = tmp_path / "outside_secret.txt"
    outside.write_text("OUTSIDE_SECRET", encoding="utf-8")

    cfg = _make_config(tmp_path, ws=ws)
    core = NativeDesktopCore(config=cfg)
    try:
        task = core.create_task("Workspace security")
        executor = ToolExecutor(core=core, config=cfg)

        listed = executor.execute(
            ToolProposal("ls-1", "workspace.list_files", {}), task_id=task["id"]
        )
        assert listed.status == "completed"
        assert isinstance(listed.output, dict)
        files = listed.output["files"]
        assert "readme.txt" in files
        assert "sub/notes.md" in files
        for forbidden_tok in (
            ".env",
            ".git",
            ".venv",
            "__pycache__",
            "secrets",
            "PRIVATE",
            ".pem",
            ".KEY",
            ".p12",
            ".db",
            ".sqlite3",
            ".server.log",
            "id_rsa",
            "credentials.json",
        ):
            assert all(forbidden_tok.lower() not in f.lower() for f in files)

        # Subdir & limit truncation
        sub_list = executor.execute(
            ToolProposal("ls-sub", "workspace.list_files", {"path": "sub", "limit": 1}),
            task_id=task["id"],
        )
        assert sub_list.status == "completed"
        assert sub_list.output["files"] == ["sub/notes.md"]  # type: ignore[index]

        # Line window & byte truncation
        read_slice = executor.execute(
            ToolProposal(
                "rd-1",
                "workspace.read_file",
                {"path": "readme.txt", "start_line": 2, "max_lines": 1},
            ),
            task_id=task["id"],
        )
        assert read_slice.status == "completed"
        assert read_slice.output["content"] == "line2"  # type: ignore[index]
        assert read_slice.output["truncated"] is True  # type: ignore[index]

        read_long = executor.execute(
            ToolProposal("rd-long", "workspace.read_file", {"path": "long.txt"}),
            task_id=task["id"],
        )
        assert read_long.status == "completed"
        assert read_long.output["truncated"] is True  # type: ignore[index]
        assert len(str(read_long.output["content"]).encode("utf-8")) <= 16_384  # type: ignore[index]

        # Reject binary, invalid UTF-8, oversized, missing, directory
        for bad_file in ("binary.dat", "badutf8.txt", "huge.bin", "missing.txt", "sub"):
            res = executor.execute(
                ToolProposal("rd-bad", "workspace.read_file", {"path": bad_file}),
                task_id=task["id"],
            )
            assert res.status == "failed"

        # Reject all secret files
        for sf in secret_files:
            rel = sf.relative_to(ws).as_posix()
            res = executor.execute(
                ToolProposal("rd-sec", "workspace.read_file", {"path": rel}),
                task_id=task["id"],
            )
            assert res.status == "denied"
            assert "TOP_SECRET_VALUE" not in str(res.output)

        # Traversal, absolute, UNC, drive, tilde, ADS, reserved stems, control chars
        bad_paths = [
            "../outside_secret.txt",
            "..\\outside_secret.txt",
            "sub/../../outside_secret.txt",
            "/etc/passwd",
            "C:\\Windows\\System32\\drivers\\etc\\hosts",
            "\\\\server\\share\\file.txt",
            "~/.ssh/id_rsa",
            "readme.txt:stream",
            "CON.txt",
            "readme.txt\x00.md",
            "sub\n/notes.md",
        ]
        for bp in bad_paths:
            res = executor.execute(
                ToolProposal("rd-trav", "workspace.read_file", {"path": bp}),
                task_id=task["id"],
            )
            assert res.status == "denied", f"Expected denied for {bp!r}, got {res}"
            assert "OUTSIDE_SECRET" not in str(res.output)

        # Optional real symlink test if OS allows unprivileged symlinks
        link_path = ws / "escape_link.txt"
        try:
            os.symlink(outside, link_path)
            symlink_created = True
        except (OSError, NotImplementedError):
            symlink_created = False
        if symlink_created:
            esc = executor.execute(
                ToolProposal("rd-sym", "workspace.read_file", {"path": "escape_link.txt"}),
                task_id=task["id"],
            )
            assert esc.status == "denied"
            assert "OUTSIDE_SECRET" not in str(esc.output)
            link_path.unlink()

        # Deterministic monkeypatched Path.resolve symlink-escape & inner-secret-symlink tests
        orig_resolve = Path.resolve

        def _fake_resolve_escape(self: Path, strict: bool = False) -> Path:
            if self.name == "readme.txt":
                return outside
            return orig_resolve(self, strict=strict)

        with monkeypatch.context() as mp:
            mp.setattr(Path, "resolve", _fake_resolve_escape)
            esc2 = executor.execute(
                ToolProposal("rd-sym2", "workspace.read_file", {"path": "readme.txt"}),
                task_id=task["id"],
            )
            assert esc2.status == "denied"
            assert esc2.output == "path_outside_root"

        def _fake_resolve_secret(self: Path, strict: bool = False) -> Path:
            if self.name == "readme.txt":
                return (ws / ".env").resolve(strict=True)
            return orig_resolve(self, strict=strict)

        with monkeypatch.context() as mp:
            mp.setattr(Path, "resolve", _fake_resolve_secret)
            esc3 = executor.execute(
                ToolProposal("rd-sym3", "workspace.read_file", {"path": "readme.txt"}),
                task_id=task["id"],
            )
            assert esc3.status == "denied"
            assert esc3.output == "excluded_secret_path"
    finally:
        core.close()


def test_notes_list_and_notes_save_validation_and_overwrite(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    cfg = _make_config(tmp_path, notes=notes_dir)
    core = NativeDesktopCore(config=cfg)
    try:
        task = core.create_task("Notes test")
        executor = ToolExecutor(core=core, config=cfg)

        # notes.list must NOT create notes_dir when absent
        listed_empty = executor.execute(ToolProposal("nl-0", "notes.list", {}), task_id=task["id"])
        assert listed_empty.status == "completed"
        assert listed_empty.output == {"count": 0, "truncated": False, "notes": []}
        assert not notes_dir.exists()

        # Bad titles and bad contents fail BEFORE creating ApprovalGrant
        for bad_title in (
            "../escape",
            "sub/note",
            "sub\\note",
            ".env",
            ".hidden",
            "evil.py",
            "run.bat",
            "CON",
            "NUL.md",
            "a:b",
            "bad\x00title",
            "   ",
        ):
            res = executor.execute(
                ToolProposal("ns-bad-t", "notes.save", {"title": bad_title, "content": "Valid"}),
                task_id=task["id"],
            )
            assert res.status == "denied"
        for bad_content in ("   ", "x\x00y", "a" * 40_000, "line\n" * 1005):
            res = executor.execute(
                ToolProposal("ns-bad-c", "notes.save", {"title": "valid", "content": bad_content}),
                task_id=task["id"],
            )
            assert res.status == "failed"
        assert core.snapshot().approvals == ()
        assert not notes_dir.exists()

        # Valid save (allows \t, \n, \r in content)
        prop = ToolProposal(
            "ns-ok", "notes.save", {"title": "my-note.txt", "content": "Line 1\n\tLine 2\r\n"}
        )
        gated = executor.execute(prop, task_id=task["id"])
        assert gated.status == "awaiting_approval" and gated.approval_id is not None
        assert not notes_dir.exists()

        core.decide_approval(gated.approval_id, True)
        saved = executor.execute(prop, task_id=task["id"], approval_id=gated.approval_id)
        assert saved.status == "completed"
        assert (notes_dir / "my-note.txt").read_bytes().decode("utf-8") == "Line 1\n\tLine 2\r\n"

        # overwrite=False (default) fails if note already exists
        prop_dup = ToolProposal("ns-dup", "notes.save", {"title": "my-note.txt", "content": "New"})
        g_dup = executor.execute(prop_dup, task_id=task["id"])
        assert g_dup.approval_id is not None
        core.decide_approval(g_dup.approval_id, True)
        dup_res = executor.execute(prop_dup, task_id=task["id"], approval_id=g_dup.approval_id)
        assert dup_res.status == "failed"
        assert dup_res.output == "note_already_exists"

        # overwrite=True updates note
        prop_ow = ToolProposal(
            "ns-ow",
            "notes.save",
            {"title": "my-note.txt", "content": "Updated", "overwrite": True},
        )
        g_ow = executor.execute(prop_ow, task_id=task["id"])
        assert g_ow.approval_id is not None
        core.decide_approval(g_ow.approval_id, True)
        ow_res = executor.execute(prop_ow, task_id=task["id"], approval_id=g_ow.approval_id)
        assert ow_res.status == "completed"
        assert (notes_dir / "my-note.txt").read_text(encoding="utf-8") == "Updated"

        listed = executor.execute(ToolProposal("nl-1", "notes.list", {}), task_id=task["id"])
        assert listed.status == "completed"
        assert listed.output["count"] == 1  # type: ignore[index]
        assert listed.output["notes"][0]["title"] == "my-note"  # type: ignore[index]
    finally:
        core.close()


def test_assistant_chat_r0_r1_resume_deny_replay_and_egress_secret(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "leaked.txt").write_text("sk-proj-" + "A" * 30, encoding="utf-8")
    notes_dir = tmp_path / "notes"
    cfg = _make_config(tmp_path, ws=ws, notes=notes_dir, with_openai=True)

    # 1. R0 chat loop -> R1 pause -> restart DB -> premature resume -> approve -> resume -> replay
    queued_responses: list[dict[str, Any]] = [
        _openai_json(
            "",
            tool_calls=[
                {
                    "id": "call_add_1",
                    "type": "function",
                    "function": {"name": "dev_add", "arguments": '{"a":10,"b":15}'},
                }
            ],
        ),
        _openai_json("The sum is 25."),
        _openai_json(
            "",
            tool_calls=[
                {
                    "id": "call_create_1",
                    "type": "function",
                    "function": {
                        "name": "task_create",
                        "arguments": '{"objective":"Verify resume flow"}',
                    },
                }
            ],
        ),
        _openai_json("Task created after approval."),
    ]
    seen_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=queued_responses.pop(0))

    core1 = NativeDesktopCore(config=cfg)
    exec1 = ToolExecutor(core=core1, config=cfg)
    svc = AssistantChatService(cfg, transport=httpx.MockTransport(handler))

    r0_res = svc.send_message("Add 10 and 15", tool_executor=exec1)
    assert r0_res.ok is True
    assert "25" in r0_res.reply
    assert r0_res.tool_executions[0].status == "completed"

    r1_res = svc.send_message("Create a task", tool_executor=exec1)
    assert r1_res.ok is True
    gated = r1_res.tool_executions[0]
    assert gated.status == "awaiting_approval" and gated.approval_id is not None
    approval_id = gated.approval_id
    core1.close()

    # Reopen DB across restart
    core2 = NativeDesktopCore(config=cfg)
    try:
        exec2 = ToolExecutor(core=core2, config=cfg)

        # Premature resume while pending stays awaiting_approval without denying grant
        premature = svc.resume_approval(approval_id, tool_executor=exec2)
        assert premature.ok is True
        assert premature.tool_executions[-1].status == "awaiting_approval"
        assert approval_id in svc.pending_approval_ids()

        # Approve in Trust Inbox and resume
        core2.decide_approval(approval_id, True)
        resumed = svc.resume_approval(approval_id, tool_executor=exec2)
        assert resumed.ok is True
        assert resumed.reply == "Task created after approval."
        assert resumed.tool_executions[-1].status == "completed"
        assert any(t["objective"] == "Verify resume flow" for t in core2.snapshot().tasks)

        # Replay via chat or direct executor fails
        replay_chat = svc.resume_approval(approval_id, tool_executor=exec2)
        assert replay_chat.ok is False
        assert replay_chat.error_code == "approval_not_found"

        replay_exec = exec2.execute(
            ToolProposal(gated.call_id, "task.create", {"objective": "Verify resume flow"}),
            approval_id=approval_id,
        )
        assert replay_exec.status == "denied"
        assert replay_exec.output == "approval_already_consumed"

        # 2. Denial path via resume_approval skips LLM
        queued_responses.append(
            _openai_json(
                "",
                tool_calls=[
                    {
                        "id": "call_note_deny",
                        "type": "function",
                        "function": {
                            "name": "notes_save",
                            "arguments": '{"title":"never","content":"Never written"}',
                        },
                    }
                ],
            )
        )
        deny_turn = svc.send_message("Save note never", tool_executor=exec2)
        deny_id = deny_turn.tool_executions[0].approval_id
        assert deny_id is not None
        core2.decide_approval(deny_id, False)
        count_before = len(seen_bodies)
        denied_resume = svc.resume_approval(deny_id, tool_executor=exec2)
        assert denied_resume.ok is True
        assert denied_resume.tool_executions[-1].status == "denied"
        assert len(seen_bodies) == count_before
        assert not notes_dir.exists()

        # 3. M16 defense-in-depth blocks inline secret returned from readable file
        queued_responses.append(
            _openai_json(
                "",
                tool_calls=[
                    {
                        "id": "call_leak",
                        "type": "function",
                        "function": {
                            "name": "workspace_read_file",
                            "arguments": '{"path":"leaked.txt"}',
                        },
                    }
                ],
            )
        )
        egress = svc.send_message("Read leaked.txt", tool_executor=exec2)
        assert egress.ok is False
        assert egress.error_code == "egress_blocked"
    finally:
        core2.close()


def test_unknown_tool_and_invalid_call_id_audited_and_concurrency_safe(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    core = NativeDesktopCore(config=cfg)
    try:
        core.snapshot()
        task = core.create_task("Concurrency and unknown tool")
        executor = ToolExecutor(core=core, config=cfg)

        unk = executor.execute(
            ToolProposal("bad-1", "shell.run", {"cmd": "dir"}), task_id=task["id"]
        )
        assert unk.status == "denied" and unk.output == "capability_denied"

        bad_cid = executor.execute(ToolProposal("bad\ncall", "dev.health", {}), task_id=task["id"])
        assert bad_cid.status == "denied" and bad_cid.output == "invalid_proposal"

        assert core.engine is not None
        with Session(core.engine) as session:
            audits = session.scalars(
                select(TrustAudit).where(TrustAudit.task_id == task["id"])
            ).all()
            assert {a.reason for a in audits} >= {"capability_denied", "invalid_proposal"}

        def _worker(idx: int) -> str:
            r0 = executor.execute(ToolProposal(f"conc-add-{idx}", "dev.add", {"a": idx, "b": 1}))
            assert r0.status == "completed"
            gated = executor.execute(
                ToolProposal(
                    f"conc-create-{idx}",
                    "task.create",
                    {"objective": f"Concurrent created {idx}"},
                )
            )
            assert gated.status == "awaiting_approval" and gated.approval_id is not None
            return gated.approval_id

        with ThreadPoolExecutor(max_workers=6) as pool:
            approval_ids = list(pool.map(_worker, range(6)))

        assert len(set(approval_ids)) == 6
        with Session(core.engine) as session:
            grants = session.scalars(select(ApprovalGrant)).all()
            assert len(grants) == 6
    finally:
        core.close()


def test_lifecycle_guard_and_disposed_engine_fail_cleanly(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    core = NativeDesktopCore(config=cfg)
    entered = 0
    closed = False

    @contextmanager
    def _guard() -> Iterator[None]:
        nonlocal entered
        if closed:
            raise NativeDesktopError("local_core_unavailable")
        entered += 1
        yield

    assert core.engine is not None
    trust = TrustService(core.engine)
    executor = ToolExecutor(core=core, trust=trust, config=cfg, lifecycle_guard=_guard)
    res_ok = executor.execute(ToolProposal("g1", "dev.health", {}))
    assert res_ok.status == "completed"
    assert entered == 1

    # Simulate closed/unavailable core via lifecycle_guard
    closed = True
    core.close()
    res_closed = executor.execute(ToolProposal("g2", "dev.health", {}))
    assert res_closed.status == "failed"
    assert res_closed.output == "local_core_unavailable"

    # Also verify SQLAlchemyError inside guard is sanitized
    class _BrokenTrust(TrustService):
        def execute_r0(self, *args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("internal sql details")

    core2 = NativeDesktopCore(config=_make_config(tmp_path / "sub"))
    try:
        assert core2.engine is not None
        reg2 = LocalToolRegistry(cfg, core=core2)
        broken_exec = ToolExecutor(
            registry=reg2,
            core=core2,
            trust=_BrokenTrust(core2.engine, registry=reg2.policy_registry()),
            config=cfg,
        )
        res_err = broken_exec.execute(ToolProposal("g3", "dev.health", {}))
        assert res_err.status == "failed"
        assert res_err.output == "tool_storage_unavailable"
    finally:
        core2.close()


def test_windows_ntfs_junction_blocked_in_workspace_and_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "legit.txt").write_text("ok", encoding="utf-8")
    (ws / "secrets").mkdir()
    (ws / "secrets" / "api.txt").write_text("TOP_SECRET", encoding="utf-8")
    (ws / "sim_junc").mkdir()
    (ws / "public_alias").mkdir()
    (ws / "unfollowed_junc").mkdir()

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "ext_file.txt").write_text("EXTERNAL_SECRET", encoding="utf-8")

    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "safe.md").write_text("safe", encoding="utf-8")
    (notes_dir / "junc_note.md").write_text("trap", encoding="utf-8")

    cfg = _make_config(tmp_path, ws=ws, notes=notes_dir)
    core = NativeDesktopCore(config=cfg)
    try:
        task = core.create_task("NTFS junction security")
        executor = ToolExecutor(core=core, config=cfg)

        orig_resolve = Path.resolve
        orig_is_junction = getattr(Path, "is_junction", None)

        def _fake_is_junction(self: Path) -> bool:
            if self.name in {"sim_junc", "public_alias", "unfollowed_junc", "junc_note.md"}:
                return True
            if orig_is_junction is not None:
                return bool(orig_is_junction(self))
            return False

        def _fake_resolve(self: Path, strict: bool = False) -> Path:
            if self == ws / "sim_junc":
                return orig_resolve(outside_dir, strict=strict)
            if self == ws / "sim_junc" / "ext_file.txt":
                return orig_resolve(outside_dir / "ext_file.txt", strict=strict)
            if self == ws / "public_alias":
                return orig_resolve(ws / "secrets", strict=strict)
            if self == ws / "public_alias" / "api.txt":
                return orig_resolve(ws / "secrets" / "api.txt", strict=strict)
            if self == notes_dir / "junc_note.md":
                return orig_resolve(outside_dir / "ext_file.txt", strict=strict)
            return orig_resolve(self, strict=strict)

        with monkeypatch.context() as mp:
            mp.setattr(Path, "is_junction", _fake_is_junction, raising=False)
            mp.setattr(Path, "resolve", _fake_resolve)

            listed = executor.execute(
                ToolProposal("junc-ls", "workspace.list_files", {}),
                task_id=task["id"],
            )
            assert listed.status == "completed"
            assert isinstance(listed.output, dict)
            assert listed.output["files"] == ["legit.txt"]

            for bad_path in ("sim_junc/ext_file.txt", "sim_junc/nonexistent.txt"):
                res_ext = executor.execute(
                    ToolProposal("junc-rd-ext", "workspace.read_file", {"path": bad_path}),
                    task_id=task["id"],
                )
                assert res_ext.status == "denied"
                assert res_ext.output == "path_outside_root"

            for bad_sec in ("public_alias/api.txt", "public_alias/nonexistent.txt"):
                res_sec = executor.execute(
                    ToolProposal("junc-rd-sec", "workspace.read_file", {"path": bad_sec}),
                    task_id=task["id"],
                )
                assert res_sec.status == "denied"
                assert res_sec.output == "excluded_secret_path"

            res_unf = executor.execute(
                ToolProposal("junc-rd-unf", "workspace.read_file", {"path": "unfollowed_junc"}),
                task_id=task["id"],
            )
            assert res_unf.status == "denied"
            assert res_unf.output == "symlink_not_allowed"

            notes_listed = executor.execute(
                ToolProposal("junc-nl", "notes.list", {}),
                task_id=task["id"],
            )
            assert notes_listed.status == "completed"
            assert isinstance(notes_listed.output, dict)
            assert [n["filename"] for n in notes_listed.output["notes"]] == ["safe.md"]  # type: ignore[attr-defined]

            note_save_target_junc = executor.execute(
                ToolProposal(
                    "junc-ns",
                    "notes.save",
                    {"title": "junc_note", "content": "Should fail", "overwrite": True},
                ),
                task_id=task["id"],
            )
            assert note_save_target_junc.status == "awaiting_approval"
            assert note_save_target_junc.approval_id is not None
            core.decide_approval(note_save_target_junc.approval_id, True)
            executed_junc = executor.execute(
                ToolProposal(
                    "junc-ns",
                    "notes.save",
                    {"title": "junc_note", "content": "Should fail", "overwrite": True},
                ),
                task_id=task["id"],
                approval_id=note_save_target_junc.approval_id,
            )
            assert executed_junc.status == "denied"
            assert executed_junc.output == "symlink_not_allowed"

        # Junction as notes_dir itself is rejected
        with monkeypatch.context() as mp:
            mp.setattr(
                Path,
                "is_junction",
                lambda self: (
                    self == notes_dir or (orig_is_junction(self) if orig_is_junction else False)
                ),
                raising=False,
            )
            nl_dir = executor.execute(
                ToolProposal("junc-nd-1", "notes.list", {}), task_id=task["id"]
            )
            assert nl_dir.status == "denied"
            assert nl_dir.output == "symlink_not_allowed"
    finally:
        core.close()


def test_notes_save_trailing_space_stem_rejected_and_validator_idempotent(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    cfg = _make_config(tmp_path, notes=notes_dir)
    core = NativeDesktopCore(config=cfg)
    try:
        task = core.create_task("Note title validation")
        reg = LocalToolRegistry(cfg, core=core)
        executor = ToolExecutor(registry=reg, core=core, config=cfg)

        for bad_title in ("NUL .txt", "CON .md", "foo .txt", "foo .md", "LPT1 .txt", " .md"):
            with pytest.raises(LocalToolError, match="invalid_note_title"):
                reg.validate("notes.save", {"title": bad_title, "content": "Hello"})
            res = executor.execute(
                ToolProposal("bad-stem", "notes.save", {"title": bad_title, "content": "Hello"}),
                task_id=task["id"],
            )
            assert res.status == "denied"
            assert res.output == "invalid_note_title"
            assert res.approval_id is None

        assert core.snapshot().approvals == ()
        assert not notes_dir.exists()

        for valid_title in ("plan", "plan.md", "plan.MD", "plan.txt", "plan.TXT", "my note", "t "):
            first = reg.validate("notes.save", {"title": valid_title, "content": "Body"})
            second = reg.validate("notes.save", first)
            assert second == first
    finally:
        core.close()


def test_notes_save_multibyte_and_max_byte_bounds_end_to_end(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    cfg = _make_config(tmp_path, notes=notes_dir)
    core = NativeDesktopCore(config=cfg)
    try:
        task = core.create_task("Note encoding bounds")
        executor = ToolExecutor(core=core, config=cfg)

        for idx, (title, content) in enumerate(
            [
                ("emojis-12k", "\U0001f600" * 3_000),
                ("emojis-32k", "\U0001f600" * 8_192),
                ("ascii-32k", "A" * 32_768),
            ]
        ):
            prop = ToolProposal(f"ok-{idx}", "notes.save", {"title": title, "content": content})
            gated = executor.execute(prop, task_id=task["id"])
            assert gated.status == "awaiting_approval", f"Expected awaiting_approval for {title}"
            assert gated.approval_id is not None
            core.decide_approval(gated.approval_id, True)
            done = executor.execute(prop, task_id=task["id"], approval_id=gated.approval_id)
            assert done.status == "completed"
            assert (notes_dir / f"{title}.md").read_text(encoding="utf-8") == content

        for idx, too_large in enumerate(["A" * 32_769, "\U0001f600" * 8_193]):
            res = executor.execute(
                ToolProposal(
                    f"big-{idx}", "notes.save", {"title": "too-big", "content": too_large}
                ),
                task_id=task["id"],
            )
            assert res.status == "failed"
            assert res.output == "note_content_too_large"
            assert res.approval_id is None
    finally:
        core.close()
