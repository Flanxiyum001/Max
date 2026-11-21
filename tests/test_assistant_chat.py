"""Mocked integration and security tests for AssistantChatService."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from max_core.assistant_chat import (
    AssistantChatService,
    ChatTurnResult,
    ToolExecutionSummary,
)
from max_core.gateway.contracts import ToolDefinition, ToolProposal
from max_core.local_config import DesktopConfig, ProviderConfig, load_desktop_config
from max_core.persona import ReplyMode

FAKE_OPENAI_KEY = "sk-synthetic-openai-0123456789abcdef"
FAKE_ANTHROPIC_KEY = "ant-synthetic-key-0123456789abcdef"


def _make_config(
    tmp_path: Path,
    *,
    provider: str | None = "openai",
    api_key: str | None = None,
    model_id: str | None = None,
    endpoint: str | None = None,
    input_rate: float | None = None,
    output_rate: float | None = None,
    max_cost_usd: float = 0.05,
) -> DesktopConfig:
    env: dict[str, str] = {
        "MAX_LOCAL_DB_PATH": str(tmp_path / "max.sqlite3"),
        "MAX_COST_USD": str(max_cost_usd),
    }
    if provider == "openai":
        env["OPENAI_API_KEY"] = api_key if api_key is not None else FAKE_OPENAI_KEY
        env["MAX_LLM_PROVIDER"] = "openai"
        if model_id is not None:
            env["MAX_OPENAI_MODEL"] = model_id
        if endpoint is not None:
            env["MAX_OPENAI_ENDPOINT"] = endpoint
        if input_rate is not None:
            env["MAX_OPENAI_INPUT_USD_PER_MILLION"] = str(input_rate)
        if output_rate is not None:
            env["MAX_OPENAI_OUTPUT_USD_PER_MILLION"] = str(output_rate)
    elif provider == "anthropic":
        env["ANTHROPIC_API_KEY"] = api_key if api_key is not None else FAKE_ANTHROPIC_KEY
        env["MAX_LLM_PROVIDER"] = "anthropic"
        if model_id is not None:
            env["MAX_ANTHROPIC_MODEL"] = model_id
        if endpoint is not None:
            env["MAX_ANTHROPIC_ENDPOINT"] = endpoint
        if input_rate is not None:
            env["MAX_ANTHROPIC_INPUT_USD_PER_MILLION"] = str(input_rate)
        if output_rate is not None:
            env["MAX_ANTHROPIC_OUTPUT_USD_PER_MILLION"] = str(output_rate)
    return load_desktop_config(env=env, data_dir=tmp_path)


def _openai_ok(
    content: str = "Hello from MAX.",
    *,
    model: str = "gpt-4o-mini-2024-07-18",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 25, "total_tokens": 145},
    }


def _anthropic_ok(
    blocks: list[dict[str, Any]] | None = None,
    *,
    model: str = "claude-sonnet-4-5-20250929",
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks if blocks is not None else [{"type": "text", "text": "Anthropic ready."}],
        "usage": {"input_tokens": 150, "output_tokens": 30},
        "stop_reason": stop_reason,
    }


class _FakeToolExecutor:
    def __init__(
        self,
        tools: Sequence[ToolDefinition],
        outcomes: dict[str, ToolExecutionSummary | list[ToolExecutionSummary]],
    ) -> None:
        self._tools = tuple(tools)
        self._outcomes = {k: list(v) if isinstance(v, list) else [v] for k, v in outcomes.items()}
        self.calls: list[tuple[ToolProposal, str | None]] = []

    def tool_definitions(self) -> Sequence[ToolDefinition]:
        return self._tools

    def execute(
        self,
        proposal: ToolProposal,
        *,
        approval_id: str | None = None,
    ) -> ToolExecutionSummary:
        self.calls.append((proposal, approval_id))
        queue = self._outcomes.get(proposal.name)
        if not queue:
            return ToolExecutionSummary(
                call_id=proposal.call_id,
                tool_name=proposal.name,
                arguments=dict(proposal.arguments),
                status="failed",
                output="unexpected_tool_call",
            )
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return ToolExecutionSummary(
            call_id=proposal.call_id,
            tool_name=proposal.name,
            arguments=dict(proposal.arguments),
            status=item.status,
            output=item.output,
            approval_id=item.approval_id,
            task_id=item.task_id,
        )


def test_openai_chat_turn_persona_snapshot_and_cost(tmp_path: Path) -> None:
    seen_payloads: list[dict[str, Any]] = []
    seen_headers: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(req.headers))
        seen_payloads.append(json.loads(req.content))
        return httpx.Response(200, json=_openai_ok("Hi there!", model="gpt-4o-mini-2024-07-18"))

    cfg = _make_config(tmp_path, provider="openai")
    svc = AssistantChatService(cfg, transport=httpx.MockTransport(handler))
    status = svc.provider_status()
    assert status.configured is True
    assert status.provider == "openai"
    assert status.model_id == "gpt-4o-mini"

    result = svc.send_message("Hello MAX")
    assert result.ok is True
    assert result.reply == "Hi there!"
    assert result.provider == "openai"
    assert result.model_id == "gpt-4o-mini"
    assert result.error_code is None
    assert result.estimated_cost_usd is not None and result.estimated_cost_usd > 0
    assert svc.history_len() == 2

    assert seen_headers[0]["authorization"] == f"Bearer {FAKE_OPENAI_KEY}"
    messages = seen_payloads[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "You are MAX" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Hello MAX"}


def test_anthropic_default_five_cent_budget_and_snapshot_passes(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-api-key"] == FAKE_ANTHROPIC_KEY
        assert req.headers["anthropic-version"] == "2023-06-01"
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=_anthropic_ok(model="claude-sonnet-4-5-20250929"))

    cfg = _make_config(tmp_path, provider="anthropic")
    assert cfg.active_provider is not None
    assert cfg.active_provider.max_cost_usd == 0.05

    svc = AssistantChatService(cfg, transport=httpx.MockTransport(handler))
    result = svc.send_message("Summarize today's focus.")
    assert result.ok is True
    assert result.reply == "Anthropic ready."
    assert result.provider == "anthropic"
    assert result.model_id == "claude-sonnet-4-5"
    assert "You are MAX" in seen[0]["system"]
    assert seen[0]["messages"] == [{"role": "user", "content": "Summarize today's focus."}]


def test_openai_dotted_tool_mapping_and_round_trip(tmp_path: Path) -> None:
    seen_payloads: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "fixture_status",
                                "arguments": '{"task_id":"t-1"}',
                            },
                        }
                    ],
                ),
            )
        return httpx.Response(200, json=_openai_ok("Task t-1 is running."))

    tools = (
        ToolDefinition(
            name="fixture.status",
            description="Check task status.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
    )
    executor = _FakeToolExecutor(
        tools,
        {
            "fixture.status": ToolExecutionSummary(
                call_id="call_1",
                tool_name="fixture.status",
                arguments={"task_id": "t-1"},
                status="completed",
                output={"state": "running"},
                task_id="t-1",
            )
        },
    )
    svc = AssistantChatService(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    result = svc.send_message("What is the status of task t-1?", tool_executor=executor)

    assert result.ok is True
    assert result.reply == "Task t-1 is running."
    assert len(result.tool_executions) == 1
    assert result.tool_executions[0].tool_name == "fixture.status"
    assert result.tool_executions[0].status == "completed"
    assert executor.calls[0][0].name == "fixture.status"

    # Wire name on OpenAI request must be dot-free and match ^[A-Za-z0-9_-]{1,64}$
    assert seen_payloads[0]["tools"][0]["function"]["name"] == "fixture_status"
    assert seen_payloads[1]["messages"][-1]["role"] == "tool"
    assert seen_payloads[1]["messages"][-1]["tool_call_id"] == "call_1"


def test_anthropic_parallel_tool_calls_coalesce_and_is_error_flag(tmp_path: Path) -> None:
    seen_payloads: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            return httpx.Response(
                200,
                json=_anthropic_ok(
                    [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "dev_health",
                            "input": {},
                        },
                        {
                            "type": "tool_use",
                            "id": "tu_2",
                            "name": "fixture_read_file",
                            "input": {"path": "missing.txt"},
                        },
                    ],
                    stop_reason="tool_use",
                ),
            )
        return httpx.Response(
            200,
            json=_anthropic_ok([{"type": "text", "text": "Health is OK; missing.txt was denied."}]),
        )

    tools = (
        ToolDefinition("dev.health", "Health check"),
        ToolDefinition("fixture.read_file", "Read file"),
    )
    executor = _FakeToolExecutor(
        tools,
        {
            "dev.health": ToolExecutionSummary("tu_1", "dev.health", {}, "completed", {"ok": True}),
            "fixture.read_file": ToolExecutionSummary(
                "tu_2",
                "fixture.read_file",
                {"path": "missing.txt"},
                "denied",
                "path_outside_root",
            ),
        },
    )
    svc = AssistantChatService(
        _make_config(tmp_path, provider="anthropic"),
        transport=httpx.MockTransport(handler),
    )
    result = svc.send_message("Run health and read missing.txt", tool_executor=executor)
    assert result.ok is True
    assert len(result.tool_executions) == 2

    # Second request must coalesce both tool_result blocks into one user message
    second_msgs = seen_payloads[1]["messages"]
    assert second_msgs[-2]["role"] == "assistant"
    assert second_msgs[-1]["role"] == "user"
    assert second_msgs[-1]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": '{"status":"completed","output":{"ok":true}}',
        },
        {
            "type": "tool_result",
            "tool_use_id": "tu_2",
            "content": '{"status":"denied","output":"path_outside_root"}',
            "is_error": True,
        },
    ]


def test_bijective_tool_name_collision_handled(tmp_path: Path) -> None:
    seen_tools: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        seen_tools.extend(t["function"]["name"] for t in payload["tools"])
        return (
            httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": seen_tools[0], "arguments": "{}"},
                        },
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {"name": seen_tools[1], "arguments": "{}"},
                        },
                    ],
                ),
            )
            if len(seen_tools) == 2
            else httpx.Response(200, json=_openai_ok("Both ran."))
        )

    tools = (
        ToolDefinition("fixture.status", "Dotted status"),
        ToolDefinition("fixture_status", "Underscore status"),
    )
    executor = _FakeToolExecutor(
        tools,
        {
            "fixture.status": ToolExecutionSummary("c1", "fixture.status", {}, "completed", "dotted_ok"),
            "fixture_status": ToolExecutionSummary("c2", "fixture_status", {}, "completed", "under_ok"),
        },
    )
    svc = AssistantChatService(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    result = svc.send_message("Check both", tool_executor=executor)
    assert result.ok is True
    assert len(set(seen_tools[:2])) == 2
    assert [e.tool_name for e in result.tool_executions] == ["fixture.status", "fixture_status"]


def test_tool_executor_none_never_executes_tools(tmp_path: Path) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_openai_ok(
                "",
                tool_calls=[
                    {
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "fixture_status", "arguments": "{}"},
                    }
                ],
            ),
        )

    tools = (ToolDefinition("fixture.status", "Check status"),)
    svc = AssistantChatService(
        _make_config(tmp_path),
        transport=httpx.MockTransport(handler),
        tools=tools,
    )
    result = svc.send_message("Check status", tool_executor=None)
    assert result.ok is True
    assert len(result.tool_executions) == 1
    assert result.tool_executions[0].tool_name == "fixture.status"
    assert result.tool_executions[0].status == "denied"
    assert result.tool_executions[0].output == "tool_execution_unavailable"


def test_awaiting_approval_detaches_and_resumes_cleanly(tmp_path: Path) -> None:
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        payload = json.loads(req.content)
        if call_count == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "call_create",
                            "type": "function",
                            "function": {
                                "name": "task_create",
                                "arguments": '{"objective":"Back up notes"}',
                            },
                        }
                    ],
                ),
            )
        # Ensure resuming sends proper assistant tool_calls + tool message pair
        assert payload["messages"][-2]["role"] == "assistant"
        assert payload["messages"][-1]["role"] == "tool"
        assert payload["messages"][-1]["tool_call_id"] == "call_create"
        return httpx.Response(200, json=_openai_ok("Created task 'Back up notes'."))

    tools = (ToolDefinition("task.create", "Create a task"),)
    executor = _FakeToolExecutor(
        tools,
        {
            "task.create": [
                ToolExecutionSummary(
                    call_id="call_create",
                    tool_name="task.create",
                    arguments={"objective": "Back up notes"},
                    status="awaiting_approval",
                    output="owner_approval_required",
                    approval_id="apr-123",
                    task_id="task-9",
                ),
                ToolExecutionSummary(
                    call_id="call_create",
                    tool_name="task.create",
                    arguments={"objective": "Back up notes"},
                    status="completed",
                    output={"task_id": "task-9", "state": "queued"},
                    approval_id="apr-123",
                    task_id="task-9",
                ),
            ]
        },
    )
    svc = AssistantChatService(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    first = svc.send_message("Create a backup task", tool_executor=executor)
    assert first.ok is True
    assert first.tool_executions[0].status == "awaiting_approval"
    assert svc.pending_approval_ids() == ("apr-123",)
    # Plain-text placeholder only — no dangling tool_calls in _history!
    assert svc.history_len() == 2

    resumed = svc.resume_approval("apr-123", tool_executor=executor)
    assert resumed.ok is True
    assert resumed.reply == "Created task 'Back up notes'."
    assert resumed.tool_executions[-1].status == "completed"
    assert svc.pending_approval_ids() == ()
    assert executor.calls[-1][1] == "apr-123"
    # Placeholder replaced with resolved 4-message turn (user, assistant tool_calls, tool, assistant)
    assert svc.history_len() == 4

    # Unknown or already-consumed approval returns approval_not_found
    missing = svc.resume_approval("apr-123", tool_executor=executor)
    assert missing.ok is False
    assert missing.error_code == "approval_not_found"


def test_resume_approval_denied_does_not_execute_remaining_or_call_llm(tmp_path: Path) -> None:
    call_count = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "task_create", "arguments": "{}"},
                        }
                    ],
                ),
            )
        pytest.fail("LLM should not be called again after owner denial")

    tools = (ToolDefinition("task.create", "Create task"),)
    executor = _FakeToolExecutor(
        tools,
        {
            "task.create": [
                ToolExecutionSummary(
                    "c1", "task.create", {}, "awaiting_approval", "pending", approval_id="apr-deny"
                ),
                ToolExecutionSummary(
                    "c1", "task.create", {}, "denied", "owner_denied", approval_id="apr-deny"
                ),
            ]
        },
    )
    svc = AssistantChatService(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    svc.send_message("Create task", tool_executor=executor)
    resumed = svc.resume_approval("apr-deny", tool_executor=executor)
    assert resumed.ok is True
    assert "denied" in resumed.reply.lower()
    assert resumed.tool_executions[-1].status == "denied"
    assert call_count == 1


def test_m16_egress_blocking_across_prompt_tool_args_outputs_and_defs(tmp_path: Path) -> None:
    secret_prompt = "Please use ghp_" + "X" * 30
    secret_dict = {"API_KEY": "secret_" + "Y" * 24}

    def fail_transport(_req: httpx.Request) -> httpx.Response:
        pytest.fail("Secret should be blocked before HTTP transport")

    svc = AssistantChatService(
        _make_config(tmp_path), transport=httpx.MockTransport(fail_transport)
    )
    blocked_prompt = svc.send_message(secret_prompt)
    assert blocked_prompt.ok is False
    assert blocked_prompt.error_code == "egress_blocked"
    assert svc.history_len() == 0

    # Secret inside tool definition metadata
    bad_tools = (ToolDefinition("fixture.list", f"Uses sk-proj-{'Z' * 30}"),)
    svc_bad_def = AssistantChatService(
        _make_config(tmp_path),
        transport=httpx.MockTransport(fail_transport),
        tools=bad_tools,
    )
    blocked_def = svc_bad_def.send_message("List my notes")
    assert blocked_def.ok is False
    assert blocked_def.error_code == "egress_blocked"

    # Secret returned by local tool output (e.g., reading a .env file)
    calls = 0

    def tool_output_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "fixture_read_file",
                                "arguments": '{"p":".env"}',
                            },
                        }
                    ],
                ),
            )
        pytest.fail("Tool output secret must never reach cloud")

    tools = (ToolDefinition("fixture.read_file", "Read file"),)
    executor = _FakeToolExecutor(
        tools,
        {
            "fixture.read_file": ToolExecutionSummary(
                "c1", "fixture.read_file", {"p": ".env"}, "completed", secret_dict
            )
        },
    )
    svc_tool = AssistantChatService(
        _make_config(tmp_path), transport=httpx.MockTransport(tool_output_handler)
    )
    blocked_out = svc_tool.send_message("Read .env", tool_executor=executor)
    assert blocked_out.ok is False
    assert blocked_out.error_code == "egress_blocked"
    assert calls == 1
    assert svc_tool.history_len() == 0

    # Secret returned inside model's tool_calls arguments before next iteration
    arg_calls = 0

    def tool_arg_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal arg_calls
        arg_calls += 1
        if arg_calls == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "fixture_list",
                                "arguments": '{"token":"Bearer ' + "A" * 24 + '"}',
                            },
                        }
                    ],
                ),
            )
        pytest.fail("Tool call arguments containing a secret must not re-egress")

    tools_arg = (ToolDefinition("fixture.list", "List notes"),)
    executor_arg = _FakeToolExecutor(
        tools_arg,
        {"fixture.list": ToolExecutionSummary("c2", "fixture.list", {}, "completed", "ok")},
    )
    svc_arg = AssistantChatService(
        _make_config(tmp_path), transport=httpx.MockTransport(tool_arg_handler)
    )
    blocked_args = svc_arg.send_message("List notes", tool_executor=executor_arg)
    assert blocked_args.ok is False
    assert blocked_args.error_code == "egress_blocked"
    assert arg_calls == 1


@pytest.mark.parametrize(
    "status_code,expected_code",
    [
        (401, "auth_failed"),
        (403, "auth_failed"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
    ],
)
def test_http_errors_sanitized_without_key_or_body_leak(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, status_code: int, expected_code: str
) -> None:
    raw_upstream_secret = f"UPSTREAM_INTERNAL_BODY_{FAKE_OPENAI_KEY}_TRACE"

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=raw_upstream_secret)

    cfg = _make_config(tmp_path, provider="openai")
    svc = AssistantChatService(cfg, transport=httpx.MockTransport(handler))

    with caplog.at_level(logging.DEBUG):
        res: ChatTurnResult = svc.send_message("Hello")

    assert res.ok is False
    assert res.error_code == expected_code
    assert FAKE_OPENAI_KEY not in res.reply
    assert raw_upstream_secret not in res.reply
    assert FAKE_OPENAI_KEY not in repr(svc)
    assert FAKE_OPENAI_KEY not in caplog.text
    assert raw_upstream_secret not in caplog.text


def test_missing_key_network_errors_budget_and_invalid_inputs(tmp_path: Path) -> None:
    # Missing API key
    empty_cfg = _make_config(tmp_path, provider=None)
    svc_missing = AssistantChatService(empty_cfg)
    assert svc_missing.provider_status().configured is False
    missing_res = svc_missing.send_message("Hello")
    assert missing_res.ok is False
    assert missing_res.error_code == "missing_api_key"

    # Invalid input
    cfg = _make_config(tmp_path)
    svc = AssistantChatService(cfg, transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    assert svc.send_message("   ").error_code == "invalid_input"
    assert svc.send_message("x" * 32_001).error_code == "invalid_input"

    # Invalid provider config (http:// remote endpoint)
    bad_cfg = DesktopConfig(
        database_url=cfg.database_url,
        is_sqlite_local=True,
        workspace_root=cfg.workspace_root,
        notes_dir=cfg.notes_dir,
        active_provider=ProviderConfig(
            provider="openai",
            model_id="gpt-4o-mini",
            endpoint="http://api.openai.com/v1/chat/completions",
            api_key=FAKE_OPENAI_KEY,
            input_usd_per_million=0.15,
            output_usd_per_million=0.60,
            max_cost_usd=0.05,
        ),
        openai=None,
        anthropic=None,
    )
    bad_svc = AssistantChatService(bad_cfg)
    assert bad_svc.send_message("Hello").error_code == "invalid_provider_config"

    # ReadTimeout -> network_error (with no secret or URL leaked)
    def timeout_handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            f"Timed out talking to {req.url} with {FAKE_OPENAI_KEY}", request=req
        )

    timeout_svc = AssistantChatService(cfg, transport=httpx.MockTransport(timeout_handler))
    t_res = timeout_svc.send_message("Hello")
    assert t_res.ok is False
    assert t_res.error_code == "network_error"
    assert FAKE_OPENAI_KEY not in t_res.reply
    assert "api.openai.com" not in t_res.reply

    # ConnectError -> provider_unavailable
    def connect_handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=req)

    conn_svc = AssistantChatService(cfg, transport=httpx.MockTransport(connect_handler))
    assert conn_svc.send_message("Hello").error_code == "provider_unavailable"

    # Budget exceeded (tiny budget on Anthropic)
    tiny_cfg = _make_config(tmp_path, provider="anthropic", max_cost_usd=0.001)
    budget_svc = AssistantChatService(
        tiny_cfg,
        transport=httpx.MockTransport(lambda _: pytest.fail("Should fail preflight budget")),
    )
    assert budget_svc.send_message("Hello").error_code == "budget_exceeded"

    # Model mismatch
    mismatch_svc = AssistantChatService(
        cfg,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=_openai_ok(model="gpt-4o"))
        ),
    )
    assert mismatch_svc.send_message("Hello").error_code == "model_mismatch"


def test_history_trimming_preserves_atomic_tool_pairs_and_bounds(tmp_path: Path) -> None:
    seen_histories: list[list[dict[str, Any]]] = []
    turn = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        payload = json.loads(req.content)
        seen_histories.append(payload["messages"])
        if turn == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c_t1",
                            "type": "function",
                            "function": {"name": "dev_health", "arguments": "{}"},
                        }
                    ],
                ),
            )
        return httpx.Response(200, json=_openai_ok(f"Reply {turn}"))

    tools = (ToolDefinition("dev.health", "Health"),)
    executor = _FakeToolExecutor(
        tools,
        {"dev.health": ToolExecutionSummary("c_t1", "dev.health", {}, "completed", "ok")},
    )
    svc = AssistantChatService(
        _make_config(tmp_path),
        max_history_messages=6,
        transport=httpx.MockTransport(handler),
    )
    # Turn 1 produces a 4-message atomic group: [user, assistant(tool_calls), tool, assistant]
    assert svc.send_message("Turn 1", tool_executor=executor).ok is True
    assert svc.history_len() == 4
    # Turn 2 adds 2 messages -> total 6
    assert svc.send_message("Turn 2", tool_executor=executor).ok is True
    assert svc.history_len() == 6
    # Turn 3 adds 2 messages -> would exceed 6, so the entire 4-message Turn 1 group is dropped atomically!
    assert svc.send_message("Turn 3", tool_executor=executor).ok is True
    assert svc.history_len() == 4
    # Last outbound call should have [system, user(Turn 2), assistant(Turn 2), user(Turn 3)] — no orphaned tool msg!
    roles = [m["role"] for m in seen_histories[-1]]
    assert roles == ["system", "user", "assistant", "user"]


def test_max_tool_iterations_bound_stops_cleanly(tmp_path: Path) -> None:
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=_openai_ok(
                "",
                tool_calls=[
                    {
                        "id": f"c_{calls}",
                        "type": "function",
                        "function": {"name": "dev_health", "arguments": "{}"},
                    }
                ],
            ),
        )

    tools = (ToolDefinition("dev.health", "Health"),)
    executor = _FakeToolExecutor(
        tools,
        {"dev.health": ToolExecutionSummary("c", "dev.health", {}, "completed", "ok")},
    )
    svc = AssistantChatService(
        _make_config(tmp_path),
        max_tool_iterations=2,
        transport=httpx.MockTransport(handler),
    )
    res = svc.send_message("Loop forever", tool_executor=executor)
    assert res.ok is True
    assert calls == 2
    assert len(res.tool_executions) == 2


def test_threadpool_concurrent_sync_calls_and_async_injected_client(tmp_path: Path) -> None:
    lock = threading.Lock()
    counter = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal counter
        payload = json.loads(req.content)
        with lock:
            counter += 1
            idx = counter
        user_content = payload["messages"][-1]["content"]
        return httpx.Response(200, json=_openai_ok(f"ack:{idx}:{user_content}"))

    svc = AssistantChatService(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    svc.set_reply_mode(ReplyMode.WORK)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(svc.send_message, f"msg-{i}") for i in range(6)]
        results = [f.result(timeout=10) for f in futures]

    assert all(r.ok for r in results)
    assert counter == 6
    svc.clear_history()
    assert svc.history_len() == 0

    async def check_injected() -> ChatTurnResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async_svc = AssistantChatService(_make_config(tmp_path), client=client)
            return await async_svc.send_message_async("async hello")

    async_res = asyncio.run(check_injected())
    assert async_res.ok is True
    assert "async hello" in async_res.reply


def test_m16_json_in_string_prefixed_and_deep_tool_outputs_blocked(tmp_path: Path) -> None:
    def fail_after_first(call_counter: list[int]) -> Any:
        def handler(_req: httpx.Request) -> httpx.Response:
            call_counter[0] += 1
            if call_counter[0] == 1:
                return httpx.Response(
                    200,
                    json=_openai_ok(
                        "",
                        tool_calls=[
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "fixture_read_file", "arguments": "{}"},
                            }
                        ],
                    ),
                )
            pytest.fail("Secret tool output must not be sent to cloud")

        return handler

    tools = (ToolDefinition("fixture.read_file", "Read file"),)
    deep_secret: dict[str, Any] = {"API_KEY": "deep_secret_token_12345"}
    for idx in range(38):
        deep_secret = {f"n{idx}": [deep_secret]}

    for secret_output in (
        '{"API_KEY":"super_secret_token_12345","DATABASE_URL":"postgresql://u:p@h/db"}',
        'File contents: {"API_KEY":"super_secret_token_12345"}',
        deep_secret,
    ):
        calls = [0]
        executor = _FakeToolExecutor(
            tools,
            {
                "fixture.read_file": ToolExecutionSummary(
                    "c1", "fixture.read_file", {}, "completed", secret_output
                )
            },
        )
        svc = AssistantChatService(
            _make_config(tmp_path), transport=httpx.MockTransport(fail_after_first(calls))
        )
        res = svc.send_message("Read config", tool_executor=executor)
        assert res.ok is False
        assert res.error_code == "egress_blocked"
        assert calls[0] == 1

    # Compact JSON secret directly in user prompt
    svc_prompt = AssistantChatService(
        _make_config(tmp_path),
        transport=httpx.MockTransport(lambda _: pytest.fail("Should block user JSON secret")),
    )
    prompt_res = svc_prompt.send_message('Parse: {"API_KEY":"super_secret_token_12345"}')
    assert prompt_res.ok is False
    assert prompt_res.error_code == "egress_blocked"


def test_long_provider_replies_bounded_across_all_paths(tmp_path: Path) -> None:
    long_text = "A" * 40_000

    # 1. Plain reply > 32,000 chars
    turn = 0

    def plain_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        return httpx.Response(200, json=_openai_ok(long_text if turn == 1 else "ok"))

    svc = AssistantChatService(
        _make_config(tmp_path, max_cost_usd=1.0),
        transport=httpx.MockTransport(plain_handler),
    )
    res1 = svc.send_message("Write a long essay")
    assert res1.ok is True
    assert len(res1.reply) == 40_000
    assert svc.history_len() == 2
    res2 = svc.send_message("Follow up")
    assert res2.ok is True and res2.reply == "ok"

    # 2. tool_executor=None with > 32,000 char reply + tool proposal
    svc_no_exec = AssistantChatService(
        _make_config(tmp_path, max_cost_usd=1.0),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=_openai_ok(
                    long_text,
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "dev_health", "arguments": "{}"},
                        }
                    ],
                ),
            )
        ),
        tools=(ToolDefinition("dev.health", "Health"),),
    )
    res_no_exec = svc_no_exec.send_message("Check", tool_executor=None)
    assert res_no_exec.ok is True
    assert len(res_no_exec.reply) == 40_000
    assert svc_no_exec.history_len() == 2

    # 3. tool_proposals path with > 32,000 char content alongside tool call
    step = 0

    def tool_handler(req: httpx.Request) -> httpx.Response:
        nonlocal step
        step += 1
        if step == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    long_text,
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "dev_health", "arguments": "{}"},
                        }
                    ],
                ),
            )
        payload = json.loads(req.content)
        assistant_hist = [m for m in payload["messages"] if m["role"] == "assistant"][0]
        assert len(assistant_hist["content"]) <= 32_000
        return httpx.Response(200, json=_openai_ok("done"))

    tools = (ToolDefinition("dev.health", "Health"),)
    executor = _FakeToolExecutor(
        tools, {"dev.health": ToolExecutionSummary("c1", "dev.health", {}, "completed", "ok")}
    )
    svc_tools = AssistantChatService(
        _make_config(tmp_path, max_cost_usd=1.0),
        transport=httpx.MockTransport(tool_handler),
    )
    res_tools = svc_tools.send_message("Run health", tool_executor=executor)
    assert res_tools.ok is True and res_tools.reply == "done"


def test_serialize_tool_output_bounds_heavy_escaping_post_json(tmp_path: Path) -> None:
    from max_core.assistant_chat import _MAX_TOOL_RESULT_CHARS, _serialize_tool_output

    for nasty in ("\x00" * 20_000, '"' * 20_000, "\n" * 20_000, {"blob": "\x00" * 20_000}):
        serialized = _serialize_tool_output("completed", nasty)
        assert len(serialized) <= _MAX_TOOL_RESULT_CHARS
        parsed = json.loads(serialized)
        assert parsed["truncated"] is True

    # Full turn integration with 20k NUL bytes tool output
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "fixture_read_file", "arguments": "{}"},
                        }
                    ],
                ),
            )
        payload = json.loads(req.content)
        assert len(payload["messages"][-1]["content"]) <= _MAX_TOOL_RESULT_CHARS
        return httpx.Response(200, json=_openai_ok("Handled binary output."))

    tools = (ToolDefinition("fixture.read_file", "Read file"),)
    executor = _FakeToolExecutor(
        tools,
        {
            "fixture.read_file": ToolExecutionSummary(
                "c1", "fixture.read_file", {}, "completed", "\x00" * 20_000
            )
        },
    )
    svc = AssistantChatService(
        _make_config(tmp_path, max_cost_usd=1.0),
        transport=httpx.MockTransport(handler),
    )
    res = svc.send_message("Read binary", tool_executor=executor)
    assert res.ok is True and res.reply == "Handled binary output."


def test_malformed_provider_tool_proposals_return_sanitized_provider_error(tmp_path: Path) -> None:
    tools = (ToolDefinition("dev.health", "Health"),)
    executor = _FakeToolExecutor(tools, {})

    for bad_call in (
        {
            "id": "call_1\n",
            "type": "function",
            "function": {"name": "dev_health", "arguments": "{}"},
        },
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "dev_health", "arguments": '{"x": NaN}'},
        },
        {
            "id": "call_3",
            "type": "function",
            "function": {
                "name": "dev_health",
                "arguments": json.dumps(
                    {"s": "é" * 17_000}, separators=(",", ":"), ensure_ascii=False
                ),
            },
        },
    ):
        svc = AssistantChatService(
            _make_config(tmp_path),
            transport=httpx.MockTransport(
                lambda _req, call=bad_call: httpx.Response(
                    200, json=_openai_ok("", tool_calls=[call])
                )
            ),
        )
        res = svc.send_message("Run", tool_executor=executor)
        assert res.ok is False
        assert res.error_code == "provider_error"
        assert executor.calls == []

    # Also test defensive ChatMessage guard directly if a response contains an invalid ToolProposal
    from max_core.gateway.contracts import GatewayResponse

    async def fake_complete_once(*_args: Any, **_kwargs: Any) -> GatewayResponse:
        return GatewayResponse(
            request_id="req1",
            provider_id="openai",
            model_id="gpt-4o-mini",
            content="",
            tool_proposals=(ToolProposal("bad\nid", "dev_health", {}),),
            finish_reason="tool_calls",
            usage=None,
            estimated_cost_usd=None,
        )

    svc_defensive = AssistantChatService(_make_config(tmp_path))
    svc_defensive._complete_once = fake_complete_once  # type: ignore[method-assign]
    defensive_res = svc_defensive.send_message("Run", tool_executor=executor)
    assert defensive_res.ok is False
    assert defensive_res.error_code == "provider_error"


def test_cumulative_turn_budget_enforced_across_tool_iterations_and_resume(tmp_path: Path) -> None:
    # Rates: $10/M input, $10/M output; max_cost_usd=$0.05.
    # Each call reports 2,000 input + 2,500 output = $0.045.
    # Iteration 1 spends $0.045 -> remaining $0.005 -> iteration 2 must fail budget_exceeded!
    calls = 0

    def expensive_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = _openai_ok(
            "",
            tool_calls=[
                {
                    "id": f"c_{calls}",
                    "type": "function",
                    "function": {"name": "dev_health", "arguments": "{}"},
                }
            ],
        )
        payload["usage"] = {"prompt_tokens": 2000, "completion_tokens": 2500, "total_tokens": 4500}
        return httpx.Response(200, json=payload)

    tools = (ToolDefinition("dev.health", "Health"),)
    executor = _FakeToolExecutor(
        tools,
        {"dev.health": ToolExecutionSummary("c", "dev.health", {}, "completed", "ok")},
    )
    cfg = _make_config(
        tmp_path,
        provider="openai",
        input_rate=10.0,
        output_rate=10.0,
        max_cost_usd=0.05,
    )
    svc = AssistantChatService(
        cfg,
        max_tool_iterations=5,
        max_output_tokens=1024,
        transport=httpx.MockTransport(expensive_handler),
    )
    res = svc.send_message("Loop until budget", tool_executor=executor)
    assert res.ok is False
    assert res.error_code == "budget_exceeded"
    assert calls == 1
    assert res.estimated_cost_usd is not None and res.estimated_cost_usd <= 0.05

    # Exact ceiling exhaustion ($0.05 spent on call 1 -> 2nd iteration stops before HTTP call)
    exact_calls = 0

    def exact_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal exact_calls
        exact_calls += 1
        payload = _openai_ok(
            "",
            tool_calls=[
                {
                    "id": "c_exact",
                    "type": "function",
                    "function": {"name": "dev_health", "arguments": "{}"},
                }
            ],
        )
        payload["usage"] = {"prompt_tokens": 2500, "completion_tokens": 2500, "total_tokens": 5000}
        return httpx.Response(200, json=payload)

    svc_exact = AssistantChatService(
        cfg,
        max_tool_iterations=5,
        max_output_tokens=1024,
        transport=httpx.MockTransport(exact_handler),
    )
    res_exact = svc_exact.send_message("Exhaust exact budget", tool_executor=executor)
    assert res_exact.ok is False
    assert res_exact.error_code == "budget_exceeded"
    assert exact_calls == 1

    # Cumulative budget carried across awaiting_approval -> resume_approval
    approval_calls = 0

    def approval_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal approval_calls
        approval_calls += 1
        payload = _openai_ok(
            "",
            tool_calls=[
                {
                    "id": "c_apr",
                    "type": "function",
                    "function": {"name": "task_create", "arguments": "{}"},
                }
            ],
        )
        payload["usage"] = {"prompt_tokens": 2500, "completion_tokens": 2500, "total_tokens": 5000}
        return httpx.Response(200, json=payload)

    apr_tools = (ToolDefinition("task.create", "Create task"),)
    apr_executor = _FakeToolExecutor(
        apr_tools,
        {
            "task.create": [
                ToolExecutionSummary(
                    "c_apr",
                    "task.create",
                    {},
                    "awaiting_approval",
                    "pending",
                    approval_id="apr-cost",
                ),
                ToolExecutionSummary(
                    "c_apr", "task.create", {}, "completed", "created", approval_id="apr-cost"
                ),
            ]
        },
    )
    svc_apr = AssistantChatService(
        cfg,
        max_tool_iterations=5,
        max_output_tokens=1024,
        transport=httpx.MockTransport(approval_handler),
    )
    paused = svc_apr.send_message("Create task", tool_executor=apr_executor)
    assert paused.ok is True
    assert paused.tool_executions[0].status == "awaiting_approval"
    resumed = svc_apr.resume_approval("apr-cost", tool_executor=apr_executor)
    assert resumed.ok is False
    assert resumed.error_code == "budget_exceeded"
    assert approval_calls == 1


def test_undeclared_tool_calls_denied_without_invoking_executor(tmp_path: Path) -> None:
    seen_payloads: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c_evil",
                            "type": "function",
                            "function": {"name": "evil_undeclared_tool", "arguments": '{"a":1}'},
                        }
                    ],
                ),
            )
        return httpx.Response(200, json=_openai_ok("Recovered after undeclared tool denial."))

    tools = (ToolDefinition("dev.health", "Health"),)
    executor = _FakeToolExecutor(
        tools,
        {"dev.health": ToolExecutionSummary("c", "dev.health", {}, "completed", "ok")},
    )
    svc = AssistantChatService(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    res = svc.send_message("Trigger undeclared tool", tool_executor=executor)
    assert res.ok is True
    assert res.reply == "Recovered after undeclared tool denial."
    assert executor.calls == []
    assert len(res.tool_executions) == 1
    assert res.tool_executions[0] == ToolExecutionSummary(
        call_id="c_evil",
        tool_name="evil_undeclared_tool",
        arguments={"a": 1},
        status="denied",
        output="undeclared_tool",
    )
    assert seen_payloads[1]["messages"][-1]["role"] == "tool"
    assert json.loads(seen_payloads[1]["messages"][-1]["content"]) == {
        "status": "denied",
        "output": "undeclared_tool",
    }

    # Also test undeclared sibling proposal in resume_approval
    apr_calls = 0

    def sibling_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal apr_calls
        apr_calls += 1
        if apr_calls == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c_ok",
                            "type": "function",
                            "function": {"name": "task_create", "arguments": "{}"},
                        },
                        {
                            "id": "c_bad",
                            "type": "function",
                            "function": {"name": "undeclared_sibling", "arguments": "{}"},
                        },
                    ],
                ),
            )
        return httpx.Response(200, json=_openai_ok("Finished resume."))

    apr_tools = (ToolDefinition("task.create", "Create task"),)
    apr_executor = _FakeToolExecutor(
        apr_tools,
        {
            "task.create": [
                ToolExecutionSummary(
                    "c_ok", "task.create", {}, "awaiting_approval", "pending", approval_id="apr-sib"
                ),
                ToolExecutionSummary(
                    "c_ok", "task.create", {}, "completed", "created", approval_id="apr-sib"
                ),
            ]
        },
    )
    svc_sib = AssistantChatService(
        _make_config(tmp_path), transport=httpx.MockTransport(sibling_handler)
    )
    svc_sib.send_message("Run batch", tool_executor=apr_executor)
    resumed = svc_sib.resume_approval("apr-sib", tool_executor=apr_executor)
    assert resumed.ok is True
    assert [c[0].name for c in apr_executor.calls] == ["task.create", "task.create"]
    assert resumed.tool_executions[-1].tool_name == "undeclared_sibling"
    assert resumed.tool_executions[-1].status == "denied"
    assert resumed.tool_executions[-1].output == "undeclared_tool"


def test_tool_name_codec_handles_leading_underscore_digit_tools(tmp_path: Path) -> None:
    seen_tools: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        if not seen_tools:
            seen_tools.extend(t["function"]["name"] for t in payload["tools"])
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": seen_tools[0], "arguments": "{}"},
                        }
                    ],
                ),
            )
        return httpx.Response(200, json=_openai_ok("Ran _1.tool."))

    tools = (ToolDefinition("_1.tool", "Leading underscore digit tool"),)
    executor = _FakeToolExecutor(
        tools,
        {"_1.tool": ToolExecutionSummary("c1", "_1.tool", {}, "completed", "ok")},
    )
    svc = AssistantChatService(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    res = svc.send_message("Run _1.tool", tool_executor=executor)
    assert res.ok is True
    assert res.reply == "Ran _1.tool."
    assert seen_tools[0] == "t_1_tool"
    assert res.tool_executions[0].tool_name == "_1.tool"
    assert executor.calls[0][0].name == "_1.tool"


@pytest.mark.parametrize(
    "secret_prompt",
    [
        "[" * 4000 + '{"API_KEY":"super_secret_token_12345"}' + "]" * 4000,
        '{"a":' * 400 + ' "x" ' + '{"API_KEY":"super_secret_token_12345"}',
        'Log: "{\\"API_KEY\\":\\"super_secret_token_12345\\"}"',
    ],
)
def test_chat_blocks_adversarial_m16_prompts_before_http(
    tmp_path: Path, secret_prompt: str
) -> None:
    svc = AssistantChatService(
        _make_config(tmp_path),
        transport=httpx.MockTransport(lambda _: pytest.fail("Secret leaked to provider")),
    )
    res = svc.send_message(secret_prompt)
    assert res.ok is False
    assert res.error_code == "egress_blocked"
    assert svc.history_len() == 0


def test_chat_blocks_large_prefixed_tool_output_secret(tmp_path: Path) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_openai_ok(
                "",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "dev_health", "arguments": "{}"},
                    }
                ],
            ),
        )

    tools = (ToolDefinition("dev.health", "Health"),)
    secret_out = ('{"a":' * 10) + (" " * 15_000) + '{"API_KEY":"mysecretvalue123456"}'
    executor = _FakeToolExecutor(
        tools,
        {"dev.health": ToolExecutionSummary("c1", "dev.health", {}, "completed", secret_out)},
    )
    svc = AssistantChatService(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    res = svc.send_message("Check health", tool_executor=executor)
    assert res.ok is False
    assert res.error_code == "egress_blocked"


def test_chat_recursion_hardening_across_prompt_provider_and_tool_outputs(tmp_path: Path) -> None:
    for benign_brackets in ("[" * 5000, '{"a":' * 5000):
        svc_ok = AssistantChatService(
            _make_config(tmp_path, max_cost_usd=1.0),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_openai_ok("ok"))),
        )
        res_ok = svc_ok.send_message(benign_brackets)
        assert res_ok.ok is True
        assert res_ok.reply == "ok"

    # Malformed deep HTTP 200 body -> provider_error with malformed response message
    svc_bad_body = AssistantChatService(
        _make_config(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="[" * 5000 + "]" * 5000)),
    )
    res_bad_body = svc_bad_body.send_message("Hi")
    assert res_bad_body.ok is False
    assert res_bad_body.error_code == "provider_error"
    assert res_bad_body.reply == "The AI provider returned a malformed response."

    # OpenAI deep tool arguments -> invalid tool proposal
    svc_bad_openai_tool = AssistantChatService(
        _make_config(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "dev_health", "arguments": "[" * 5000},
                        }
                    ],
                ),
            )
        ),
    )
    res_bad_openai_tool = svc_bad_openai_tool.send_message("Run")
    assert res_bad_openai_tool.ok is False
    assert res_bad_openai_tool.error_code == "provider_error"
    assert res_bad_openai_tool.reply == "The AI provider returned an invalid tool proposal."

    # Anthropic depth-2000 tool_use -> rejected cleanly on Turn 1 as invalid tool proposal by depth cap
    deep_2000_json = '{"a":' * 2000 + "1" + "}" * 2000
    anthropic_deep_2000 = (
        '{"id":"msg_test","type":"message","role":"assistant","model":"claude-sonnet-4-5-20250929",'
        '"content":[{"type":"tool_use","id":"tu1","name":"dev_health","input":'
        + deep_2000_json
        + "}],"
        '"usage":{"input_tokens":150,"output_tokens":30},"stop_reason":"tool_use"}'
    ).encode("utf-8")
    svc_bad_anthropic = AssistantChatService(
        _make_config(tmp_path, provider="anthropic", max_cost_usd=1.0),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=anthropic_deep_2000,
                headers={"content-type": "application/json"},
            )
        ),
    )
    res_bad_anthropic = svc_bad_anthropic.send_message("Run")
    assert res_bad_anthropic.ok is False
    assert res_bad_anthropic.error_code == "provider_error"
    assert res_bad_anthropic.reply in {
        "The AI provider returned an invalid tool proposal.",
        "The AI provider returned a malformed response.",
    }

    # Anthropic depth-2985 tool_use -> also fails cleanly with domain provider_error (never RecursionError)
    deep_2985_json = '{"a":' * 2985 + "1" + "}" * 2985
    anthropic_deep_2985 = (
        '{"id":"msg_test","type":"message","role":"assistant","model":"claude-sonnet-4-5-20250929",'
        '"content":[{"type":"tool_use","id":"tu1","name":"dev_health","input":'
        + deep_2985_json
        + "}],"
        '"usage":{"input_tokens":150,"output_tokens":30},"stop_reason":"tool_use"}'
    ).encode("utf-8")
    svc_bad_anthropic_2985 = AssistantChatService(
        _make_config(tmp_path, provider="anthropic", max_cost_usd=1.0),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=anthropic_deep_2985,
                headers={"content-type": "application/json"},
            )
        ),
    )
    res_bad_anthropic_2985 = svc_bad_anthropic_2985.send_message("Run")
    assert res_bad_anthropic_2985.ok is False
    assert res_bad_anthropic_2985.error_code == "provider_error"
    assert res_bad_anthropic_2985.reply in {
        "The AI provider returned an invalid tool proposal.",
        "The AI provider returned a malformed response.",
    }

    # Tool executor returning depth-3000 benign dict, depth-3000 secret dict, and "[" * 5000
    deep_3000_benign: dict[str, Any] = {"ok": 1}
    deep_3000_secret: dict[str, Any] = {"API_KEY": "deep_secret_token_12345"}
    for _ in range(3000):
        deep_3000_benign = {"a": deep_3000_benign}
        deep_3000_secret = {"a": deep_3000_secret}

    tools = (ToolDefinition("dev.health", "Health"),)
    for benign_out in (deep_3000_benign, "[" * 5000):
        calls = 0

        def tool_handler(_req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    200,
                    json=_openai_ok(
                        "",
                        tool_calls=[
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "dev_health", "arguments": "{}"},
                            }
                        ],
                    ),
                )
            return httpx.Response(200, json=_openai_ok("Done"))

        executor = _FakeToolExecutor(
            tools,
            {"dev.health": ToolExecutionSummary("c1", "dev.health", {}, "completed", benign_out)},
        )
        svc_tool_ok = AssistantChatService(
            _make_config(tmp_path, max_cost_usd=1.0),
            transport=httpx.MockTransport(tool_handler),
        )
        res_tool_ok = svc_tool_ok.send_message("Run", tool_executor=executor)
        assert res_tool_ok.ok is True
        assert res_tool_ok.reply == "Done"

    secret_executor = _FakeToolExecutor(
        tools,
        {"dev.health": ToolExecutionSummary("c1", "dev.health", {}, "completed", deep_3000_secret)},
    )
    svc_tool_secret = AssistantChatService(
        _make_config(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "dev_health", "arguments": "{}"},
                        }
                    ],
                ),
            )
        ),
    )
    res_tool_secret = svc_tool_secret.send_message("Run", tool_executor=secret_executor)
    assert res_tool_secret.ok is False
    assert res_tool_secret.error_code == "egress_blocked"


def test_chat_lone_surrogates_rejected_cleanly_without_history_poisoning(tmp_path: Path) -> None:
    # 1. Surrogate user_text -> invalid_input, 0 HTTP calls, 0 history
    svc_user = AssistantChatService(
        _make_config(tmp_path),
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Surrogate user text reached transport")
        ),
    )
    res_user = svc_user.send_message("hello \ud800 world")
    assert res_user.ok is False
    assert res_user.error_code == "invalid_input"
    assert svc_user.history_len() == 0

    # 2. Provider plain reply with lone surrogate -> provider_error, no history poisoning
    call_count = 0

    def reply_surrogate_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                content=b'{"id":"1","model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"message":{"role":"assistant","content":"hi \\ud800"},"finish_reason":"stop"}]}',
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json=_openai_ok("Follow up ok"))

    svc_reply = AssistantChatService(
        _make_config(tmp_path), transport=httpx.MockTransport(reply_surrogate_handler)
    )
    res_bad_reply = svc_reply.send_message("First")
    assert res_bad_reply.ok is False
    assert res_bad_reply.error_code == "provider_error"
    assert svc_reply.history_len() == 0
    res_followup = svc_reply.send_message("follow up")
    assert res_followup.ok is True
    assert res_followup.reply == "Follow up ok"

    # 3. Provider surrogate content + tool_calls (with and without executor) -> provider_error
    tools = (ToolDefinition("dev.health", "Health"),)
    executor = _FakeToolExecutor(
        tools,
        {"dev.health": ToolExecutionSummary("c1", "dev.health", {}, "completed", "ok")},
    )
    bad_tool_content_body = (
        b'{"id":"1","model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,'
        b'"message":{"role":"assistant","content":"bad \\ud800","tool_calls":['
        b'{"id":"c1","type":"function","function":{"name":"dev_health","arguments":"{}"}}'
        b']},"finish_reason":"tool_calls"}]}'
    )
    for maybe_exec in (None, executor):
        svc_tc = AssistantChatService(
            _make_config(tmp_path),
            transport=httpx.MockTransport(
                lambda _r, b=bad_tool_content_body: httpx.Response(
                    200, content=b, headers={"content-type": "application/json"}
                )
            ),
        )
        res_tc = svc_tc.send_message("Trigger", tool_executor=maybe_exec)
        assert res_tc.ok is False
        assert res_tc.error_code == "provider_error"
        assert svc_tc.history_len() == 0
    assert executor.calls == []

    # 4. Provider tool proposal with surrogate in arguments or call_id -> invalid tool proposal
    bad_arg_openai = (
        b'{"id":"1","model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,'
        b'"message":{"role":"assistant","content":null,"tool_calls":['
        b'{"id":"c1","type":"function","function":{"name":"dev_health","arguments":"{\\"x\\":\\"\\\\ud800\\"}"}}'
        b']},"finish_reason":"tool_calls"}]}'
    )
    bad_id_openai = (
        b'{"id":"1","model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,'
        b'"message":{"role":"assistant","content":null,"tool_calls":['
        b'{"id":"c_\\ud800","type":"function","function":{"name":"dev_health","arguments":"{}"}}'
        b']},"finish_reason":"tool_calls"}]}'
    )
    for body in (bad_arg_openai, bad_id_openai):
        svc_bad_prop = AssistantChatService(
            _make_config(tmp_path),
            transport=httpx.MockTransport(
                lambda _r, b=body: httpx.Response(
                    200, content=b, headers={"content-type": "application/json"}
                )
            ),
        )
        res_bad_prop = svc_bad_prop.send_message("Run", tool_executor=executor)
        assert res_bad_prop.ok is False
        assert res_bad_prop.error_code == "provider_error"
        assert res_bad_prop.reply == "The AI provider returned an invalid tool proposal."
    assert executor.calls == []

    bad_arg_anthropic = (
        b'{"id":"msg1","type":"message","role":"assistant","model":"claude-sonnet-4-5-20250929",'
        b'"content":[{"type":"tool_use","id":"tu1","name":"dev_health","input":{"x":"\\ud800"}}],'
        b'"usage":{"input_tokens":10,"output_tokens":10},"stop_reason":"tool_use"}'
    )
    svc_anth_prop = AssistantChatService(
        _make_config(tmp_path, provider="anthropic"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=bad_arg_anthropic, headers={"content-type": "application/json"}
            )
        ),
    )
    res_anth_prop = svc_anth_prop.send_message("Run", tool_executor=executor)
    assert res_anth_prop.ok is False
    assert res_anth_prop.error_code == "provider_error"
    assert res_anth_prop.reply == "The AI provider returned an invalid tool proposal."
    assert executor.calls == []

    # 5. Surrogate tool outputs pass _output_has_secret and _serialize_tool_output cleanly
    for sur_out in ("\ud800" * 5000, {"bad_surrogate": "\ud800\udfff"}):
        turn_calls = 0

        def sur_out_handler(_req: httpx.Request) -> httpx.Response:
            nonlocal turn_calls
            turn_calls += 1
            if turn_calls == 1:
                return httpx.Response(
                    200,
                    json=_openai_ok(
                        "",
                        tool_calls=[
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "dev_health", "arguments": "{}"},
                            }
                        ],
                    ),
                )
            return httpx.Response(200, json=_openai_ok("Handled surrogate output"))

        sur_exec = _FakeToolExecutor(
            tools,
            {"dev.health": ToolExecutionSummary("c1", "dev.health", {}, "completed", sur_out)},
        )
        svc_sur_out = AssistantChatService(
            _make_config(tmp_path, max_cost_usd=1.0),
            transport=httpx.MockTransport(sur_out_handler),
        )
        res_sur_out = svc_sur_out.send_message("Run", tool_executor=sur_exec)
        assert res_sur_out.ok is True
        assert res_sur_out.reply == "Handled surrogate output"

    # 6. Mutated ToolDefinition.parameters containing surrogate -> rejected cleanly in codec and _prepare_request
    mut_tool = ToolDefinition("dev.health", "Health", {"type": "object"})
    object.__setattr__(mut_tool, "parameters", {"type": "object", "bad": "\ud800"})
    svc_mut_tool = AssistantChatService(
        _make_config(tmp_path),
        tools=(mut_tool,),
        transport=httpx.MockTransport(lambda _: pytest.fail("Should fail before transport")),
    )
    res_mut_tool = svc_mut_tool.send_message("Hi")
    assert res_mut_tool.ok is False
    assert res_mut_tool.error_code == "invalid_provider_config"

    from max_core.assistant_chat import _build_model_spec
    from max_core.gateway.contracts import ChatMessage

    cfg_mut = _make_config(tmp_path)
    assert cfg_mut.active_provider is not None
    spec_mut, max_cost_mut = _build_model_spec(cfg_mut.active_provider, has_tools=True)
    prepared_mut = svc_mut_tool._prepare_request(
        spec=spec_mut,
        max_cost_decimal=max_cost_mut,
        mode=ReplyMode.CONVERSATION,
        history_groups=[],
        turn_messages=[ChatMessage("user", "Hi")],
        wire_tools=(mut_tool,),
    )
    assert isinstance(prepared_mut, ChatTurnResult)
    assert prepared_mut.ok is False
    assert prepared_mut.error_code == "invalid_request"

    # 7. Defensive ChatMessage guard if _complete_once returns surrogate GatewayResponse
    from max_core.gateway.contracts import GatewayResponse

    svc_direct = AssistantChatService(_make_config(tmp_path))

    async def fake_complete_once(_spec: Any, _req: Any) -> GatewayResponse:
        return GatewayResponse("r1", "openai", "gpt-4o-mini", "bad \ud800", (), "stop", None, None)

    svc_direct._complete_once = fake_complete_once  # type: ignore[method-assign]
    res_direct = svc_direct.send_message("Hi")
    assert res_direct.ok is False
    assert res_direct.error_code == "provider_error"
    assert svc_direct.history_len() == 0

    # 8. resume_approval where continuation provider reply contains "\ud800" -> no history poisoning
    apr_tools = (ToolDefinition("task.create", "Create task"),)
    apr_exec = _FakeToolExecutor(
        apr_tools,
        {
            "task.create": [
                ToolExecutionSummary(
                    "c_apr",
                    "task.create",
                    {},
                    "awaiting_approval",
                    "pending",
                    approval_id="apr-sur",
                ),
                ToolExecutionSummary(
                    "c_apr", "task.create", {}, "completed", "created", approval_id="apr-sur"
                ),
            ]
        },
    )
    apr_turn = 0

    def apr_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal apr_turn
        apr_turn += 1
        if apr_turn == 1:
            return httpx.Response(
                200,
                json=_openai_ok(
                    "",
                    tool_calls=[
                        {
                            "id": "c_apr",
                            "type": "function",
                            "function": {"name": "task_create", "arguments": "{}"},
                        }
                    ],
                ),
            )
        if apr_turn == 2:
            return httpx.Response(
                200,
                content=b'{"id":"2","model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"message":{"role":"assistant","content":"resumed \\ud800"},"finish_reason":"stop"}]}',
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json=_openai_ok("Clean turn"))

    svc_apr = AssistantChatService(
        _make_config(tmp_path, max_cost_usd=1.0),
        transport=httpx.MockTransport(apr_handler),
    )
    paused = svc_apr.send_message("Create task", tool_executor=apr_exec)
    assert paused.ok is True
    resumed = svc_apr.resume_approval("apr-sur", tool_executor=apr_exec)
    assert resumed.ok is False
    assert resumed.error_code == "provider_error"
    clean_next = svc_apr.send_message("Next question", tool_executor=apr_exec)
    assert clean_next.ok is True
    assert clean_next.reply == "Clean turn"


def test_cloud_model_never_receives_private_local_read_tool_definitions(tmp_path: Path) -> None:
    """Cloud inference cannot silently list or read local files, notes, or tasks."""
    private_reads = (
        "workspace.list_files",
        "workspace.read_file",
        "notes.list",
        "task.list",
        "task.status",
        "system.status",
    )
    allowed = ("dev.health", "dev.add", "task.create", "notes.save")
    definitions = tuple(ToolDefinition(name, name) for name in (*private_reads, *allowed))
    executor = _FakeToolExecutor(definitions, {})
    svc = AssistantChatService(_make_config(tmp_path), tools=definitions)
    selected = {definition.name for definition in svc._resolve_tools(executor)}
    assert set(private_reads).isdisjoint(selected)
    assert selected == set(allowed)
    assert {definition.name for definition in svc._resolve_tools(None)} == set(allowed)
