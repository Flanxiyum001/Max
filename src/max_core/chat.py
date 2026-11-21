"""Interactive MAX terminal chat with bounded tool calling.

Run ``python -m max_core.chat``. The model is configured with the existing
``MAX_TRIAL_*`` variables (see docs/M23_TOOL_LOOP.md): a local Ollama /
llama.cpp endpoint or an owner-enabled free OpenAI-compatible API.
Set ``MAX_WORKSPACE_DIR`` to give MAX read access to one folder.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from max_core.gateway import (
    ChatMessage,
    Gateway,
    GatewayRequest,
    HTTPAdapter,
    InferenceDestination,
    Privacy,
)
from max_core.gateway_trial import TrialConfigurationError, TrialSettings
from max_core.persona import persona_message
from max_core.tool_loop import ToolLoop, ToolStep
from max_core.tools import builtin_registry

MAX_HISTORY = 30


def request_factory(
    settings: TrialSettings,
) -> Callable[[tuple[ChatMessage, ...]], GatewayRequest]:
    spec = settings.spec
    is_cloud = spec.inference_destination == InferenceDestination.CLOUD
    output = min(1024, max(256, spec.context_tokens // 4))

    def make(messages: tuple[ChatMessage, ...]) -> GatewayRequest:
        envelope = sum(len(m.content.encode("utf-8")) + 32 for m in messages)
        return GatewayRequest(
            request_id=uuid4().hex,
            messages=messages,
            # Cloud routes only ever see what the owner typed into a PUBLIC session.
            privacy=Privacy.PUBLIC if is_cloud else Privacy.CONFIDENTIAL,
            max_input_tokens=max(envelope, 1),
            max_output_tokens=output,
            max_cost_usd=settings.max_cost_usd,
            cloud_allowed=is_cloud,
            local_only=not is_cloud,
            allow_fallback=False,
            timeout_seconds=60.0,
        )

    return make


def _ask_approval(name: str, arguments: dict[str, Any]) -> bool:
    print(f"\n  MAX wants to run {name} with:")
    for key, value in arguments.items():
        preview = str(value)
        print(f"    {key}: {preview[:300]}{'...' if len(preview) > 300 else ''}")
    try:
        return input("  Allow this exact action? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def _show_step(step: ToolStep) -> None:
    if step.status == "executed":
        print(f"  [tool] {step.tool_name} done")
    elif step.status in {"denied", "failed"}:
        print(f"  [tool] {step.tool_name} {step.status}: {step.error_code}")


async def chat(settings: TrialSettings, workspace: Path | None) -> None:
    registry = builtin_registry(workspace)
    make = request_factory(settings)
    system = [persona_message()] if settings.persona_enabled else []
    history: list[ChatMessage] = []
    where = (
        "cloud API"
        if settings.spec.inference_destination == InferenceDestination.CLOUD
        else "local"
    )
    print(
        f"MAX online ({where} model, {len(registry)} tools). Type /exit to leave, /reset to clear."
    )
    if settings.spec.inference_destination == InferenceDestination.CLOUD:
        print("Note: messages and tool results in this session are sent to the cloud provider.")
    async with httpx.AsyncClient(trust_env=False, timeout=70.0) as client:
        loop = ToolLoop(
            Gateway((settings.spec,), HTTPAdapter(client)),
            registry,
            approve=_ask_approval,
            on_step=_show_step,
        )
        while True:
            try:
                text = (await asyncio.to_thread(input, "\nyou > ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                return
            if text == "/reset":
                history.clear()
                print("(conversation cleared)")
                continue
            history.append(ChatMessage("user", text[:20_000]))
            try:
                result = await loop.run(system + history[-MAX_HISTORY:], make)
            except TimeoutError:
                print("max > (that took too long; try again)")
                history.pop()
                continue
            print(f"max > {result.answer}")
            history.append(ChatMessage("assistant", result.answer[:20_000]))


def main() -> int:
    try:
        settings = TrialSettings.from_env()
    except TrialConfigurationError as exc:
        print(f"MAX chat configuration error: {exc.code}", file=sys.stderr)
        print("See docs/M23_TOOL_LOOP.md for setup.", file=sys.stderr)
        return 2
    raw = os.environ.get("MAX_WORKSPACE_DIR")
    workspace = Path(raw) if raw else None
    try:
        asyncio.run(chat(settings, workspace))
    except ValueError as exc:
        print(f"MAX chat error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
