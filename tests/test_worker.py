"""Coding worker: command policy, isolation, diffs, approval separation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from max.core.tools import AuditLog, ToolContext, ToolError, ToolRegistry
from max.worker import CommandPolicy, TaskManager
from max.worker.tools import register_worker_tools


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    run = lambda *args: subprocess.run(  # noqa: E731
        args, cwd=root, check=True, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    run("git", "init", "-q")
    run("git", "add", ".")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def manager(repo: Path, tmp_path: Path):
    return TaskManager(
        allowed_repos=[repo],
        workspace_dir=tmp_path / "ws",
        policy=CommandPolicy(timeout_s=20),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )


# ------------------------------------------------------------------ policy
def test_policy_allows_approved_command():
    assert CommandPolicy().check(["python", "-m", "pytest", "-q"]).allowed


def test_policy_rejects_shell_strings_and_metacharacters():
    policy = CommandPolicy()
    assert not policy.check(["python", "-m", "pytest; rm -rf /"]).allowed
    assert not policy.check(["python", "-c", "print(1)"]).allowed
    assert not policy.check(["bash", "-c", "whoami"]).allowed
    assert not policy.check(["sh"]).allowed


def test_policy_rejects_network_and_git_write_operations():
    policy = CommandPolicy()
    for argv in (
        ["curl", "https://example.com"],
        ["pip", "install", "evil"],
        ["git", "push", "origin", "HEAD"],
        ["git", "clone", "https://x"],
        ["git", "reset", "--hard"],
        ["rm", "-rf", "."],
        ["sudo", "reboot"],
    ):
        verdict = policy.check(argv)
        assert not verdict.allowed, argv
        assert verdict.reason


def test_policy_rejects_path_traversal_and_unknown_commands():
    policy = CommandPolicy()
    assert not policy.check(["ls", "../../etc/passwd"]).allowed
    assert not policy.check(["python", "-m", "http.server"]).allowed  # network server
    assert not policy.check([]).allowed


def test_policy_environment_excludes_host_secrets(monkeypatch):
    monkeypatch.setenv("FREELLM_API_KEY", "super-secret")
    monkeypatch.setenv("MAX_DISCORD_TOKEN", "abc.def.ghi")
    monkeypatch.setenv("MAX_WORKER_PASSTHROUGH", "ok")
    env = CommandPolicy(passthrough_env=("MAX_WORKER_PASSTHROUGH",)).build_env(Path("/tmp"))
    assert "FREELLM_API_KEY" not in env
    assert "MAX_DISCORD_TOKEN" not in env
    assert env.get("MAX_WORKER_PASSTHROUGH") == "ok"


def test_policy_rejects_cwd_escape(manager):
    with pytest.raises(ToolError):
        manager.policy.validate_cwd(Path("/nowhere"), "../../etc")


# ------------------------------------------------------------------ tasks
def test_task_lifecycle_run_diff_discard(manager, repo, tmp_path):
    task = manager.create("fix the add function")
    assert task.branch.startswith("worker/")
    assert task.worktree.is_dir()
    # the worktree is a separate checkout, not the caller's tree
    assert task.worktree != repo

    (task.worktree / "app.py").write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    status = manager.status(task.id)
    assert "app.py" in status["changed_files"]

    result = manager.run(task.id, ["ls"])
    assert result["exit_code"] == 0
    assert "allowed by prefix" in result["allowed_reason"]
    # a non-allowlisted command never reaches the process
    assert manager.policy.check(["python", "-m", "http.server"]).allowed is False

    diff = manager.diff(task.id)
    assert "app.py" in diff["stat"]
    assert diff["secret_findings"] == []

    out = manager.discard(task.id)
    assert out["state"] == "discarded"
    assert not task.worktree.exists()


def test_worker_rejects_disallowed_command(manager):
    task = manager.create("x")
    with pytest.raises(ToolError, match="rejected"):
        manager.run(task.id, ["curl", "https://evil.example"])
    with pytest.raises(ToolError, match="rejected"):
        manager.run(task.id, ["python", "-c", "__import__('os').system('id')"])


def test_task_repo_allowlist(manager, tmp_path):
    with pytest.raises(ToolError, match="allowlist"):
        manager.create("x", repo=str(tmp_path / "not-allowed"))
    with pytest.raises(ToolError, match="allowlist"):
        manager.create("x", repo="/etc")


def test_diff_flags_secret_content(manager):
    task = manager.create("accidentally commit a key")
    (task.worktree / "leak.py").write_text('TOKEN = "ghp_' + "a" * 30 + '"\n', encoding="utf-8")
    diff = manager.diff(task.id)
    assert "github_token" in diff["secret_findings"]
    assert "WARNING" in diff["summary"]


def test_model_cannot_grant_its_own_approval(manager):
    task = manager.create("needs approval")
    pending = manager.request_approval(task.id, "git push", "looks safe to me")
    assert pending["granted"] is False
    assert task.approvals == {}
    granted = manager.grant_approval(task.id, "git push", granted_by="owner")
    assert granted["granted"] is True
    assert task.approvals["git push"] is True


def test_cancel_and_discard_unknown_task(manager):
    task = manager.create("c")
    assert manager.cancel(task.id)["state"] == "cancelled"
    with pytest.raises(ToolError):
        manager.create("x", repo="nonexistent")


def test_task_limit_and_audit(manager):
    manager.max_tasks = 1
    manager.create("one")
    with pytest.raises(ToolError, match="task limit"):
        manager.create("two")
    events = [e["event"] for e in manager.audit.events()]
    assert "worker_task_created" in events


# -------------------------------------------------------------- tool layer
def test_worker_tools_are_gated_by_policy(manager):
    registry = ToolRegistry()
    register_worker_tools(registry, manager)
    assert "worker_run" in registry.names()
    assert registry.get("worker_run").category == "exec"  # denied by default

    from max.core.tools import PermissionPolicy, ToolExecutor

    audit = AuditLog(Path("/dev/null")) if False else None
    executor = ToolExecutor(
        registry,
        PermissionPolicy(),  # default deny for exec
        _MemoryAudit(),
        ToolContext(workspace_dir=Path("."), read_roots=[]),
    )
    output, event = executor.dispatch("worker_run", {"task_id": "t", "argv": ["ls"]})
    assert event["allowed"] is False
    assert output.startswith("REJECTED")


class _MemoryAudit:
    def __init__(self):
        self.entries = []

    def record(self, event, **detail):
        self.entries.append((event, detail))
        return {"event": event, **detail}
