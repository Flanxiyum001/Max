"""Security tests for live MCP execution: approvals, drift, bounded output, no egress pre-approval."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from max_core.gateway.contracts import ToolProposal
from max_core.mcp_discovery import MCPConnection, ObservedTool
from max_core.mcp_execute import (
    CompositeToolExecutor,
    McpExecuteError,
    MCPToolExecutor,
    ToolExecutionSummary,
    auto_risk,
    build_mcp_policy_registry,
    dotted_tool_name,
)
from max_core.mcp_policy import ReviewedToolPolicy
from max_core.mcp_policy import RiskTier as ReviewedRiskTier
from max_core.store import TaskStore
from max_core.trust import RiskTier, TrustService

_SCHEMA = {
    "type": "object",
    "properties": {"topic": {"type": "string"}, "quiet": {"type": "boolean"}},
    "required": ["topic"],
}
_CONNECTION = MCPConnection("notes", "https://mcp.example/mcp", True, None)


def observed(read_only: bool = True) -> ObservedTool:
    encoded = json.dumps(_SCHEMA, sort_keys=True, separators=(",", ":"))
    return ObservedTool(
        "notes",
        "search",
        encoded,
        hashlib.sha256(encoded.encode()).hexdigest(),
        read_only,
    )


def policy(observation: ObservedTool, *, enabled: bool = True) -> ReviewedToolPolicy:
    return ReviewedToolPolicy(
        "owner",
        observation.connection_id,
        observation.name,
        observation.schema_sha256,
        ReviewedRiskTier.R2,
        frozenset({"topic", "quiet"}),
        enabled,
    )


def call_fixture(
    *,
    listed_schema: dict[str, object] | None = None,
    call_result: dict[str, object] | None = None,
    drop_tool: bool = False,
) -> tuple[httpx.MockTransport, list[dict[str, object]]]:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        calls.append(message)
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif message["method"] == "tools/list":
            if drop_tool or listed_schema is not None:
                schema = listed_schema or {"type": "object", "properties": {}}
                tool = {
                    "name": "search",
                    "inputSchema": schema,
                    "annotations": {"readOnlyHint": True},
                }
            else:
                tool = {
                    "name": "search",
                    "inputSchema": _SCHEMA,
                    "annotations": {"readOnlyHint": True},
                }
            result = {"tools": [tool]}
        else:
            assert message["method"] == "tools/call"
            arguments = message["params"]["arguments"]
            result = call_result or {
                "content": [{"type": "text", "text": f"found:{arguments['topic']}"}],
                "isError": False,
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    return httpx.MockTransport(handler), calls


def make_executor(
    store: TaskStore,
    transport: httpx.AsyncBaseTransport,
    *,
    read_only: bool = True,
    enabled: bool = True,
    owner_id: str = "owner",
) -> MCPToolExecutor:
    observation = observed(read_only)
    return MCPToolExecutor(
        task_store=store,
        connections=(_CONNECTION,),
        reviewed=((observation, policy(observation, enabled=enabled)),),
        owner_id=owner_id,
        transport=transport,
    )


def test_auto_risk_labels_hint_r1_and_absence_r2() -> None:
    assert auto_risk(observed(True)) is RiskTier.R1
    assert auto_risk(observed(False)) is RiskTier.R2


def test_registry_uses_dotted_names_and_rejects_ambiguity() -> None:
    observation = observed()
    registry = build_mcp_policy_registry(
        ((observation, policy(observation)),),
        connections=(_CONNECTION,),
        owner_id="owner",
    )
    assert registry.names() == frozenset({"mcp.notes.search"})
    assert dotted_tool_name("notes", "search") == "mcp.notes.search"
    duplicated = (
        (observation, policy(observation)),
        (observed(), policy(observed())),
    )
    with pytest.raises(McpExecuteError, match="ambiguous_mcp_tool_name"):
        build_mcp_policy_registry(duplicated, connections=(_CONNECTION,), owner_id="owner")


def test_disabled_and_cross_owner_reviewed_policies_never_register(store: TaskStore) -> None:
    transport, _ = call_fixture()
    with pytest.raises(McpExecuteError, match="invalid_mcp_live_policy"):
        make_executor(store, transport, owner_id="someone-else")
    disabled = make_executor(store, transport, enabled=False)
    assert disabled.tool_definitions() == ()
    summary = disabled.execute(ToolProposal("c1", "mcp.notes.search", {"topic": "x"}))
    assert summary.status == "denied"
    assert summary.output == "capability_denied"


def test_high_risk_reviewed_policies_are_rejected(store: TaskStore) -> None:
    observation = observed()
    reviewed_policy = ReviewedToolPolicy(
        "owner",
        observation.connection_id,
        observation.name,
        observation.schema_sha256,
        ReviewedRiskTier.R3,
        frozenset({"topic", "quiet"}),
        True,
    )
    with pytest.raises(McpExecuteError, match="invalid_mcp_live_policy"):
        MCPToolExecutor(
            task_store=store,
            connections=(_CONNECTION,),
            reviewed=((observation, reviewed_policy),),
        )


def test_tool_definitions_expose_dotted_names_and_reviewed_schema(store: TaskStore) -> None:
    transport, _ = call_fixture()
    executor = make_executor(store, transport)
    tools = executor.tool_definitions()
    assert [tool.name for tool in tools] == ["mcp.notes.search"]
    assert "search" in tools[0].description
    assert tool_definitions_params(tools[0].parameters)


def tool_definitions_params(parameters: dict[str, object]) -> bool:
    return parameters["type"] == "object" and "topic" in parameters["properties"]  # type: ignore[operator]


def test_no_network_egress_before_owner_approval(store: TaskStore) -> None:
    transport, calls = call_fixture()
    executor = make_executor(store, transport)
    summary = executor.execute(ToolProposal("c1", "mcp.notes.search", {"topic": "x"}))
    assert summary.status == "awaiting_approval"
    assert summary.output == "owner_approval_required"
    assert summary.approval_id
    assert calls == []


def test_approved_proposal_dispatches_once_with_validated_arguments(
    store: TaskStore,
) -> None:
    transport, calls = call_fixture()
    executor = make_executor(store, transport)
    proposal = ToolProposal("c1", "mcp.notes.search", {"topic": "x", "quiet": True})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    completed = executor.execute(proposal, approval_id=approval_id)
    assert completed.status == "completed"
    output = completed.output
    assert isinstance(output, dict) and output["result"] == "found:x"
    tool_calls = [message for message in calls if message["method"] == "tools/call"]
    assert len(tool_calls) == 1
    params = tool_calls[0]["params"]
    assert params == {"name": "search", "arguments": {"topic": "x", "quiet": True}}  # type: ignore[union-attr,index]


def test_dispatch_requires_single_use_grant(store: TaskStore) -> None:
    transport, _ = call_fixture()
    executor = make_executor(store, transport)
    proposal = ToolProposal("c1", "mcp.notes.search", {"topic": "x"})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    first = executor.execute(proposal, approval_id=approval_id)
    assert first.status == "completed"
    second = executor.execute(proposal, approval_id=approval_id)
    assert second.status == "denied"
    assert second.output == "approval_already_consumed"


def test_owner_denied_grant_maps_to_owner_denied(store: TaskStore) -> None:
    transport, _ = call_fixture()
    executor = make_executor(store, transport)
    proposal = ToolProposal("c1", "mcp.notes.search", {"topic": "x"})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, False)
    denied = executor.execute(proposal, approval_id=approval_id)
    assert denied.status == "denied"
    assert denied.output == "owner_denied"
    assert executor.trust_service.list_pending("owner") == []


def test_unreviewed_arguments_are_denied(store: TaskStore) -> None:
    transport, _ = call_fixture()
    executor = make_executor(store, transport)
    summary = executor.execute(ToolProposal("c1", "mcp.notes.search", {"topic": "x", "extra": "y"}))
    assert summary.status == "denied"
    assert summary.output == "unreviewed_arguments"


def test_unknown_tool_names_are_denied(store: TaskStore) -> None:
    transport, _ = call_fixture()
    executor = make_executor(store, transport)
    for name in ("task.status", "mcp.notes.unknown", "mcp", "mcp.other.search"):
        summary = executor.execute(ToolProposal("c1", name, {}))
        assert summary.status == "denied"
        assert summary.output == "capability_denied"


def test_schema_drift_before_dispatch_is_denied(store: TaskStore) -> None:
    changed = {
        "type": "object",
        "properties": {"topic": {"type": "integer"}},
        "required": ["topic"],
    }
    transport, _ = call_fixture(listed_schema=changed)
    executor = make_executor(store, transport)
    proposal = ToolProposal("c1", "mcp.notes.search", {"topic": "x"})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    summary = executor.execute(proposal, approval_id=approval_id)
    assert summary.status == "denied"
    assert summary.output == "schema_drifted"


def test_missing_live_tool_is_denied_as_drift(store: TaskStore) -> None:
    transport, _ = call_fixture(drop_tool=True)
    executor = make_executor(store, transport)
    proposal = ToolProposal("c1", "mcp.notes.search", {"topic": "x"})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    summary = executor.execute(proposal, approval_id=approval_id)
    assert summary.status == "denied"
    assert summary.output == "schema_drifted"


def test_upstream_tool_error_maps_to_mcp_tool_failed(store: TaskStore) -> None:
    transport, _ = call_fixture(call_result={"content": [], "isError": True})
    executor = make_executor(store, transport)
    proposal = ToolProposal("c1", "mcp.notes.search", {"topic": "x"})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    summary = executor.execute(proposal, approval_id=approval_id)
    assert summary.status == "failed"
    assert summary.output == "mcp_tool_failed"


def test_oversized_tool_output_is_bounded(store: TaskStore) -> None:
    transport, _ = call_fixture(
        call_result={"content": [{"type": "text", "text": "x" * (16_001 + 256)}], "isError": False}
    )
    executor = make_executor(store, transport)
    proposal = ToolProposal("c1", "mcp.notes.search", {"topic": "x"})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    summary = executor.execute(proposal, approval_id=approval_id)
    assert summary.status == "completed"
    output = summary.output
    assert isinstance(output, dict)
    result = output["result"]
    assert isinstance(result, str)
    assert len(result) <= 16_050
    assert result.endswith("(mcp_tool_output_truncated)")


def test_executor_rejects_config_without_trust_or_store() -> None:
    observation = observed()
    with pytest.raises(ValueError, match="requires trust or task_store"):
        MCPToolExecutor(
            connections=(_CONNECTION,),
            reviewed=((observation, policy(observation)),),
        )


def test_composite_executor_routes_by_dotted_prefix(store: TaskStore) -> None:
    class FakePrimary:
        def tool_definitions(self) -> tuple[object, ...]:
            return ()

        def execute(
            self, proposal: ToolProposal, *, approval_id: str | None = None
        ) -> ToolExecutionSummary:
            return ToolExecutionSummary(proposal.call_id, proposal.name, {}, "completed", "local")

    transport, _ = call_fixture()
    composite = CompositeToolExecutor(FakePrimary(), make_executor(store, transport))
    local = composite.execute(ToolProposal("c1", "task.status", {}))
    assert local.tool_name == "task.status" and local.status == "completed"
    remote = composite.execute(ToolProposal("c2", "mcp.notes.search", {"topic": "x"}))
    assert remote.status == "awaiting_approval"


def test_trust_registry_provided_by_host_is_supported(store: TaskStore) -> None:
    observation = observed()
    registry = build_mcp_policy_registry(
        ((observation, policy(observation)),),
        connections=(_CONNECTION,),
        owner_id="owner",
    )
    trust = TrustService(store.engine, registry=registry)
    transport, _ = call_fixture()
    executor = MCPToolExecutor(
        trust=trust,
        connections=(_CONNECTION,),
        reviewed=((observation, policy(observation)),),
        transport=transport,
    )
    summary = executor.execute(ToolProposal("c1", "mcp.notes.search", {"topic": "x"}))
    assert summary.status == "awaiting_approval"


def test_bearer_token_flows_only_in_authorization_header(store: TaskStore) -> None:
    secured = MCPConnection("notes", "https://mcp.example/mcp", True, "fixture-not-a-secret")
    observation = observed()
    transport, calls = call_fixture()
    executor = MCPToolExecutor(
        task_store=store,
        connections=(secured,),
        reviewed=((observation, policy(observation)),),
        transport=transport,
    )
    proposal = ToolProposal("c1", "mcp.notes.search", {"topic": "x"})
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    executor.trust_service.decide("owner", approval_id, True)
    completed = executor.execute(proposal, approval_id=approval_id)
    assert completed.status == "completed"
    tool_calls = [message for message in calls if message["method"] == "tools/call"]
    assert len(tool_calls) == 1
    assert "fixture-not-a-secret" not in json.dumps(calls, default=str)


def test_trust_error_from_request_maps_to_denied_summary(store: TaskStore) -> None:
    transport, _ = call_fixture()
    executor = make_executor(store, transport)
    summary = executor.execute(ToolProposal("bad call id!", "mcp.notes.search", {"topic": "x"}))
    assert summary.status == "denied"


def test_execute_validates_actor_configuration(store: TaskStore) -> None:
    transport, _ = call_fixture()
    with pytest.raises(ValueError, match="invalid_owner_id"):
        make_executor(store, transport, owner_id="bad owner!")
    with pytest.raises(ValueError, match="invalid_actor_scope"):
        MCPToolExecutor(
            task_store=store, connections=(_CONNECTION,), reviewed=(), actor_id="x" * 65
        )
