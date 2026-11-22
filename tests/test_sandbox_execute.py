"""Sandboxed code.execute_python: R2 approval gate, hardened docker argv, env scrubbing."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from max_core.bounded_commands import BoundedCommandError
from max_core.container_runner import run_python_sandboxed, validate_python_script
from max_core.gateway.contracts import ToolProposal
from max_core.local_tools import LocalToolExecutor, LocalToolRegistry
from max_core.store import TaskStore
from max_core.trust import RiskTier, TrustService

_STUB_DOCKER = """\
#!/usr/bin/env python3
import json
import os
import sys

print("STUB-ARGV " + json.dumps(sys.argv[1:]))
print("STUB-ENV " + json.dumps(sorted(os.environ)))
sys.exit(0)
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def stub_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(_STUB_DOCKER, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return stub


def execute_approved(
    executor: LocalToolExecutor, registry: LocalToolRegistry, store: TaskStore, script: object
) -> object:
    requested = executor.execute(ToolProposal("call-1", "code.execute_python", {"script": script}))
    assert requested.status == "awaiting_approval"
    approval_id = requested.approval_id
    assert approval_id is not None
    TrustService(store.engine, registry=registry.policy_registry()).decide(
        "owner", approval_id, True
    )
    return executor.execute(
        ToolProposal("call-1", "code.execute_python", {"script": script}), approval_id=approval_id
    )


def make_executor(store: TaskStore, workspace: Path) -> tuple[LocalToolExecutor, LocalToolRegistry]:
    registry = LocalToolRegistry(task_store=store, workspace_root=workspace, owner_id="owner")
    return LocalToolExecutor(registry=registry, task_store=store), registry


def mounted_workspace(argv: list[str]) -> str:
    for index, part in enumerate(argv):
        if part == "-v":
            return argv[index + 1].split(":")[0]
    raise AssertionError("no mount found")


def test_execute_python_is_r2_and_requires_approval(store: TaskStore, workspace: Path) -> None:
    executor, registry = make_executor(store, workspace)
    spec = registry.get_spec("code.execute_python")
    assert spec is not None
    assert spec.risk_tier is RiskTier.R2
    assert spec.requires_approval is True
    requested = executor.execute(
        ToolProposal("call-1", "code.execute_python", {"script": "print(1)"})
    )
    assert requested.status == "awaiting_approval"
    assert requested.output == "owner_approval_required"


def test_execute_python_script_validation_denies_bad_payload(
    store: TaskStore, workspace: Path
) -> None:
    executor, _registry = make_executor(store, workspace)
    for script in (None, 42, "", "x" * 33_000, "print(\x00)"):
        summary = executor.execute(
            ToolProposal(f"call-{uuid4()}", "code.execute_python", {"script": script})
        )
        assert summary.status == "denied"
    extra = executor.execute(ToolProposal("call-1", "code.execute_python", {"script": "1", "b": 2}))
    assert extra.status == "denied"


def test_execute_python_uses_hardened_docker_argv(
    store: TaskStore, workspace: Path, stub_docker: Path
) -> None:
    executor, registry = make_executor(store, workspace)
    completed = execute_approved(executor, registry, store, "print('hi')")
    assert completed.status == "completed"
    output = completed.output
    assert isinstance(output, dict)
    assert output["exit_code"] == 0
    argv_line = next(
        line for line in str(output["output"]).splitlines() if line.startswith("STUB-ARGV")
    )
    argv = json.loads(argv_line.split(" ", 1)[1])
    assert argv[0] == "run"
    for flag in ("--rm", "--read-only", "--cap-drop"):
        assert flag in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--memory") + 1] == "256m"
    assert argv[argv.index("--cpus") + 1] == "1.0"
    assert argv[argv.index("--pids-limit") + 1] == "64"
    assert argv[argv.index("-u") + 1] == "65534:65534"
    assert argv[argv.index("--tmpfs") + 1] == "/tmp:rw,size=8m,noexec,nodev,nosuid"
    assert argv[argv.index("-v") + 1].endswith(":/work:rw")
    scratch = Path(argv[argv.index("-v") + 1].split(":")[0])
    assert scratch.parent == workspace / ".max-scratch"
    assert argv[argv.index("python") :] == ["python", "-I", "-B", "/work/script.py"]
    assert not (workspace / ".max-scratch").exists()


def test_execute_python_does_not_inherit_host_secrets(
    store: TaskStore,
    workspace: Path,
    stub_docker: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_SANDBOX_TEST_SECRET", "do-not-leak")
    executor, registry = make_executor(store, workspace)
    completed = execute_approved(executor, registry, store, "print('hi')")
    assert completed.status == "completed"
    output = str(completed.output)
    assert "PATH" in output
    assert "MAX_SANDBOX_TEST_SECRET" not in output


def test_execute_python_without_docker_fails_closed(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(BoundedCommandError, match="tool_unavailable"):
        run_python_sandboxed(workspace, "print(1)")


def test_validate_python_script_bounds() -> None:
    assert validate_python_script("print(1)") == "print(1)"
    for bad in (None, 3, "", "x" * 33_000, "print(\x00)"):
        with pytest.raises(BoundedCommandError, match="invalid_script"):
            validate_python_script(bad)


def test_sandbox_refuses_unknown_image(workspace: Path) -> None:
    with pytest.raises(BoundedCommandError, match="invalid_script_image"):
        run_python_sandboxed(workspace, "print(1)", image="ubuntu:latest")
