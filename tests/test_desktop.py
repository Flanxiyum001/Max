"""Desktop control: command policy, approval gate, MCP server, CLI, wiring.

Every test here runs against the real stdio MCP server as a child process, so
the protocol, the approval file handshake and the sandbox are exercised
together. No test needs a display: the input backend is optional and its
absence is asserted as an actionable error rather than a crash.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from max.desktop import ApprovalBook, ApprovalError, classify, desktop_server_config, fingerprint
from max.desktop.policy import DENIED_PROGRAMS
from max.desktop.server import DesktopMCPServer, redact_argv, sandbox_env
from max.mcp import MCPClient

# --------------------------------------------------------------- policy tier


@pytest.mark.parametrize(
    "argv",
    [
        ["ls"], ["ls", "-la"], ["pwd"], ["cat", "notes.md"], ["cat", "./a/b.txt"],
        ["grep", "-r", "todo", "src"], ["find", ".", "-name", "*.py"],
        ["git", "status"], ["git", "diff", "--stat"], ["git", "log", "--oneline"],
        ["python", "-m", "pytest", "-q"], ["head", "-n", "5", "file.txt"],
    ],
)
def test_read_only_commands_are_safe(argv):
    assert classify(argv).tier == "safe"


@pytest.mark.parametrize(
    "argv",
    [
        ["mkdir", "sub"], ["rm", "build"], ["rm", "-rf", "build"], ["cp", "a", "b"],
        ["mv", "a", "b"], ["touch", "x"], ["sed", "-i", "s/a/b/", "f.txt"],
        ["git", "add", "."], ["git", "commit", "-m", "x"], ["make", "all"],
        ["python", "setup.py", "install"], ["pip3", "list"],
    ],
)
def test_state_changing_commands_need_approval(argv):
    verdict = classify(argv)
    assert verdict.tier in {"approval", "denied"}
    if verdict.tier == "approval":
        assert verdict.argv == tuple(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["sudo", "ls"], ["su", "-", "root"], ["bash", "-c", "ls"], ["sh"],
        ["chmod", "777", "."], ["docker", "run", "x"], ["curl", "http://x"],
        ["wget", "http://x"], ["ssh", "host"], ["nc", "-l"], ["pip", "install", "x"],
        ["npm", "install"], ["systemctl", "restart", "x"], ["shutdown", "-h", "now"],
        ["kill", "-9", "1"], ["crontab", "-e"], ["dd", "if=/dev/zero"],
        ["ln", "-s", "/etc/passwd", "x"], ["xargs", "ls"], ["python", "-c", "print(1)"],
        ["python", "-m", "pip", "install", "x"],
    ],
)
def test_denied_regardless_of_approval(argv):
    verdict = classify(argv)
    assert verdict.tier == "denied", argv
    assert verdict.reason


def test_recursive_force_delete_is_denied_but_plain_rm_is_approvable():
    assert classify(["rm", "-rf", "build"]).tier == "denied"
    assert classify(["rm", "-fr", "build"]).tier == "denied"
    assert classify(["rm", "build"]).tier == "approval"


@pytest.mark.parametrize(
    "argv",
    [
        ["cat", "/etc/passwd"],
        ["cat", "~/.ssh/id_rsa"],
        ["ls", "../../etc"],
        ["git", "status", "--work-tree=/"],
        ["grep", "x", "--file=/etc/shadow"],
        ["echo", "a;b"],
        ["echo", "$(whoami)"],
        ["echo", "a | b"],
        ["git", "push", "origin", "main"],
        ["git", "clone", "http://x"],
        ["find", ".", "-delete"],
        ["find", ".", "-exec", "ls", ";"],
    ],
)
def test_paths_metachars_and_egress_are_denied(argv):
    assert classify(argv).tier == "denied", argv


def test_malformed_commands_are_denied():
    assert classify("").tier == "denied"
    assert classify("ls -la").tier == "denied"  # never a shell string
    assert classify([]).tier == "denied"
    assert classify(["ls", ""]).tier == "denied"
    assert classify(["ls", 42]).tier == "denied"  # type: ignore[list-item]
    assert classify(["ls"] * 64).tier == "denied"
    assert classify(["ls", "x" * 600]).tier == "denied"
    assert classify(None).tier == "denied"


def test_denied_program_list_covers_shell_escalation_and_egress():
    for name in ("sudo", "su", "bash", "sh", "curl", "docker", "pip", "ssh", "chmod"):
        assert name in DENIED_PROGRAMS


# ----------------------------------------------------------- approval book


def _book(tmp_path, **kw) -> ApprovalBook:
    return ApprovalBook(tmp_path / "state", **kw)


def test_approval_roundtrip_is_one_shot(tmp_path):
    book = _book(tmp_path)
    fp = fingerprint("term", ["mkdir", "sub"], str(tmp_path))
    record = book.request("term", ["mkdir", "sub"], str(tmp_path), "grows a folder")
    assert record["status"] == "pending"

    with pytest.raises(ApprovalError, match="not granted"):
        book.consume(record["id"], fp, "term")

    book.grant(record["id"], granted_by="operator")
    book.consume(record["id"], fp, "term")

    with pytest.raises(ApprovalError):
        book.consume(record["id"], fp, "term")  # spent


def test_approval_does_not_transfer_between_actions(tmp_path):
    book = _book(tmp_path)
    record = book.request("term", ["mkdir", "sub"], "/w", "one thing")
    book.grant(record["id"], "operator")
    other = fingerprint("term", ["rm", "sub"], "/w")
    with pytest.raises(ApprovalError, match="does not match"):
        book.consume(record["id"], other, "term")


def test_approval_kind_does_not_cross(tmp_path):
    book = _book(tmp_path)
    record = book.request("gui", ["gui", "control_mouse_keyboard"], "", "input")
    book.grant(record["id"], "operator")
    with pytest.raises(ApprovalError, match="not term"):
        book.consume(record["id"], record["fingerprint"], "term")


def test_approval_expires(tmp_path):
    now = [1000.0]
    book = _book(tmp_path, ttl_s=60, clock=lambda: now[0])
    record = book.request("term", ["mkdir", "x"], "/w", "r")
    book.grant(record["id"], "operator")
    now[0] += 61
    with pytest.raises(ApprovalError, match="expired"):
        book.consume(record["id"], record["fingerprint"], "term")


def test_denied_request_stays_denied(tmp_path):
    book = _book(tmp_path)
    record = book.request("term", ["mkdir", "x"], "/w", "r")
    book.deny(record["id"], "operator")
    with pytest.raises(ApprovalError, match="denied"):
        book.grant(record["id"], "operator")


def test_gui_grant_is_counted_and_bounded(tmp_path):
    now = [1000.0]
    book = _book(tmp_path, gui_max_actions=2, gui_ttl_s=30, clock=lambda: now[0])
    record = book.request("gui", ["gui", "control_mouse_keyboard"], "", "input")
    book.grant(record["id"], "operator")
    fp = record["fingerprint"]
    book.consume(record["id"], fp, "gui")
    book.consume(record["id"], fp, "gui")
    with pytest.raises(ApprovalError, match="used up"):
        book.consume(record["id"], fp, "gui")


def test_requests_for_same_action_share_one_record(tmp_path):
    book = _book(tmp_path)
    first = book.request("term", ["mkdir", "x"], "/w", "r")
    second = book.request("term", ["mkdir", "x"], "/w", "again")
    assert first["id"] == second["id"]


def test_malformed_approval_id_is_rejected(tmp_path):
    book = _book(tmp_path)
    for bad in ("", "../etc", "a/b", "x" * 40):
        with pytest.raises(ApprovalError):
            book.get(bad)


# ----------------------------------------------------------- server helpers


def test_sandbox_env_cannot_carry_secrets(monkeypatch):
    monkeypatch.setenv("MAX_DISCORD_TOKEN", "super-secret")
    monkeypatch.setenv("FREELLM_API_KEY", "sk-also-secret")
    env = sandbox_env(Path("/tmp/ws"))
    assert "MAX_DISCORD_TOKEN" not in env
    assert "FREELLM_API_KEY" not in env
    assert set(env) == {"PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "MAX_DESKTOP", "PYTHONDONTWRITEBYTECODE"}
    assert env["HOME"] == "/tmp/ws"


def test_redact_argv_masks_secret_values():
    out = redact_argv(["curl", "--token=abc123", "-p", "password=hunter2", "plain"])
    assert "--token=***" in out
    assert "password=***" in out
    assert "plain" in out
    assert "abc123" not in " ".join(out)
    assert "hunter2" not in " ".join(out)


# ------------------------------------------------------- real MCP subprocess


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp("desktop-ws")
    (root / "hello.txt").write_text("hi there\n", encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def state(tmp_path_factory):
    return tmp_path_factory.mktemp("desktop-state")


@pytest.fixture(scope="module")
def client(workspace, state):
    command = [
        sys.executable, "-m", "max.desktop.server",
        "--root", str(workspace), "--state", str(state),
        "--max-timeout", "10",
    ]
    c = MCPClient(command)
    c.start(timeout=20)
    yield c
    c.close()


def _call(client, tool: str, args: dict) -> dict:
    return json.loads(client.call_tool(tool, args))


def test_server_handshake_and_tool_surface(client):
    assert client.server_info["serverInfo"]["name"] == "max-desktop"
    names = [t["name"] for t in client.list_tools()]
    assert names == ["execute_terminal_command", "control_mouse_keyboard", "read_active_screen"]
    for tool in client.list_tools():
        assert tool["inputSchema"]["type"] == "object"
        assert len(tool["description"]) > 40


def test_safe_command_runs_in_workspace(client, workspace):
    result = _call(client, "execute_terminal_command", {"command": ["cat", "hello.txt"]})
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "hi there" in result["stdout"]
    assert result["timed_out"] is False


def test_output_is_bounded():
    from max.desktop.server import _clip

    text, cut = _clip("x" * 500, 100)
    assert cut is True and text.startswith("x" * 100) and "truncated 400" in text
    assert _clip("short", 100) == ("short", False)


def test_state_change_requires_approval_then_runs(client, workspace, state):
    result = _call(client, "execute_terminal_command", {"command": ["mkdir", "approved-dir"]})
    assert result["status"] == "approval_required"
    approval_id = result["approval_id"]
    assert "max desktop approve" in result["instructions"]

    # not yet granted
    again = _call(
        client,
        "execute_terminal_command",
        {"command": ["mkdir", "approved-dir"], "approval_id": approval_id},
    )
    assert again["status"] == "approval_required"
    assert "not granted" in again["reason"]

    ApprovalBook(state).grant(approval_id, granted_by="operator")

    granted = _call(
        client,
        "execute_terminal_command",
        {"command": ["mkdir", "approved-dir"], "approval_id": approval_id},
    )
    assert granted["status"] == "ok", granted
    assert (workspace / "approved-dir").is_dir()


def test_granted_approval_cannot_run_a_different_command(client, state):
    result = _call(client, "execute_terminal_command", {"command": ["mkdir", "other-dir"]})
    approval_id = result["approval_id"]
    ApprovalBook(state).grant(approval_id, granted_by="operator")

    mismatch = _call(
        client,
        "execute_terminal_command",
        {"command": ["rmdir", "other-dir"], "approval_id": approval_id},
    )
    assert mismatch["status"] == "approval_required"
    assert "does not match" in mismatch["reason"]
    # the mismatch request created a fresh, separate approval
    assert mismatch["approval_id"] != approval_id


def test_denied_command_never_runs(client):
    result = _call(client, "execute_terminal_command", {"command": ["sudo", "ls"]})
    assert result["status"] == "denied"
    assert "never permitted" in result["reason"]
    # and no approval record was created for it
    state_dir = Path(client.command[client.command.index("--state") + 1])
    pending = ApprovalBook(state_dir).pending()
    assert all("sudo" not in json.dumps(record) for record in pending)


def test_command_timeout_kills_the_process_group(client):
    result = _call(
        client,
        "execute_terminal_command",
        {"command": ["sleep", "8"], "timeout_s": 1},
    )
    assert result["status"] == "failed"
    assert result["timed_out"] is True
    assert result["seconds"] < 8  # killed early, not waited out


def test_cwd_escape_is_rejected(client):
    result = _call(client, "execute_terminal_command", {"command": ["ls"], "cwd": "../.."})
    assert result["status"] == "error"
    assert "workspace" in result["error"]


def test_missing_program_reports_clear_error(client, state):
    argv = ["definitely-not-a-real-bin", "--flag"]
    result = _call(client, "execute_terminal_command", {"command": argv})
    assert result["status"] == "approval_required"  # unknown programs are never 'safe'
    approval_id = result["approval_id"]
    ApprovalBook(state).grant(approval_id, granted_by="operator")
    granted = _call(
        client, "execute_terminal_command", {"command": argv, "approval_id": approval_id}
    )
    assert granted["status"] == "error"
    assert "not found" in granted["error"]


def test_gui_input_requires_a_session_approval(client):
    result = _call(client, "control_mouse_keyboard", {"action": "click", "x": 10, "y": 20})
    assert result["status"] == "approval_required"
    assert result["max_actions"] >= 1
    assert "approve" in result["instructions"]


def test_gui_rejects_unknown_action_without_spending_an_approval(client, state):
    result = _call(client, "control_mouse_keyboard", {"action": "launch-missiles"})
    assert result["status"] == "error"
    assert "unknown action" in result["error"]
    # no approval was created for a nonsense action
    pending = ApprovalBook(Path(state)).pending()
    assert all("launch-missiles" not in json.dumps(r) for r in pending)


def test_screen_capture_degrades_without_pyautogui(client):
    try:
        import pyautogui  # noqa: F401
    except Exception:  # noqa: BLE001 - the common case on a headless host
        result = _call(client, "read_active_screen", {})
        assert result["status"] == "error"
        assert "max-assistant[desktop]" in result["error"]
    else:  # pragma: no cover - host with a display
        result = _call(client, "read_active_screen", {})
        assert result["status"] == "ok"


def test_server_writes_an_audit_trail(client, state):
    entries = [
        json.loads(line)
        for line in (Path(state) / "desktop_audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    events = {e["event"] for e in entries}
    assert "desktop_server_started" in events
    assert "desktop_exec" in events
    assert "desktop_approval_requested" in events
    for entry in entries:
        blob = json.dumps(entry)
        assert "MAX_DISCORD_TOKEN" not in blob
        assert "super-secret" not in blob


def test_unknown_method_returns_jsonrpc_error(client):
    client._id += 1
    payload = {"jsonrpc": "2.0", "id": client._id, "method": "does/not/exist", "params": {}}
    client._write(payload)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        message = client._read(deadline - time.monotonic())
        if message and message.get("id") == client._id:
            assert message["error"]["code"] == -32601
            return
    raise AssertionError("no error response")


# ------------------------------------------------------------------- wiring


def _agent(tmp_path, monkeypatch, **env):
    from max.config import Config
    from max.core.factory import build_agent
    from max.providers import MockProvider

    monkeypatch.setenv("MAX_PROVIDER", "mock")
    monkeypatch.setenv("MAX_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("MAX_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("MAX_SKILLS_DIRS", str(tmp_path / "skills"))
    monkeypatch.setenv("MAX_ENABLE_SKILLS", "1")
    monkeypatch.setenv("MAX_TOOLS_ALLOW", "")
    monkeypatch.setenv("MAX_TOOLS_DENY", "")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = Config.from_env(repo_root=tmp_path, apply_dotenv=False)
    config.ensure_dirs()
    return build_agent(config, provider=MockProvider()), config


def test_desktop_tools_exist_but_are_denied_by_default(tmp_path, monkeypatch):
    agent, config = _agent(tmp_path, monkeypatch, MAX_DESKTOP_ENABLE="0")
    assert config.desktop_enabled is False
    for name in ("desktop_exec", "desktop_screen", "desktop_input"):
        spec = agent.registry.get(name)
        assert spec is not None and spec.category == "exec"
        decision = agent.policy.check(name, "exec")
        assert decision.allowed is False
        output, meta = agent.executor.dispatch(name, {"command": ["ls"]})
        assert meta["allowed"] is False and output.startswith("REJECTED:")


def test_enabling_desktop_allows_the_three_tools(tmp_path, monkeypatch):
    agent, config = _agent(tmp_path, monkeypatch, MAX_DESKTOP_ENABLE="1")
    assert config.desktop_enabled is True
    for name in ("desktop_exec", "desktop_screen", "desktop_input"):
        assert agent.policy.check(name, "exec").allowed is True


def test_max_tools_deny_still_revokes_desktop(tmp_path, monkeypatch):
    agent, _ = _agent(
        tmp_path, monkeypatch, MAX_DESKTOP_ENABLE="1", MAX_TOOLS_DENY="desktop_exec"
    )
    assert agent.policy.check("desktop_exec", "exec").allowed is False
    assert agent.policy.check("desktop_screen", "exec").allowed is True


def test_desktop_server_config_leaks_no_environment(tmp_path, monkeypatch):
    _, config = _agent(tmp_path, monkeypatch, MAX_DESKTOP_ENABLE="1")
    server = desktop_server_config(config)
    assert server.enabled is True
    assert server.env_allow == ()          # PATH/LANG only, by MCP manager design
    assert server.tool_allow == (
        "execute_terminal_command", "control_mouse_keyboard", "read_active_screen",
    )
    assert server.command[:3] == [sys.executable, "-m", "max.desktop.server"]
    assert str(config.workspace_dir.resolve()) in server.command
    assert str((config.data_dir / "desktop").resolve()) in server.command


def test_disabled_desktop_registers_no_server(tmp_path, monkeypatch):
    from max.desktop import desktop_server_config
    from max.mcp.backend import parse_server_configs

    _, config = _agent(tmp_path, monkeypatch, MAX_DESKTOP_ENABLE="0")
    assert config.desktop_enabled is False
    # the helper still exists, but the factory never calls it when disabled
    assert desktop_server_config(config).id == "desktop"
    assert not any(c.id == "desktop" for c in parse_server_configs(config.mcp_servers))


# ---------------------------------------------------------------------- CLI


def test_cli_status_reports_configuration(tmp_path, monkeypatch, capsys):
    from max.cli import main

    monkeypatch.setenv("MAX_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("MAX_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("MAX_DESKTOP_ENABLE", "1")
    assert main(["desktop", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["desktop_enabled"] is True
    assert payload["pending_approvals"] == 0
    assert "token" not in json.dumps(payload).lower()


def test_cli_approve_gates_on_confirmation(tmp_path, monkeypatch, capsys):
    from max.cli import main

    monkeypatch.setenv("MAX_DATA_DIR", str(tmp_path / "var"))
    book = ApprovalBook(tmp_path / "var" / "desktop")
    record = book.request("term", ["mkdir", "x"], str(tmp_path), "grow it")

    # non-interactive without --yes must refuse rather than hang
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["desktop", "approve", record["id"]]) == 2
    assert book.get(record["id"])["status"] == "pending"
    err = capsys.readouterr().err
    assert "--yes" in err

    assert main(["desktop", "approve", record["id"], "--yes"]) == 0
    assert book.get(record["id"])["status"] == "granted"


def test_cli_approve_rejects_unknown_id(tmp_path, monkeypatch, capsys):
    from max.cli import main

    monkeypatch.setenv("MAX_DATA_DIR", str(tmp_path / "var"))
    assert main(["desktop", "approve", "deadbeef0000", "--yes"]) == 1
    assert "unknown approval id" in capsys.readouterr().err


def test_cli_approvals_lists_pending(tmp_path, monkeypatch, capsys):
    from max.cli import main

    monkeypatch.setenv("MAX_DATA_DIR", str(tmp_path / "var"))
    book = ApprovalBook(tmp_path / "var" / "desktop")
    record = book.request("term", ["mkdir", "x"], "/w", "grow it")
    assert main(["desktop", "approvals"]) == 0
    out = capsys.readouterr().out
    assert record["id"] in out and "grow it" in out

    book.deny(record["id"], "operator")
    capsys.readouterr()
    assert main(["desktop", "approvals"]) == 0
    assert "no pending" in capsys.readouterr().out


def test_server_module_has_no_side_effects_on_import():
    """Importing the server must not start a loop or touch the filesystem."""
    proc = subprocess.run(
        [sys.executable, "-c", "import max.desktop.server; print('ok')"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "ok"
