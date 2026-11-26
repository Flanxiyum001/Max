"""Security tests for the allowlisted local stdio MCP bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

from max_core.gateway.contracts import ToolProposal
from max_core.mcp_discovery import DiscoveryError
from max_core.mcp_execute import MCPToolExecutor
from max_core.mcp_policy import ReviewedToolPolicy
from max_core.mcp_policy import RiskTier as ReviewedRiskTier
from max_core.mcp_stdio import (
    StdioBridgeError,
    StdioMCPConnection,
    allowlisted_stdio_connection,
    discover_stdio_tools,
)
from max_core.store import TaskStore
from tests.stdio_fixture import write_stdio_fixture_server


def _connection(
    tmp_path: Path,
    server: Path,
    *,
    mode: str = "normal",
    discovery_approved: bool = True,
) -> StdioMCPConnection:
    executable = str(Path(sys.executable).resolve())
    args = [str(server)]
    if mode != "normal":
        args.append(mode)
    return allowlisted_stdio_connection(
        "fixture",
        executable,
        args,
        allowed_commands=(executable,),
        discovery_approved=discovery_approved,
    )


def test_empty_allowlist_denies_every_stdio_command() -> None:
    with pytest.raises(StdioBridgeError, match="stdio_not_allowed"):
        allowlisted_stdio_connection("fixture", "/usr/bin/git", (), allowed_commands=())


def test_unallowlisted_command_is_denied(tmp_path: Path) -> None:
    server = write_stdio_fixture_server(tmp_path)
    executable = str(Path(sys.executable).resolve())
    with pytest.raises(StdioBridgeError, match="stdio_command_not_allowed"):
        allowlisted_stdio_connection(
            "fixture", executable, (str(server),), allowed_commands=("/bin/true",)
        )
    with pytest.raises(StdioBridgeError, match="stdio_command_not_allowed"):
        allowlisted_stdio_connection(
            "fixture", "sys.executable", (), allowed_commands=("/bin/true",)
        )


def test_connection_rejects_relative_and_escape_paths() -> None:
    with pytest.raises(DiscoveryError, match="invalid_mcp_stdio_connection"):
        StdioMCPConnection("fixture", "relative/python", ())
    with pytest.raises(DiscoveryError, match="invalid_mcp_stdio_connection"):
        StdioMCPConnection("fixture", "/bin/../../usr/bin/python", ())


def test_discovery_requires_explicit_approval(tmp_path: Path) -> None:
    server = write_stdio_fixture_server(tmp_path)
    connection = _connection(tmp_path, server, discovery_approved=False)
    with pytest.raises(DiscoveryError, match="mcp_discovery_not_approved"):
        asyncio.run(discover_stdio_tools(connection))


def test_discovery_observes_pinned_schema_and_hint(tmp_path: Path) -> None:
    server = write_stdio_fixture_server(tmp_path)
    connection = _connection(tmp_path, server)
    observed = asyncio.run(discover_stdio_tools(connection, env_source={"PATH": "/usr/bin"}))
    assert len(observed) == 1
    tool = observed[0]
    assert tool.connection_id == "fixture"
    assert tool.name == "probe"
    assert tool.read_only_hint is True
    schema = json.dumps(
        {"type": "object", "properties": {}, "additionalProperties": False},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert tool.schema_json == schema
    assert tool.schema_sha256 == hashlib.sha256(schema.encode()).hexdigest()


def make_stdio_executor(store: TaskStore, connection: StdioMCPConnection) -> MCPToolExecutor:
    observation = asyncio.run(discover_stdio_tools(connection))[0]
    policy = ReviewedToolPolicy(
        "owner",
        observation.connection_id,
        observation.name,
        observation.schema_sha256,
        ReviewedRiskTier.R1,
        frozenset(),
        True,
    )
    return MCPToolExecutor(
        task_store=store, connections=(connection,), reviewed=((observation, policy),)
    )


def test_stdio_execution_requires_and_consumes_single_use_approval(
    store: TaskStore, tmp_path: Path
) -> None:
    server = write_stdio_fixture_server(tmp_path)
    connection = _connection(tmp_path, server)
    executor = make_stdio_executor(store, connection)
    proposal = ToolProposal("c1", "mcp.fixture.probe", {})
    requested = executor.execute(proposal)
    assert requested.status == "awaiting_approval"
    assert requested.output == "owner_approval_required"
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    completed = executor.execute(proposal, approval_id=approval_id)
    assert completed.status == "completed"
    replayed = executor.execute(proposal, approval_id=approval_id)
    assert replayed.status == "denied"
    assert replayed.output == "approval_already_consumed"


def test_stdio_child_environment_is_scrubbed(
    store: TaskStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_STDIO_TEST_SECRET", "do-not-leak")
    server = write_stdio_fixture_server(tmp_path)
    connection = _connection(tmp_path, server)
    executor = make_stdio_executor(store, connection)
    proposal = ToolProposal("c1", "mcp.fixture.probe", {})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    completed = executor.execute(proposal, approval_id=approval_id)
    assert completed.status == "completed"
    output = completed.output
    assert isinstance(output, dict)
    payload = output["result"]
    assert "PATH" in payload and "MAX_STDIO_TEST_SECRET" not in payload


def test_stdio_schema_drift_denies_dispatch(store: TaskStore, tmp_path: Path) -> None:
    server = write_stdio_fixture_server(tmp_path)
    observed_connection = _connection(tmp_path, server)
    observation = asyncio.run(discover_stdio_tools(observed_connection))[0]
    drifted = _connection(tmp_path, server, mode="drift")
    policy = ReviewedToolPolicy(
        "owner",
        observation.connection_id,
        observation.name,
        observation.schema_sha256,
        ReviewedRiskTier.R1,
        frozenset(),
        True,
    )
    executor = MCPToolExecutor(
        task_store=store,
        connections=(drifted,),
        reviewed=((observation, policy),),
    )
    proposal = ToolProposal("c1", "mcp.fixture.probe", {})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    denied = executor.execute(proposal, approval_id=approval_id)
    assert denied.status == "denied"
    assert denied.output == "schema_drifted"


def test_stateful_stdio_keeps_one_server_and_preserves_approval_gate(
    store: TaskStore, tmp_path: Path
) -> None:
    server = write_stdio_fixture_server(tmp_path)
    base = _connection(tmp_path, server, mode="counter")
    connection = StdioMCPConnection(base.connection_id, base.command, base.args, True, True)
    executor = make_stdio_executor(store, connection)
    try:
        for index in (1, 2):
            proposal = ToolProposal(f"call-{index}", "mcp.fixture.probe", {})
            pending = executor.execute(proposal)
            assert pending.status == "awaiting_approval"
            assert pending.approval_id is not None
            executor.trust_service.decide("owner", pending.approval_id, True)
            done = executor.execute(proposal, approval_id=pending.approval_id)
            assert done.status == "completed"
            assert isinstance(done.output, dict)
            assert json.loads(done.output["result"])["calls"] == index
    finally:
        executor.close()
