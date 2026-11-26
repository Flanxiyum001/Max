"""Interactive MAX terminal chat with bounded tool calling.

Run ``python -m max_core.chat``. The model is configured with the existing
``MAX_TRIAL_*`` variables (see docs/M23_TOOL_LOOP.md): a local Ollama /
llama.cpp endpoint or an owner-enabled free OpenAI-compatible API.
Set ``MAX_WORKSPACE_DIR`` to give MAX read access to one folder.
"""

from __future__ import annotations

import asyncio
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
from max_core.persona import ReplyMode, persona_message
from max_core.tool_loop import ToolLoop, ToolStep
from max_core.tools import ToolRegistry, builtin_registry

MAX_HISTORY = 30
# Rough per-screenshot input budget for local vision models.
_IMAGE_TOKEN_ESTIMATE = 1_600


def request_factory(
    settings: TrialSettings,
) -> Callable[[tuple[ChatMessage, ...]], GatewayRequest]:
    spec = settings.spec
    is_cloud = spec.inference_destination == InferenceDestination.CLOUD
    output = min(1024, max(256, spec.context_tokens // 4))

    def make(messages: tuple[ChatMessage, ...]) -> GatewayRequest:
        envelope = sum(
            len(m.content.encode("utf-8")) + 32 + _IMAGE_TOKEN_ESTIMATE * len(m.images)
            for m in messages
        )
        has_images = any(m.images for m in messages)
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
            required_capabilities=frozenset({"text", "vision"} if has_images else {"text"}),
        )

    return make


def _ask_approval(name: str, arguments: dict[str, Any]) -> bool:
    print(f"\n  MAX wants to run {name} with:")
    # Full repr makes newlines visible and prevents a hidden suffix from being
    # approved under the false claim that only the first 300 chars were shown.
    for key, value in arguments.items():
        print(f"    {key}: {value!r}")
    try:
        return input("  Allow this exact action? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def _show_step(step: ToolStep) -> None:
    if step.status == "executed":
        print(f"  [tool] {step.tool_name} done")
    elif step.status in {"denied", "failed"}:
        print(f"  [tool] {step.tool_name} {step.status}: {step.error_code}")


def registry_for_session(
    settings: TrialSettings, workspace: Path | None, options: Any = None
) -> ToolRegistry:
    """Cloud models cannot request local workspace reads or note writes.

    Merely printing a warning is not consent to transmit local file contents.
    With ``options`` (a ``SessionOptions``) the full assistant toolkit is added.
    """
    is_cloud = settings.spec.inference_destination == InferenceDestination.CLOUD
    if options is None:
        return builtin_registry() if is_cloud else builtin_registry(workspace)
    from max_core.assistant_tools import assistant_registry

    return assistant_registry(
        data_dir=options.data_dir,
        workspace=workspace,
        cloud=is_cloud,
        cloud_personal_tools=options.cloud_personal_tools,
        enable_shell=options.enable_shell,
        enable_web=options.enable_web,
    )


def session_system_messages(settings: TrialSettings, mode: ReplyMode) -> list[ChatMessage]:
    """Only an explicit, opt-in persona mode can add engineering guidance."""
    return [persona_message(mode)] if settings.persona_enabled else []


async def chat(settings: TrialSettings, workspace: Path | None, options: Any = None) -> None:
    registry = registry_for_session(settings, workspace, options)
    store = None
    if options is not None and "reminders.list" in {t.name for t in registry}:
        from max_core.assistant_tools import AssistantStore

        store = AssistantStore(options.data_dir)
    make = request_factory(settings)
    mode = ReplyMode.CONVERSATION
    history: list[ChatMessage] = []
    where = (
        "cloud API"
        if settings.spec.inference_destination == InferenceDestination.CLOUD
        else "local"
    )
    print(
        f"MAX online ({where} model, {len(registry)} tools). "
        "Type /exit to leave, /reset to clear, /mode engineering for coding guidance."
    )
    if settings.spec.inference_destination == InferenceDestination.CLOUD:
        print(
            "Note: public chat text and non-file tool results go to the cloud provider. "
            "Workspace reads and note writes are disabled in cloud mode."
        )
    async with httpx.AsyncClient(trust_env=False, timeout=70.0) as client:
        loop = ToolLoop(
            Gateway((settings.spec,), HTTPAdapter(client)),
            registry,
            approve=_ask_approval,
            on_step=_show_step,
        )
        while True:
            if store is not None:
                for item in store.pop_due():
                    print(f"\n  [reminder] {item['text']}")
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
            if text == "/mode" or text.startswith("/mode "):
                chosen = text.removeprefix("/mode").strip().lower()
                if not settings.persona_enabled:
                    print("(mode unavailable: set MAX_TRIAL_PERSONA=yes to opt in)")
                elif chosen not in {"conversation", "engineering"}:
                    print("(use /mode engineering or /mode conversation)")
                else:
                    mode = ReplyMode(chosen)
                    history.clear()  # avoid silently carrying work data across modes
                    print(f"(MAX {mode.value} mode; conversation cleared)")
                continue
            history.append(ChatMessage("user", text[:20_000]))
            try:
                result = await loop.run(
                    session_system_messages(settings, mode) + history[-MAX_HISTORY:], make
                )
            except TimeoutError:
                print("max > (that took too long; try again)")
                history.pop()
                continue
            print(f"max > {result.answer}")
            history.append(ChatMessage("assistant", result.answer[:20_000]))


def main() -> int:
    from max_core.assistant_session import SessionOptions, merged_environment

    env = merged_environment()
    try:
        settings = TrialSettings.from_env(env)
    except TrialConfigurationError as exc:
        print(f"MAX chat configuration error: {exc.code}", file=sys.stderr)
        print("See docs/M23_TOOL_LOOP.md for setup.", file=sys.stderr)
        return 2
    options = SessionOptions.from_env(env)
    try:
        asyncio.run(chat(settings, options.workspace, options))
    except ValueError as exc:
        print(f"MAX chat error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
