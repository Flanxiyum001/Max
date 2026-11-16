"""Offline security tests for inert tool-intent review."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from max_core.gateway.contracts import GatewayResponse, ToolProposal
from max_core.mcp_discovery import ObservedTool
from max_core.mcp_policy import (
    MCPPolicyError,
    ReviewedToolPolicy,
    RiskTier,
    review_gateway_proposals,
)
from max_core.trust import ActorContext


def context(*, session: str = "s1") -> ActorContext:
    return ActorContext("owner-a", str(uuid4()), "actor", session, "device")


def observed(schema: dict[str, Any] | None = None) -> ObservedTool:
    spec = schema if schema is not None else {
        "type": "object",
        "properties": {"task_id": {"type": "string"}, "quiet": {"type": "boolean"}},
        "required": ["task_id"],
    }
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return ObservedTool("reviewed-service", "task_status", encoded,
                        hashlib.sha256(encoded.encode()).hexdigest())


def policy(
    observation: ObservedTool,
    owner: str = "owner-a",
    enabled: bool = True,
    risk: RiskTier = RiskTier.R2,
) -> ReviewedToolPolicy:
    return ReviewedToolPolicy(
        owner, observation.connection_id, observation.name, observation.schema_sha256,
        risk, frozenset({"task_id", "quiet"}), enabled
    )


def response(
    args: dict[str, Any] | None = None,
    name: str = "mcp:reviewed-service:task_status",
) -> GatewayResponse:
    return GatewayResponse(
        "request-1", "mock", "mock", "",
        (ToolProposal("call-1", name, args if args is not None else {"task_id": "abc"}),),
        "tool_calls", None, None,
    )


def test_review_creates_only_inert_binding() -> None:
    tool = observed()
    intent = review_gateway_proposals(context(), response(), [(tool, policy(tool))])[0]
    assert intent.state == "blocked_pending_dispatch"
    assert intent.requires_owner_approval and intent.risk == RiskTier.R2
    assert len(intent.binding_sha256) == 64
    assert "abc" not in repr(intent)


def test_server_readonly_hint_cannot_lower_risk() -> None:
    tool = observed({
        "type": "object",
        "properties": {"task_id": {"type": "string"}, "quiet": {"type": "boolean"}},
        "required": ["task_id"], "readOnlyHint": True,
    })
    intent = review_gateway_proposals(context(), response(), [(tool, policy(tool))])[0]
    assert intent.risk == RiskTier.R2


def test_wrong_owner_disabled_schema_drift_denied() -> None:
    tool = observed()
    for setting in (policy(tool, owner="other"), policy(tool, enabled=False)):
        with pytest.raises(MCPPolicyError, match="unreviewed_tool_denied"):
            review_gateway_proposals(context(), response(), [(tool, setting)])
    drift = observed({
        "type": "object",
        "properties": {"task_id": {"type": "integer"}, "quiet": {"type": "boolean"}},
        "required": ["task_id"],
    })
    with pytest.raises(MCPPolicyError, match="schema_not_reviewed"):
        review_gateway_proposals(context(), response(), [(drift, policy(tool))])


def test_scalar_argument_allowlist_enforced() -> None:
    tool = observed()
    for args in (
        {"task_id": []}, {"task_id": "abc", "shell": "rm -rf /"},
        {"quiet": True}, {"task_id": "a" * 1200},
    ):
        with pytest.raises(MCPPolicyError, match="unreviewed_arguments"):
            review_gateway_proposals(context(), response(args), [(tool, policy(tool))])


def test_intent_binds_session_and_arguments() -> None:
    tool = observed()
    actor = context()
    first = review_gateway_proposals(actor, response(), [(tool, policy(tool))])[0]
    changed_args = review_gateway_proposals(
        actor, response({"task_id": "xyz"}), [(tool, policy(tool))]
    )[0]
    changed_session = review_gateway_proposals(
        replace(actor, session_id="other"), response(), [(tool, policy(tool))]
    )[0]
    assert first.binding_sha256 != changed_args.binding_sha256
    assert first.binding_sha256 != changed_session.binding_sha256


def test_unknown_duplicate_and_ambiguous_fail_closed() -> None:
    tool = observed()
    with pytest.raises(MCPPolicyError, match="unreviewed_tool_denied"):
        review_gateway_proposals(context(), response(name="mcp:other:task_status"),
                                 [(tool, policy(tool))])
    with pytest.raises(MCPPolicyError, match="ambiguous_review"):
        review_gateway_proposals(context(), response(), [(tool, policy(tool))] * 2)
    duplicate = replace(response(), tool_proposals=response().tool_proposals * 2)
    with pytest.raises(MCPPolicyError, match="duplicate_tool_call"):
        review_gateway_proposals(context(), duplicate, [(tool, policy(tool))])
