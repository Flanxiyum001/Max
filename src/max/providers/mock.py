"""Deterministic mock provider used by tests, benchmarks and offline runs.

A *script* is an ordered list of response dicts. Each call pops the next
entry; when the script is exhausted the provider falls back to
``default_text``. Entries:

    {"text": "..."}                          -> plain assistant reply
    {"tool_calls": [{"name": .., "arguments": {...}}]}
    {"error": "rate_limited" | "timeout" | "server_error" | "auth"}
    {"delay_s": 0.01}                        -> simulated latency

The provider also records every request so tests can assert what the model
actually saw (e.g. that conversation history was included).
"""

from __future__ import annotations

import json
import time
from typing import Any

from .base import (
    ChatProvider,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimit,
    ProviderResult,
    ProviderServerError,
    ProviderTimeout,
    ToolCall,
)

_ERRORS: dict[str, type[ProviderError]] = {
    "rate_limited": ProviderRateLimit,
    "timeout": ProviderTimeout,
    "server_error": ProviderServerError,
    "auth": ProviderAuthError,
}


class MockProvider(ChatProvider):
    name = "mock"

    def __init__(
        self,
        script: list[dict] | None = None,
        default_text: str | None = None,
        fail: str | None = None,
        sleep=time.sleep,
    ):
        self.script = list(script or [])
        self.fail = fail
        self.default_text = default_text
        self._sleep = sleep
        self.requests: list[dict[str, Any]] = []
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> ProviderResult:
        self.calls += 1
        self.requests.append({"messages": json.loads(json.dumps(messages)), "tools": tools})

        if self.fail:
            raise _ERRORS.get(self.fail, ProviderServerError)(f"mock {self.fail}")

        started = time.monotonic()
        entry: dict = {}
        if self.script:
            entry = self.script.pop(0)
        if "delay_s" in entry:
            self._sleep(float(entry["delay_s"]))
        if "error" in entry:
            raise _ERRORS.get(str(entry["error"]), ProviderServerError)(f"mock {entry['error']}")

        if "tool_calls" in entry:
            calls = [
                ToolCall(
                    id=str(tc.get("id") or f"call_{i}"),
                    name=str(tc["name"]),
                    arguments=dict(tc.get("arguments") or {}),
                )
                for i, tc in enumerate(entry["tool_calls"])
            ]
            text = str(entry.get("text") or "")
        else:
            calls = []
            text = str(entry.get("text") or self._default(messages))

        latency_ms = (time.monotonic() - started) * 1000.0
        return ProviderResult(
            text=text,
            tool_calls=calls,
            usage={"prompt_tokens": _approx_tokens(messages), "completion_tokens": _approx_tokens([{"content": text}])},
            latency_ms=latency_ms,
            model="mock-1",
        )

    def _default(self, messages: list[dict]) -> str:
        if self.default_text is not None:
            return self.default_text
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
        )
        return f"MOCK_REPLY: {last_user}"[:400]

    def last_messages(self) -> list[dict]:
        return self.requests[-1]["messages"] if self.requests else []


def _approx_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += max(1, len(content) // 4)
        for call in message.get("tool_calls") or []:
            total += 8
    return total
