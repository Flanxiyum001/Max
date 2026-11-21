"""Mocked server-sent events for both gateway wire formats."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from max_core.gateway import Dialect, Gateway, GatewayError, HTTPAdapter, ModelSpec, Privacy
from max_core.gateway.contracts import ToolDefinition
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


@pytest.mark.parametrize(
    "configured,returned,dialect",
    [
        ("gpt-4o", "gpt-4o-2024-08-06", Dialect.OPENAI_CHAT),
        ("gpt-4o-mini", "gpt-4o-mini-2024-07-18", Dialect.OPENAI_CHAT),
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929", Dialect.ANTHROPIC_MESSAGES),
        ("claude-3-5-sonnet-latest", "claude-3-5-sonnet-20241022", Dialect.ANTHROPIC_MESSAGES),
    ],
)
def test_stream_accepts_matching_snapshot_model_ids(
    configured: str, returned: str, dialect: Dialect
) -> None:
    if dialect == Dialect.OPENAI_CHAT:
        spec = cloud(model_id=configured)
        body = sse(
            [
                {"model": returned, "choices": [{"index": 0, "delta": {"content": "Hi"}}]},
                "[DONE]",
            ]
        )
    else:
        spec = anthropic(model_id=configured)
        body = sse(
            [
                {"type": "message_start", "message": {"model": returned}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": "Hi"},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ],
            anthropic_events=True,
        )
    events = stream_result([spec], request(cloud_allowed=True, max_cost_usd=Decimal("1")), body)
    assert "".join(e.text for e in events) == "Hi"


@pytest.mark.parametrize(
    "configured,returned,dialect",
    [
        ("gpt-4o", "gpt-4o-mini-2024-07-18", Dialect.OPENAI_CHAT),
        ("gpt-4o-2024-08-06", "gpt-4o-2024-11-20", Dialect.OPENAI_CHAT),
        ("claude-sonnet-4-5", "claude-3-5-haiku-20241022", Dialect.ANTHROPIC_MESSAGES),
    ],
)
def test_stream_rejects_mismatched_snapshot_model_ids(
    configured: str, returned: str, dialect: Dialect
) -> None:
    if dialect == Dialect.OPENAI_CHAT:
        spec = cloud(model_id=configured)
        body = sse(
            [
                {"model": returned, "choices": [{"index": 0, "delta": {"content": "Hi"}}]},
                "[DONE]",
            ]
        )
    else:
        spec = anthropic(model_id=configured)
        body = sse(
            [
                {"type": "message_start", "message": {"model": returned}},
                {"type": "message_stop"},
            ],
            anthropic_events=True,
        )
    with pytest.raises(GatewayError, match="model_mismatch"):
        stream_result([spec], request(cloud_allowed=True, max_cost_usd=Decimal("1")), body)


def test_stream_serializes_tools_when_supported() -> None:
    tool = ToolDefinition("read_note", "Read note")
    seen: list[dict[str, Any]] = []

    async def run() -> list[Any]:
        def handler(r: httpx.Request) -> httpx.Response:
            seen.append(json.loads(r.content))
            return httpx.Response(
                200,
                text=sse(
                    [{"model": "demo-chat", "choices": [{"delta": {"content": "ok"}}]}, "[DONE]"]
                ),
                headers={"content-type": "text/event-stream"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            req = request(
                cloud_allowed=True,
                max_cost_usd=Decimal("1"),
                max_input_tokens=400,
                tools=(tool,),
            )
            spec = cloud(capabilities=frozenset({"text", "tools"}))
            return [e async for e in Gateway([spec], HTTPAdapter(client)).stream(req)]

    events = asyncio.run(run())
    assert "".join(e.text for e in events) == "ok"
    assert seen[0]["tools"][0]["function"]["name"] == "read_note"


@pytest.mark.parametrize(
    "configured,returned,dialect",
    [
        ("gpt-4o", "gpt-4o-2024-99-99", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-0231", Dialect.OPENAI_CHAT),
        ("claude-3-5-sonnet-latest", "claude-3-5-sonnet-2024-02-31", Dialect.ANTHROPIC_MESSAGES),
    ],
)
def test_stream_rejects_invalid_calendar_date_snapshots(
    configured: str, returned: str, dialect: Dialect
) -> None:
    if dialect == Dialect.OPENAI_CHAT:
        spec = cloud(model_id=configured)
        body = sse(
            [
                {"model": returned, "choices": [{"index": 0, "delta": {"content": "Hi"}}]},
                "[DONE]",
            ]
        )
    else:
        spec = anthropic(model_id=configured)
        body = sse(
            [
                {"type": "message_start", "message": {"model": returned}},
                {"type": "message_stop"},
            ],
            anthropic_events=True,
        )
    with pytest.raises(GatewayError, match="model_mismatch"):
        stream_result([spec], request(cloud_allowed=True, max_cost_usd=Decimal("1")), body)


def test_stream_rejects_nan_and_newline_call_id_in_tool_proposals() -> None:
    openai_nan = sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "look_up", "arguments": '{"x": Infinity}'},
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
        stream_result([local()], request(), openai_nan)

    anthropic_nan = sse(
        [
            {"type": "message_start", "message": {"model": "demo-claude"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tu_1", "name": "look_up"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"x": NaN}'},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ],
        anthropic_events=True,
    )
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        stream_result(
            [anthropic()], request(cloud_allowed=True, max_cost_usd=Decimal("1")), anthropic_nan
        )

    openai_bad_id = sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1\n",
                                    "function": {"name": "look_up", "arguments": "{}"},
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
        stream_result([local()], request(), openai_bad_id)


def test_stream_deep_nested_json_and_tool_args_fail_cleanly_without_recursion_error() -> None:
    deep_frame = "[" * 5000 + "]" * 5000
    openai_deep_frame = sse([deep_frame, "[DONE]"])
    with pytest.raises(GatewayError, match="malformed_provider_response"):
        stream_result([local()], request(), openai_deep_frame)

    anthropic_deep_frame = f"event: message_start\ndata: {deep_frame}\n\n"
    with pytest.raises(GatewayError, match="malformed_provider_response"):
        stream_result(
            [anthropic()],
            request(cloud_allowed=True, max_cost_usd=Decimal("1")),
            anthropic_deep_frame,
        )

    openai_deep_tool = sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "look_up", "arguments": "[" * 5000},
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
        stream_result([local()], request(), openai_deep_tool)

    anthropic_deep_tool = sse(
        [
            {"type": "message_start", "message": {"model": "demo-claude"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tu_1", "name": "look_up"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "[" * 5000},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ],
        anthropic_events=True,
    )
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        stream_result(
            [anthropic()],
            request(cloud_allowed=True, max_cost_usd=Decimal("1")),
            anthropic_deep_tool,
        )


def test_stream_rejects_lone_surrogates_and_preserves_split_surrogate_pair_escapes() -> None:
    from max_core.gateway.streaming import anthropic_events, openai_events, sse_frames

    async def bad_lines() -> Any:
        yield "data: hi \ud800\n"
        yield ""

    async def run_sse() -> None:
        with pytest.raises(GatewayError, match="malformed_provider_response"):
            async for _ in sse_frames(bad_lines()):
                pass

    asyncio.run(run_sse())

    # OpenAI lone surrogate in text delta, tool call_id, tool arguments, and finish_reason
    async def run_openai_frames(raw_frames: list[tuple[str, str]]) -> list[Any]:
        async def gen() -> Any:
            for item in raw_frames:
                yield item

        return [e async for e in openai_events(gen(), request(), local())]

    with pytest.raises(GatewayError, match="malformed_provider_response"):
        asyncio.run(
            run_openai_frames(
                [
                    ("message", '{"choices":[{"delta":{"content":"hi \\ud800"}}]}'),
                    ("message", "[DONE]"),
                ]
            )
        )
    with pytest.raises(GatewayError, match="malformed_provider_response"):
        asyncio.run(
            run_openai_frames(
                [
                    ("message", '{"choices":[{"delta":{},"finish_reason":"stop_\\ud800"}]}'),
                    ("message", "[DONE]"),
                ]
            )
        )
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        asyncio.run(
            run_openai_frames(
                [
                    (
                        "message",
                        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c_\\ud800","function":{"name":"look_up","arguments":"{}"}}]}}]}',
                    ),
                    ("message", "[DONE]"),
                ]
            )
        )
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        asyncio.run(
            run_openai_frames(
                [
                    (
                        "message",
                        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"look_up","arguments":"{\\"x\\":\\"\\ud800\\"}"}}]}}]}',
                    ),
                    ("message", "[DONE]"),
                ]
            )
        )

    # Anthropic lone surrogate in tool_use id/name, partial_json, and stop_reason
    async def run_anthropic_frames(raw_frames: list[tuple[str, str]]) -> list[Any]:
        async def gen() -> Any:
            for item in raw_frames:
                yield item

        req = request(cloud_allowed=True, max_cost_usd=Decimal("1"))
        return [e async for e in anthropic_events(gen(), req, anthropic())]

    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        asyncio.run(
            run_anthropic_frames(
                [
                    ("message_start", '{"type":"message_start","message":{"model":"demo-claude"}}'),
                    (
                        "content_block_start",
                        '{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu_\\ud800","name":"look_up"}}',
                    ),
                ]
            )
        )
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        asyncio.run(
            run_anthropic_frames(
                [
                    ("message_start", '{"type":"message_start","message":{"model":"demo-claude"}}'),
                    (
                        "content_block_start",
                        '{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu_1","name":"look_up"}}',
                    ),
                    (
                        "content_block_delta",
                        '{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\ud800"}}',
                    ),
                ]
            )
        )
    with pytest.raises(GatewayError, match="malformed_provider_response"):
        asyncio.run(
            run_anthropic_frames(
                [
                    ("message_start", '{"type":"message_start","message":{"model":"demo-claude"}}'),
                    (
                        "message_delta",
                        '{"type":"message_delta","delta":{"stop_reason":"end_\\ud800"}}',
                    ),
                    ("message_stop", '{"type":"message_stop"}'),
                ]
            )
        )

    # Split valid surrogate pair JSON escape across two chunks still produces valid emoji
    split_pair_stream = sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "look_up", "arguments": '{"x":"\\ud83d'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '\\ude80"}'}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        ]
    )
    events = stream_result([local()], request(), split_pair_stream)
    proposals = [e for e in events if e.kind == "tool_proposal"]
    assert len(proposals) == 1
    assert proposals[0].tool_proposal.arguments == {"x": "🚀"}
