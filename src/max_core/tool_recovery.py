"""Per-turn, trusted tool failure diagnosis and replay guard.

Only fixed error codes enter guidance. Arguments are fingerprinted locally and
never shown to a model as a diagnosis or sent to a search provider.
"""

from __future__ import annotations

import hashlib
import json
import re

_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ADVICE = {
    "unknown_tool": "Check the advertised tool names and select an available tool.",
    "undeclared_tool": "Use only an advertised tool; requesting an undeclared tool will not work.",
    "missing_argument": "Inspect the tool schema and provide each required argument.",
    "unknown_argument": "Remove arguments not present in the tool schema.",
    "argument_type": "Match each argument to the type in the tool schema.",
    "arguments_not_object": "Supply a JSON object for tool arguments.",
    "owner_denied": "The owner refused this action. Do not attempt it again without a new request.",
    "tool_timeout": "The tool timed out. Check availability; avoid repeating the same request.",
    "network_error": "Check network or service availability before using a different approach.",
    "tool_failed": "The handler failed. Inspect safe local evidence or public documentation before changing approach.",
}


def safe_error_code(value: object) -> str:
    """Strip provider, OS and tool payloads from model-facing error guidance."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else "tool_failed"


class ToolFailureGuard:
    """Limit exact failures and all failures of one tool within one owner turn."""

    def __init__(self) -> None:
        self._exact: dict[tuple[str, str], int] = {}
        self._by_tool: dict[str, int] = {}
        self._denied: set[tuple[str, str]] = set()

    @staticmethod
    def signature(name: str, arguments: object) -> tuple[str, str]:
        try:
            encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=True).encode()
        except (TypeError, ValueError, RecursionError):
            encoded = b"<invalid arguments>"
        return name, hashlib.sha256(encoded).hexdigest()

    def blocked(self, name: str, arguments: object) -> bool:
        signature = self.signature(name, arguments)
        return (
            signature in self._denied
            or self._exact.get(signature, 0) >= 2
            or self._by_tool.get(name, 0) >= 4
        )

    def success(self, name: str, arguments: object) -> None:
        self._exact.pop(self.signature(name, arguments), None)
        self._by_tool.pop(name, None)

    def failed(
        self,
        name: str,
        arguments: object,
        code: object,
        *,
        denied: bool = False,
        can_search: bool = False,
    ) -> str:
        signature = self.signature(name, arguments)
        self._exact[signature] = self._exact.get(signature, 0) + 1
        self._by_tool[name] = self._by_tool.get(name, 0) + 1
        if denied:
            self._denied.add(signature)
        safe = safe_error_code(code)
        advice = _ADVICE.get(
            safe, "Inspect the tool schema or service state before changing approach."
        )
        search = (
            " If public documentation might help, use an available web.search tool "
            "with generic error terms only; never include private paths, arguments, or output."
            if can_search and safe in {"tool_failed", "network_error", "tool_timeout"}
            else ""
        )
        repeat = (
            " This exact call has failed twice; it will be blocked if proposed again."
            if self._exact[signature] >= 2
            else ""
        )
        return f"Error {safe}. {advice}{repeat}{search}"
