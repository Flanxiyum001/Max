"""Evented turn/step agent loop over the session log.

A turn is zero or more steps; a step is one model request plus the tool calls
it proposes. Request messages are always ``derive_messages(log)`` so nothing
reaches the model without being logged. Effectful tools need the owner's
approval callback; ``tools/pre-execute`` listeners can only *deny*.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from max_core.agent_harness import CompletionGateway
from max_core.gateway.contracts import ChatMessage, GatewayRequest
from max_core.harness_core.context import Context
from max_core.harness_core.session import SessionLog, derive_messages
from max_core.tool_loop import ApprovalCallback, parse_tool_calls, tool_system_prompt
from max_core.tool_recovery import safe_error_code
from max_core.tools import MAX_RESULT_CHARS, ToolError, ToolRegistry, ToolRisk

RequestFactory = Callable[[tuple[ChatMessage, ...]], GatewayRequest]
StopReason = Literal["answered", "step_limit", "model_failed"]


@dataclass(frozen=True, slots=True)
class TurnResult:
    answer: str
    steps: int
    stopped_reason: StopReason
    error_code: str | None = None


class AgentLoop:
    def __init__(
        self,
        ctx: Context,
        gateway: CompletionGateway,
        registry: ToolRegistry,
        *,
        approve: ApprovalCallback | None = None,
        max_steps: int = 6,
        max_calls_per_step: int = 4,
        tool_timeout_seconds: float = 10.0,
    ) -> None:
        if not 1 <= max_steps <= 12 or not 1 <= max_calls_per_step <= 8:
            raise ValueError("invalid_loop_budget")
        if not 0 < tool_timeout_seconds <= 60:
            raise ValueError("invalid_loop_deadline")
        self._ctx = ctx
        self._gateway = gateway
        self._registry = registry
        self._approve = approve
        self._max_steps = max_steps
        self._max_calls = max_calls_per_step
        self._tool_timeout = tool_timeout_seconds

    def start_session(self, system_prompt: str = "") -> SessionLog:
        log = SessionLog()
        if system_prompt:
            log.append("system/message", text=system_prompt)
        tools = tool_system_prompt(self._registry)
        if tools:
            log.append("system/message", text=tools)
        return log

    async def _call_tool(self, log: SessionLog, turn: int, name: str, arguments: object) -> str:
        tool = self._registry.get(name)
        label = tool.name if tool is not None else "unrecognized"
        log.append("tool/call", turn=turn, name=label)
        if tool is None:
            return self._deny(log, turn, label, "unknown_tool")
        try:
            clean = tool.validate(arguments)
        except ToolError as exc:
            return self._deny(log, turn, label, safe_error_code(exc.code))
        payload = {"turn": turn, "name": label, "effectful": tool.risk == ToolRisk.EFFECTFUL}
        reason = self._ctx.deny_reason("tools/pre-execute", payload)
        if reason is not None:
            return self._deny(log, turn, label, safe_error_code(reason))
        if tool.risk == ToolRisk.EFFECTFUL:
            try:
                approved = self._approve is not None and self._approve(name, dict(clean)) is True
            except Exception:
                approved = False
            if not approved:
                return self._deny(log, turn, label, "owner_denied")
        try:
            if tool.risk == ToolRisk.EFFECTFUL:
                output = tool.handler(clean)  # never detach writes into a thread
            else:
                output = await asyncio.wait_for(
                    asyncio.to_thread(tool.handler, clean), timeout=self._tool_timeout
                )
            body = json.dumps({"output": str(output)[:MAX_RESULT_CHARS]})
            status = "executed"
        except ToolError as exc:
            body, status = json.dumps({"error": safe_error_code(exc.code)}), "failed"
        except TimeoutError:
            body, status = json.dumps({"error": "tool_timeout"}), "failed"
        except Exception:
            body, status = json.dumps({"error": "tool_failed"}), "failed"
        log.append("tool/result", turn=turn, label=label, text=body, status=status)
        self._ctx.emit("tools/post-execute", {"turn": turn, "name": label, "status": status})
        return status

    @staticmethod
    def _deny(log: SessionLog, turn: int, label: str, code: str) -> str:
        log.append("tool/denied", turn=turn, name=label, code=code)
        log.append("tool/result", turn=turn, label=label, text=json.dumps({"error": code}))
        return "denied"

    async def run_turn(self, log: SessionLog, user_text: str, make: RequestFactory) -> TurnResult:
        turn = sum(1 for e in log if e.kind == "turn/start")
        log.append("turn/start", turn=turn)
        self._ctx.emit("turn/start", {"turn": turn})
        log.append("user/message", text=user_text)
        result = await self._steps(log, turn, make)
        log.append("turn/end", turn=turn, reason=result.stopped_reason)
        self._ctx.emit("turn/end", {"turn": turn, "reason": result.stopped_reason})
        return result

    async def _steps(self, log: SessionLog, turn: int, make: RequestFactory) -> TurnResult:
        for step in range(self._max_steps):
            log.append("step/start", turn=turn, step=step)
            self._ctx.emit("step/start", {"turn": turn, "step": step})
            request = make(derive_messages(log))
            self._ctx.emit("agent/request", {"turn": turn, "step": step})
            try:
                response = await self._gateway.complete(request)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or type(exc).__name__)[:64]
                log.append("step/end", turn=turn, step=step, outcome="model_failed")
                return TurnResult(
                    "I could not reach my model just now.", step + 1, "model_failed", code
                )
            log.append("assistant/message", text=response.content.strip())
            calls = parse_tool_calls(response)
            if not calls:
                log.append("step/end", turn=turn, step=step, outcome="answered")
                self._ctx.emit("step/end", {"turn": turn, "step": step})
                return TurnResult(response.content.strip(), step + 1, "answered")
            for name, arguments in calls[: self._max_calls]:
                await self._call_tool(log, turn, name, arguments)
            if len(calls) > self._max_calls:
                log.append(
                    "tool/result", turn=turn, label="limit", text='{"error": "too_many_calls"}'
                )
            log.append("step/end", turn=turn, step=step, outcome="tools")
            self._ctx.emit("step/end", {"turn": turn, "step": step})
        return TurnResult(
            "I hit my tool-step limit before finishing.", self._max_steps, "step_limit"
        )
