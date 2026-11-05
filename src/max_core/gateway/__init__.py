"""MAX-owned internal model gateway. No credentials or endpoints are auto-configured."""

from max_core.gateway.adapters import HTTPAdapter
from max_core.gateway.contracts import (
    ChatMessage,
    Dialect,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    InferenceDestination,
    ModelSpec,
    Privacy,
    StreamEvent,
    TokenUsage,
    ToolProposal,
)
from max_core.gateway.router import Gateway

__all__ = [
    "ChatMessage",
    "Dialect",
    "Gateway",
    "GatewayError",
    "GatewayRequest",
    "GatewayResponse",
    "HTTPAdapter",
    "InferenceDestination",
    "ModelSpec",
    "Privacy",
    "StreamEvent",
    "TokenUsage",
    "ToolProposal",
]
