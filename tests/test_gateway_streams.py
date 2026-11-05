"""Mocked server-sent events for both gateway wire formats."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from max_core.gateway import Gateway, GatewayError, HTTPAdapter, ModelSpec, Privacy
from tests.test_gateway import anthropic, cloud, local, request


def sse(data: list[dict[str, Any] | str], *, anthropic_events: bool = False) -> str:
    frames: list[str] = []
    for item in data:
        if isinstance(item, str):
            frames.append(f"data: {item}\n\n")
        else:
            frames.append(
                f"event: {item['type']}\ndata: {json.dumps(item, separators=(',', ':'))}\n\n"
                if anthropic_events
                else f"data: {json.dumps(item, separators=(',', ':'))}\n\n"
            )
    return "".join(frames)


def stream_result(
    models: list[ModelSpec], req: Any, payload: str, *, status: int = 200
) -> list[Any]:
    async def get() -> list[Any]:
        def handler(r: httpx.Request) -> httpx.Response:
            assert json.loads(r.content)["stream"] is True
            return httpx.Response(
                status, text=payload, headers={"content-type": "text/event-stream"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [event async for event in Gateway(models, HTTPAdapter(client)).stream(req)]

    return asyncio.run(get())


def openai_stream() -> str:
    return sse(
        [
            {
                "model": "demo-chat",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hel"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "demo-chat",
                "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}],
            },
            {
                "model": "demo-chat",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call1",
                                    "function": {"name": "look_up", "arguments": '{"id":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "demo-chat",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"n1"}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            {
                "model": "demo-chat",
                "choices": [],
                "usage": {"prompt_tokens": 23, "completion_tokens": 8},
            },
            "[DONE]",
        ]
    )


def anthropic_stream() -> str:
    return sse(
        [
            {
                "type": "message_start",
                "message": {
                    "model": "demo-claude",
                    "usage": {"input_tokens": 12, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Answer"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tool1", "name": "look_up"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"id":"n1"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 10},
            },
            {"type": "message_stop"},
        ],
        anthropic_events=True,
    )


def test_openai_stream_order_and_inert_tool_json() -> None:
    events = stream_result([local()], request(), openai_stream())
    assert [e.kind for e in events] == [
        "start",
        "content_delta",
        "content_delta",
        "tool_proposal",
        "usage",
        "complete",
    ]
    assert [e.sequence for e in events] == list(range(len(events)))
    assert "".join(e.text for e in events) == "Hello"
    assert events[3].tool_proposal.arguments == {"id": "n1"}
    assert events[4].usage.input_tokens == 23
    assert events[-1].finish_reason == "tool_calls"


def test_anthropic_stream_text_tool_and_usage() -> None:
    req = request(cloud_allowed=True, max_cost_usd=Decimal("1"))
    events = stream_result([anthropic()], req, anthropic_stream())
    assert [e.kind for e in events] == [
        "start",
        "content_delta",
        "tool_proposal",
        "usage",
        "complete",
    ]
    assert events[1].text == "Answer"
    assert events[2].tool_proposal.arguments == {"id": "n1"}
    assert events[3].usage.input_tokens == 12
    assert events[3].usage.output_tokens == 10
    assert events[-1].finish_reason == "tool_use"


def test_interrupted_stream_never_falls_back() -> None:
    calls: list[str] = []

    async def consume() -> None:
        def handler(r: httpx.Request) -> httpx.Response:
            calls.append(str(r.url))
            return httpx.Response(200, text=sse([{"choices": [{"delta": {"content": "partial"}}]}]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = Gateway(
                [local(provider_id="first"), local(provider_id="second")], HTTPAdapter(client)
            )
            async for _ in gateway.stream(request(allow_fallback=True)):
                pass

    with pytest.raises(GatewayError, match="stream_interrupted"):
        asyncio.run(consume())
    assert len(calls) == 1


def test_stream_no_fallback_even_on_429() -> None:
    calls: list[str] = []

    async def consume() -> None:
        def handler(r: httpx.Request) -> httpx.Response:
            calls.append(str(r.url))
            return httpx.Response(429)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = Gateway(
                [cloud(provider_id="first", priority=0), cloud(provider_id="second", priority=10)],
                HTTPAdapter(client),
            )
            async for _ in gateway.stream(
                request(cloud_allowed=True, max_cost_usd=Decimal("1"), allow_fallback=True)
            ):
                pass

    with pytest.raises(GatewayError, match="rate_limited"):
        asyncio.run(consume())
    assert len(calls) == 1


def test_malformed_partial_tool_is_never_emitted() -> None:
    payload = sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call1",
                                    "function": {
                                        "name": "test",
                                        "arguments": "{not json",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            "[DONE]",
        ]
    )
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        stream_result([local()], request(), payload)


def test_stream_size_limit() -> None:
    payload = sse([{"choices": [{"delta": {"content": "s" * 1_000_000}}]}, "[DONE]"])
    with pytest.raises(GatewayError, match="stream_too_large"):
        stream_result([local()], request(), payload)


def test_stream_actual_usage_can_exceed_admission_budget() -> None:
    payload = sse(
        [
            {"model": "demo-chat", "choices": [{"delta": {"content": "text"}}]},
            {
                "model": "demo-chat",
                "choices": [],
                "usage": {"prompt_tokens": 2_000_000, "completion_tokens": 0},
            },
            "[DONE]",
        ]
    )
    with pytest.raises(GatewayError, match="budget_exceeded"):
        stream_result(
            [cloud()], request(cloud_allowed=True, max_cost_usd=Decimal("0.001")), payload
        )


def test_restricted_stream_never_contacts_cloud() -> None:
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        stream_result(
            [cloud()],
            request(privacy=Privacy.RESTRICTED, cloud_allowed=True, max_cost_usd=Decimal("1")),
            openai_stream(),
        )


def test_missing_anthropic_terminal_event_is_interrupted() -> None:
    body = sse(
        [
            {"type": "message_start", "message": {"model": "demo-claude"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "x"},
            },
            {"type": "content_block_stop", "index": 0},
        ],
        anthropic_events=True,
    )
    with pytest.raises(GatewayError, match="stream_interrupted"):
        stream_result([anthropic()], request(cloud_allowed=True, max_cost_usd=Decimal("1")), body)
