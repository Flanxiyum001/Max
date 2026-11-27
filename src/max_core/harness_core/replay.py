"""Keyless snapshot testing: record model replies once, replay deterministically."""

from __future__ import annotations

import json
from pathlib import Path

from max_core.agent_harness import CompletionGateway
from max_core.gateway.contracts import GatewayRequest, GatewayResponse


class RecordingGateway:
    def __init__(self, inner: CompletionGateway) -> None:
        self._inner = inner
        self.replies: list[str] = []

    async def complete(self, request: GatewayRequest) -> GatewayResponse:
        response = await self._inner.complete(request)
        self.replies.append(response.content)
        return response

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"version": 1, "replies": self.replies}, indent=1) + "\n")


class ReplayGateway:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    @classmethod
    def load(cls, path: Path) -> ReplayGateway:
        data = json.loads(path.read_text())
        if data.get("version") != 1 or not all(isinstance(r, str) for r in data["replies"]):
            raise ValueError("unsupported_snapshot")
        return cls(data["replies"])

    async def complete(self, request: GatewayRequest) -> GatewayResponse:
        if not self._replies:
            raise RuntimeError("snapshot_exhausted")
        content = self._replies.pop(0)
        return GatewayResponse(
            request.request_id, "replay", "replay", content, (), "stop", None, None
        )
