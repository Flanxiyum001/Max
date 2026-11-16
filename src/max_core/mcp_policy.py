"""Review model tool proposals against exactly pinned MCP observations, without execution."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from max_core.gateway.contracts import GatewayResponse, ToolProposal
from max_core.mcp_discovery import ObservedTool
from max_core.trust import ActorContext

_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_OWNER = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


class MCPPolicyError(ValueError):
    """Stable code: never show tool input, schema or credential."""


class RiskTier(StrEnum):
    R0 = "r0_read"
    R1 = "r1_local"
    R2 = "r2_write"
    R3 = "r3_high_risk"


@dataclass(frozen=True, slots=True)
class ReviewedToolPolicy:
    owner_id: str
    connection_id: str
    tool_name: str
    schema_sha256: str
    risk: RiskTier
    allowed_arguments: frozenset[str]
    enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or _OWNER.fullmatch(self.owner_id) is None:
            raise MCPPolicyError("invalid_reviewed_owner")
        if (
            not isinstance(self.connection_id, str)
            or _NAME.fullmatch(self.connection_id) is None
            or not isinstance(self.tool_name, str)
            or _NAME.fullmatch(self.tool_name) is None
            or not isinstance(self.schema_sha256, str)
            or _SHA.fullmatch(self.schema_sha256) is None
            or not isinstance(self.risk, RiskTier)
            or type(self.enabled) is not bool
            or not isinstance(self.allowed_arguments, frozenset)
            or any(not isinstance(x, str) or _NAME.fullmatch(x) is None
                   for x in self.allowed_arguments)
        ):
            raise MCPPolicyError("invalid_reviewed_policy")

    @property
    def qualified_name(self) -> str:
        return f"mcp:{self.connection_id}:{self.tool_name}"


@dataclass(frozen=True, slots=True)
class PendingToolIntent:
    call_id: str
    qualified_name: str
    schema_sha256: str
    argument_sha256: str
    binding_sha256: str
    risk: RiskTier
    requires_owner_approval: bool
    state: str = "blocked_pending_dispatch"


def _reviewed_schema(observed: ObservedTool, policy: ReviewedToolPolicy) -> dict[str, Any]:
    if (
        not isinstance(observed, ObservedTool)
        or observed.connection_id != policy.connection_id
        or observed.name != policy.tool_name
        or observed.schema_sha256 != policy.schema_sha256
        or not isinstance(observed.schema_json, str)
        or len(observed.schema_json.encode("utf-8")) > 65536
        or hashlib.sha256(observed.schema_json.encode()).hexdigest() != policy.schema_sha256
    ):
        raise MCPPolicyError("schema_not_reviewed")
    try:
        data = json.loads(observed.schema_json)
        if not isinstance(data, dict) or data.get("type") != "object":
            raise ValueError("invalid schema")
        if json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False) != (
            observed.schema_json
        ):
            raise ValueError("noncanonical schema")
        props = data.get("properties", {})
        required = data.get("required", [])
        if (
            not isinstance(props, dict)
            or not isinstance(required, list)
            or any(not isinstance(x, str) for x in required)
            or not policy.allowed_arguments <= props.keys()
            or not set(required) <= policy.allowed_arguments
        ):
            raise ValueError("unreviewed fields")
        return data
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise MCPPolicyError("schema_not_reviewed") from None


def _arguments_hash(
    proposal: ToolProposal, policy: ReviewedToolPolicy, schema: dict[str, Any]
) -> str:
    if (
        not isinstance(proposal, ToolProposal)
        or not isinstance(proposal.call_id, str)
        or _NAME.fullmatch(proposal.call_id) is None
        or proposal.name != policy.qualified_name
        or not isinstance(proposal.arguments, dict)
        or not set(proposal.arguments) <= policy.allowed_arguments
        or not set(schema.get("required", [])) <= proposal.arguments.keys()
    ):
        raise MCPPolicyError("unreviewed_arguments")
    properties = schema["properties"]
    for key, value in proposal.arguments.items():
        descriptor = properties.get(key)
        if not isinstance(descriptor, dict):
            raise MCPPolicyError("unreviewed_arguments")
        type_name = descriptor.get("type")
        valid = (
            (type_name == "string" and type(value) is str and len(value) <= 1024)
            or (type_name == "integer" and type(value) is int and abs(value) <= 1_000_000_000)
            or (type_name == "number" and type(value) in (int, float)
                and math.isfinite(value) and abs(value) <= 1_000_000_000)
            or (type_name == "boolean" and type(value) is bool)
            or (type_name == "null" and value is None)
        )
        if not valid or ("enum" in descriptor and value not in descriptor["enum"]):
            raise MCPPolicyError("unreviewed_arguments")
    try:
        canonical = json.dumps(
            proposal.arguments, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise MCPPolicyError("unreviewed_arguments") from None
    if len(canonical) > 8192:
        raise MCPPolicyError("unreviewed_arguments")
    return hashlib.sha256(canonical).hexdigest()


def review_gateway_proposals(
    context: ActorContext,
    response: GatewayResponse,
    reviewed: Sequence[tuple[ObservedTool, ReviewedToolPolicy]],
) -> tuple[PendingToolIntent, ...]:
    """All-or-nothing inert intent review; NO grants, network, worker or timeline."""
    if (
        not isinstance(context, ActorContext)
        or not isinstance(response, GatewayResponse)
        or len(response.tool_proposals) > 8
    ):
        raise MCPPolicyError("invalid_batch")
    policies: dict[str, tuple[ObservedTool, ReviewedToolPolicy]] = {}
    for observation, policy in reviewed:
        if not isinstance(policy, ReviewedToolPolicy) or not isinstance(
            observation, ObservedTool
        ):
            raise MCPPolicyError("invalid_registry")
        if policy.owner_id != context.owner_id or not policy.enabled:
            continue
        if policy.qualified_name in policies:
            raise MCPPolicyError("ambiguous_review")
        policies[policy.qualified_name] = observation, policy
    seen: set[str] = set()
    result: list[PendingToolIntent] = []
    for proposal in response.tool_proposals:
        if not isinstance(proposal, ToolProposal) or not isinstance(proposal.name, str):
            raise MCPPolicyError("invalid_batch")
        entry = policies.get(proposal.name)
        if entry is None:
            raise MCPPolicyError("unreviewed_tool_denied")
        observation, policy = entry
        digest = _arguments_hash(proposal, policy, _reviewed_schema(observation, policy))
        if proposal.call_id in seen:
            raise MCPPolicyError("duplicate_tool_call")
        seen.add(proposal.call_id)
        binding = json.dumps(
            {
                "owner": context.owner_id, "task": context.task_id,
                "actor": context.actor_id, "session": context.session_id,
                "device": context.device_id, "request": response.request_id,
                "provider": response.provider_id, "model": response.model_id,
                "call": proposal.call_id, "tool": policy.qualified_name,
                "schema": policy.schema_sha256, "args": digest, "risk": policy.risk.value,
                "version": 1,
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
        result.append(
            PendingToolIntent(
                proposal.call_id, policy.qualified_name, policy.schema_sha256,
                digest, hashlib.sha256(binding).hexdigest(), policy.risk,
                policy.risk != RiskTier.R0,
            )
        )
    return tuple(result)
