"""Scoped developer tools: git.status, git.diff and test.run_pytest behind R1 Trust approval."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from max_core.gateway.contracts import ToolProposal
from max_core.local_tools import LocalToolExecutor, LocalToolRegistry
from max_core.store import TaskStore
from max_core.trust import TrustService


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(root)}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env, timeout=30)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "owner@example.invalid"],
        check=True,
        env=env,
        timeout=30,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Owner"],
        check=True,
        env=env,
        timeout=30,
    )
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True, env=env, timeout=30)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "init"],
        check=True,
        env=env,
        timeout=30,
    )
    return root


@pytest.fixture
def repo(store: TaskStore, tmp_path: Path) -> tuple[LocalToolExecutor, Path, TaskStore]:
    workspace = _init_repo(tmp_path / "workspace")
    registry = LocalToolRegistry(task_store=store, workspace_root=workspace, owner_id="owner")
    executor = LocalToolExecutor(registry=registry, task_store=store)
    return executor, workspace, store


def approve(store: TaskStore, owner: str, registry: LocalToolRegistry, approval_id: str) -> None:
    TrustService(store.engine, registry=registry.policy_registry()).decide(owner, approval_id, True)


def test_dev_tools_are_r1_and_need_approval(
    repo: tuple[LocalToolExecutor, Path, TaskStore],
) -> None:
    executor, workspace, _store = repo
    names = {definition.name for definition in executor.tool_definitions()}
    assert {"git.status", "git.diff", "test.run_pytest"} <= names
    requested = executor.execute(ToolProposal("c1", "git.status", {}))
    assert requested.status == "awaiting_approval"
    assert requested.output == "owner_approval_required"
    assert requested.approval_id
    assert workspace.exists()


def test_git_status_reports_clean_tree_after_approval(
    repo: tuple[LocalToolExecutor, Path, TaskStore],
) -> None:
    executor, workspace, store = repo
    registry = LocalToolRegistry(task_store=store, workspace_root=workspace, owner_id="owner")
    requested = executor.execute(ToolProposal("c1", "git.status", {}))
    assert requested.status == "awaiting_approval"
    approval_id = requested.approval_id
    assert approval_id
    approve(store, "owner", registry, approval_id)
    completed = executor.execute(ToolProposal("c1", "git.status", {}), approval_id=approval_id)
    assert completed.status == "completed"
    output = completed.output
    assert isinstance(output, dict)
    assert output["exit_code"] == 0
    assert output["tool"] == "git.status"
    assert output["output"] == ""


def test_git_diff_reports_uncommitted_change_after_approval(
    repo: tuple[LocalToolExecutor, Path, TaskStore],
) -> None:
    executor, workspace, store = repo
    registry = LocalToolRegistry(task_store=store, workspace_root=workspace, owner_id="owner")
    (workspace / "README.md").write_text("changed\n", encoding="utf-8")
    requested = executor.execute(ToolProposal("c1", "git.diff", {"path": "README.md"}))
    approval_id = requested.approval_id
    assert approval_id
    approve(store, "owner", registry, approval_id)
    completed = executor.execute(
        ToolProposal("c1", "git.diff", {"path": "README.md"}), approval_id=approval_id
    )
    assert completed.status == "completed"
    output = completed.output
    assert isinstance(output, dict)
    assert output["tool"] == "git.diff"
    assert "+changed" in str(output["output"])


def test_tool_arguments_cannot_leave_the_workspace(
    repo: tuple[LocalToolExecutor, Path, TaskStore],
) -> None:
    executor, _workspace, _store = repo
    for name in ("git.diff", "test.run_pytest"):
        summary = executor.execute(ToolProposal(f"c-{uuid4()}", name, {"path": "../outside"}))
        assert summary.status == "denied"
        assert summary.output == "path_outside_root"
        flag = executor.execute(ToolProposal(f"c-{uuid4()}", name, {"path": "-prefix"}))
        assert flag.status == "denied"


def test_pytest_runs_a_workspace_test_after_approval(
    repo: tuple[LocalToolExecutor, Path, TaskStore],
) -> None:
    executor, workspace, store = repo
    tests_dir = workspace / "pkgtests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_demo.py").write_text(
        "def test_pass() -> None:\n    assert True\n", encoding="utf-8"
    )
    registry = LocalToolRegistry(task_store=store, workspace_root=workspace, owner_id="owner")
    requested = executor.execute(ToolProposal("c1", "test.run_pytest", {"path": "pkgtests"}))
    approval_id = requested.approval_id
    assert approval_id
    approve(store, "owner", registry, approval_id)
    completed = executor.execute(
        ToolProposal("c1", "test.run_pytest", {"path": "pkgtests"}), approval_id=approval_id
    )
    assert completed.status == "completed"
    output = completed.output
    assert isinstance(output, dict)
    assert output["tool"] == "test.run_pytest"
    assert "1 passed" in str(output["output"])
