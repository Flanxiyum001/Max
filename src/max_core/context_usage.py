"""Context window usage breakdown for MAX.

Computes an estimated token-budget breakdown (~4 characters/token) across the
active session's system prompt, system tools, MCP tools, memory entries,
conversation messages, autocompact buffer, and remaining free space.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from max_core.persona import persona_message

_CHARS_PER_TOKEN = 4
_DEFAULT_CONTEXT_TOKENS = 128_000


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def format_token_count(tokens: int) -> str:
    if tokens >= 1_000_000:
        val = tokens / 1_000_000
        return f"{val:.1f}M".replace(".0M", "M")
    if tokens >= 1_000:
        val = tokens / 1_000
        return f"{val:.1f}K".replace(".0K", "K")
    return str(tokens)


def _is_mcp_tool(name: str) -> bool:
    return name.startswith(("mcp.", "github.", "browser_"))


def build_context_snapshot(
    session: Any,
    *,
    previous_used_tokens: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build a display-safe JSON-serializable context breakdown for ``session``."""
    total_tokens = _DEFAULT_CONTEXT_TOKENS
    settings = getattr(session, "settings", None)
    spec = getattr(settings, "spec", None)
    spec_tokens = getattr(spec, "context_window_tokens", None)
    if isinstance(spec_tokens, int) and spec_tokens >= 1_000:
        total_tokens = spec_tokens

    registry = list(getattr(session, "registry", []) or [])
    mcp_tool_names: list[str] = []
    system_tool_names: list[str] = []
    mcp_tool_tokens = 0
    system_tool_tokens = 0

    for tool in registry:
        name = str(getattr(tool, "name", "tool"))
        describe = getattr(tool, "describe", None)
        schema_text = json.dumps(describe()) if callable(describe) else name
        tok = estimate_tokens(schema_text)
        if _is_mcp_tool(name):
            mcp_tool_names.append(name)
            mcp_tool_tokens += tok
        else:
            system_tool_names.append(name)
            system_tool_tokens += tok

    history = list(getattr(session, "history", []) or [])
    message_tokens = sum(
        estimate_tokens(f"{getattr(m, 'role', '')}: {getattr(m, 'content', '')}") for m in history
    )

    # System prompt (persona + tool/harness framing)
    system_prompt_tokens = estimate_tokens(persona_message().content)
    if registry:
        system_prompt_tokens += 260  # tool protocol + engineering workflow prompt

    # Skills / workflow instructions
    skills_tokens = (
        320 if any(n.startswith("plan.") or n.startswith("code.") for n in system_tool_names) else 0
    )

    # MCP server instructions
    mcp_instructions_tokens = 180 if mcp_tool_names else 0

    # Memory files / stored memories
    memory_names: list[str] = []
    memory_tokens = 0
    store = getattr(session, "store", None)
    list_memories = getattr(store, "list_memories", None)
    if callable(list_memories):
        try:
            memories = list(list_memories())
            for item in memories[:100]:
                label = str(item.get("key") or item.get("text") or "memory")[:80]
                memory_names.append(label)
                memory_tokens += estimate_tokens(json.dumps(item, default=str))
        except Exception:
            pass

    used_tokens = (
        mcp_tool_tokens
        + message_tokens
        + skills_tokens
        + system_tool_tokens
        + system_prompt_tokens
        + mcp_instructions_tokens
        + memory_tokens
    )
    autocompact_buffer = min(33_000, max(4_000, total_tokens // 12))
    free_space = max(0, total_tokens - used_tokens - autocompact_buffer)
    used_percent = round(min(100.0, (used_tokens / total_tokens) * 100.0), 1)
    since_last = (
        max(0, used_tokens - previous_used_tokens) if previous_used_tokens is not None else 0
    )

    def _pct(val: int) -> float:
        return round((val / total_tokens) * 100.0, 1)

    categories = [
        {
            "key": "mcp_tools",
            "label": "MCP tools",
            "tokens": mcp_tool_tokens,
            "percent": _pct(mcp_tool_tokens),
            "color": "#3b82f6",
        },
        {
            "key": "messages",
            "label": "Messages",
            "tokens": message_tokens,
            "percent": _pct(message_tokens),
            "color": "#f97316",
        },
        {
            "key": "skills",
            "label": "Skills",
            "tokens": skills_tokens,
            "percent": _pct(skills_tokens),
            "color": "#10b981",
        },
        {
            "key": "system_tools",
            "label": "System tools",
            "tokens": system_tool_tokens,
            "percent": _pct(system_tool_tokens),
            "color": "#9ca3af",
        },
        {
            "key": "system_prompt",
            "label": "System prompt",
            "tokens": system_prompt_tokens,
            "percent": _pct(system_prompt_tokens),
            "color": "#9ca3af",
        },
        {
            "key": "mcp_instructions",
            "label": "MCP server instructions",
            "tokens": mcp_instructions_tokens,
            "percent": _pct(mcp_instructions_tokens),
            "color": "#9ca3af",
        },
        {
            "key": "memory_files",
            "label": "Memory files",
            "tokens": memory_tokens,
            "percent": _pct(memory_tokens),
            "color": "#9ca3af",
        },
        {
            "key": "since_last_update",
            "label": "Since last update",
            "tokens": since_last,
            "percent": _pct(since_last),
            "color": "#9ca3af",
        },
        {
            "key": "autocompact_buffer",
            "label": "Autocompact buffer",
            "tokens": autocompact_buffer,
            "percent": _pct(autocompact_buffer),
            "color": "#6b7280",
        },
        {
            "key": "free_space",
            "label": "Free space",
            "tokens": free_space,
            "percent": _pct(free_space),
            "color": "#374151",
        },
    ]

    return {
        "total_tokens": total_tokens,
        "used_tokens": used_tokens,
        "used_percent": used_percent,
        "updated_at": int(now if now is not None else time.time()),
        "categories": categories,
        "counts": {
            "mcp_tools": len(mcp_tool_names),
            "system_tools": len(system_tool_names),
            "memory_files": len(memory_names),
            "messages": len(history),
        },
        "details": {
            "mcp_tools": sorted(mcp_tool_names)[:100],
            "memory_files": memory_names[:50],
        },
    }


def format_context_summary(snapshot: Mapping[str, Any] | dict[str, Any]) -> str:
    used = format_token_count(int(snapshot.get("used_tokens", 0)))
    total = format_token_count(int(snapshot.get("total_tokens", _DEFAULT_CONTEXT_TOKENS)))
    pct = snapshot.get("used_percent", 0)
    counts = snapshot.get("counts", {})
    return (
        f"Using {pct}% of context ({used} / {total} tokens) — "
        f"{counts.get('messages', 0)} messages, "
        f"{counts.get('mcp_tools', 0)} MCP tools, "
        f"{counts.get('memory_files', 0)} memory files."
    )
