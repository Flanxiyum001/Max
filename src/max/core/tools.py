"""Tool registry, permission policy, path sandbox and audit log.

The permission policy lives *outside* the model loop: the agent asks the
policy before dispatching any tool call, and the policy is driven by
configuration plus hard-coded default-deny categories (exec, net). The model
cannot widen its own permissions through a tool result.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ------------------------------------------------------------------ errors


class ToolError(Exception):
    """Recoverable tool failure; surfaced to the model as an ERROR result."""


# -------------------------------------------------------------- path guard

_PATH_ESCAPE = re.compile(r"(^|/)\.\.(/|$)")


def resolve_within(root: Path, user_path: str) -> Path:
    """Resolve ``user_path`` under ``root`` or raise ToolError.

    Rejects absolute paths, parent traversal and symlink escapes.
    """
    root = root.resolve()
    raw = str(user_path or "").strip()
    if not raw:
        raise ToolError("empty path")
    if raw.startswith("~") or _PATH_ESCAPE.search(raw.replace("\\", "/")):
        raise ToolError(f"path escapes sandbox: {user_path}")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ToolError(f"absolute paths are not allowed: {user_path}")
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ToolError(f"path escapes sandbox: {user_path}")
    return resolved


# ------------------------------------------------------------------ policy


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


DEFAULT_DENY_CATEGORIES = frozenset({"exec", "net"})
DEFAULT_ALLOWED_CATEGORIES = frozenset({"read", "write", "memory", "skill", "mcp", "meta"})


class PermissionPolicy:
    """Explicit deny > explicit allow > category default > allowlist mode."""

    def __init__(
        self,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        deny_categories: set[str] | None = None,
        restrictive: bool = False,
    ):
        self.allow = set(allow or [])
        self.deny = set(deny or [])
        self.deny_categories = set(deny_categories if deny_categories is not None else DEFAULT_DENY_CATEGORIES)
        self.restrictive = restrictive

    @classmethod
    def from_config(cls, config) -> "PermissionPolicy":
        return cls(allow=config.tools_allow, deny=config.tools_deny)

    def check(self, name: str, category: str) -> Decision:
        if name in self.deny:
            return Decision(False, f"tool '{name}' is explicitly denied by configuration")
        if name in self.allow:
            return Decision(True, "explicitly allowed by configuration")
        if category in self.deny_categories:
            return Decision(
                False, f"category '{category}' is denied by default (policy outside model control)"
            )
        if self.restrictive and name not in self.allow:
            return Decision(False, "restrictive mode: tool not in allowlist")
        if category not in DEFAULT_ALLOWED_CATEGORIES:
            return Decision(False, f"category '{category}' is not approved")
        return Decision(True, "allowed by category policy")


# ------------------------------------------------------------------ audit


class AuditLog:
    """Append-only JSONL audit trail (data/audit/audit.jsonl)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **detail: Any) -> dict:
        entry = {"ts": round(time.time(), 3), "event": event, **detail}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def events(self, event: str | None = None) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if event is None or entry.get("event") == event:
                out.append(entry)
        return out


# ------------------------------------------------------------------ tools


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    category: str
    handler: Callable[[dict, "ToolContext"], str]


@dataclass
class ToolContext:
    workspace_dir: Path
    read_roots: list[Path]
    memory: Any = None
    registry: Any = None
    extra: dict = field(default_factory=dict)


# Prompt-injection markers in *untrusted* tool output. These never grant
# permissions; they only cause the output to be flagged and audited.
INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+(instructions|prompts)"),
    re.compile(r"(?i)disregard\s+(your|the|all)\s+(system|initial)\s+prompt"),
    re.compile(r"(?i)system\s*:\s*you\s+are\s+now"),
    re.compile(r"(?i)override\s+your\s+(safety|system)\s+(rules|prompt)"),
    re.compile(r"(?i)\bnew\s+system\s+prompt\b"),
]


def find_injection_markers(text: str) -> list[str]:
    hits = []
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text or ""):
            hits.append(pattern.pattern)
    return hits


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    def schemas(self) -> list[dict]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in self.specs()
        ]


class ToolExecutor:
    """Dispatch through the policy; every outcome is audited."""

    MAX_OUTPUT_CHARS = 8000

    def __init__(self, registry: ToolRegistry, policy: PermissionPolicy, audit: AuditLog, context: ToolContext):
        self.registry = registry
        self.policy = policy
        self.audit = audit
        self.context = context

    def dispatch(self, name: str, arguments: dict) -> tuple[str, dict]:
        arguments = arguments if isinstance(arguments, dict) else {}
        spec = self.registry.get(name)
        if spec is None:
            self.audit.record("tool_rejected", tool=name, reason="unknown_tool")
            return f"ERROR: unknown tool '{name}'", {"allowed": False, "reason": "unknown_tool"}

        decision = self.policy.check(spec.name, spec.category)
        if not decision.allowed:
            self.audit.record("tool_rejected", tool=name, reason=decision.reason, arguments=_safe_args(arguments))
            return (
                f"REJECTED: tool '{name}' denied: {decision.reason}",
                {"allowed": False, "reason": decision.reason},
            )

        self.audit.record("tool_allowed", tool=name, arguments=_safe_args(arguments))
        try:
            output = spec.handler(arguments, self.context)
        except ToolError as exc:
            return f"ERROR: {exc}", {"allowed": True, "reason": "tool_error"}
        except Exception as exc:  # noqa: BLE001 - a tool bug must not crash the agent
            self.audit.record("tool_crashed", tool=name, error=exc.__class__.__name__)
            return f"ERROR: tool failed ({exc.__class__.__name__})", {"allowed": True, "reason": "crash"}

        output = str(output)
        flagged = find_injection_markers(output)
        if flagged:
            self.audit.record("injection_flagged", tool=name, patterns=flagged)
            output = "[max: untrusted content flagged — possible prompt injection detected]\n" + output
        if len(output) > self.MAX_OUTPUT_CHARS:
            output = output[: self.MAX_OUTPUT_CHARS] + "\n...[truncated]"
        return output, {"allowed": True, "reason": decision.reason, "flagged": bool(flagged)}


def _safe_args(arguments: dict) -> dict:
    """Audit-friendly arguments: long values truncated, nothing secret-shaped logged fully."""
    safe = {}
    for key, value in arguments.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        safe[key] = text[:200] + ("…" if len(text) > 200 else "")
    return safe
