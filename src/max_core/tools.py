"""MAX tool registry: owner-defined tools with trusted-code permission tiers.

Model output only *names* a tool and supplies arguments. Whether a tool runs is
decided here and in ``tool_loop`` by trusted code, never by prompt text:

* ``READ_ONLY`` tools have no side effects and may run without a prompt.
* ``EFFECTFUL`` tools change something and require an explicit owner approval
  callback for each exact call. With no approver they are always denied.
"""

from __future__ import annotations

import ast
import datetime as _dt
import json
import math
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

_NAME = re.compile(r"[a-z][a-z0-9_]{0,30}(\.[a-z][a-z0-9_]{0,30}){0,2}")
MAX_RESULT_CHARS = 8_000


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    EFFECTFUL = "effectful"


class ToolImage(str):
    """Tool text output that also carries PNG images for a vision model.

    Behaves as the plain text everywhere else; the harness attaches ``images``
    to the next model message only for local vision models.
    """

    images: tuple[bytes, ...]

    def __new__(cls, text: str, images: tuple[bytes, ...]) -> ToolImage:
        obj = super().__new__(cls, text)
        obj.images = images
        return obj


class ToolError(Exception):
    """Fixed, display-safe failure code. Never wraps provider or OS messages."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ToolParam:
    name: str
    kind: type  # str, int, float or bool
    description: str
    required: bool = True
    max_length: int = 2_000


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    risk: ToolRisk
    params: tuple[ToolParam, ...]
    handler: Callable[[Mapping[str, Any]], str]
    argument_validator: Callable[[object], dict[str, Any]] | None = None
    input_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name) and not re.fullmatch(
            r"mcp\.github\.[a-z][a-z0-9_]{0,63}", self.name
        ):
            raise ValueError("invalid_tool_name")
        if not isinstance(self.risk, ToolRisk):
            raise ValueError("invalid_tool_risk")
        if len({p.name for p in self.params}) != len(self.params):
            raise ValueError("duplicate_tool_param")

    def validate(self, arguments: object) -> dict[str, Any]:
        """Strict argument check: no unknown keys, exact primitive types, bounded text."""
        if self.argument_validator is not None:
            return self.argument_validator(arguments)
        if not isinstance(arguments, dict):
            raise ToolError("arguments_not_object")
        known = {p.name: p for p in self.params}
        if any(key not in known for key in arguments):
            raise ToolError("unknown_argument")
        clean: dict[str, Any] = {}
        for param in self.params:
            if param.name not in arguments:
                if param.required:
                    raise ToolError("missing_argument")
                continue
            value = arguments[param.name]
            if param.kind is float and type(value) is int:
                try:
                    value = float(value)
                except OverflowError:
                    raise ToolError("argument_type") from None
            if type(value) is not param.kind:
                raise ToolError("argument_type")
            if isinstance(value, float) and not math.isfinite(value):
                raise ToolError("argument_type")
            if isinstance(value, str) and len(value) > param.max_length:
                raise ToolError("argument_too_long")
            clean[param.name] = value
        return clean

    def describe(self) -> dict[str, Any]:
        types = {str: "string", int: "integer", float: "number", bool: "boolean"}
        arguments: dict[str, Any] = {
            p.name: f"{types[p.kind]}{'' if p.required else ' (optional)'} - {p.description}"
            for p in self.params
        }
        if self.input_schema is not None:
            arguments = self.input_schema
        return {
            "name": self.name,
            "description": self.description,
            "needs_owner_approval": self.risk == ToolRisk.EFFECTFUL,
            "arguments": arguments,
        }


class ToolRegistry:
    def __init__(self, tools: tuple[Tool, ...] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError("duplicate_tool")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __iter__(self) -> Any:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def catalogue_json(self) -> str:
        return json.dumps([t.describe() for t in self._tools.values()], indent=1)


# ---------------------------------------------------------------------------
# Built-in tools


def _clock_now(_: Mapping[str, Any]) -> str:
    now = _dt.datetime.now().astimezone()
    return now.strftime("%A %Y-%m-%d %H:%M:%S %Z (UTC%z)")


_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_FUNCS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "abs": abs,
    "round": round,
}
_CONSTS = {"pi": math.pi, "e": math.e}


def safe_calculate(expression: str) -> float | int:
    """Arithmetic only: no names beyond a fixed whitelist, no attributes, bounded powers."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ToolError("invalid_expression") from None

    def ev(node: ast.AST, depth: int = 0) -> Any:
        if depth > 40:
            raise ToolError("expression_too_deep")
        if isinstance(node, ast.Expression):
            return ev(node.body, depth + 1)
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return node.value
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = ev(node.operand, depth + 1)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = ev(node.left, depth + 1), ev(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and (abs(right) > 1_000 or abs(left) > 1e12):
                raise ToolError("power_too_large")
            return _BIN_OPS[type(node.op)](left, right)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS
            and not node.keywords
            and 1 <= len(node.args) <= 2
        ):
            return _FUNCS[node.func.id](*(ev(a, depth + 1) for a in node.args))
        raise ToolError("unsupported_expression")

    try:
        result = ev(tree)
    except ToolError:
        raise
    except (ArithmeticError, ValueError, TypeError):
        raise ToolError("math_error") from None
    if isinstance(result, int) and abs(result) > 10**100:
        raise ToolError("result_too_large")
    return result  # type: ignore[no-any-return]


def _calculate(args: Mapping[str, Any]) -> str:
    return repr(safe_calculate(args["expression"]))


_SECRET_NAMES = re.compile(
    r"(^\.env|\.pem$|\.key$|^id_(rsa|ed25519|ecdsa)|credential|secret|password|token|\.kdbx$)",
    re.IGNORECASE,
)


class Workspace:
    """A single owner-chosen folder. Paths may never leave it (symlinks included)."""

    def __init__(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("workspace_not_directory")
        if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
            raise ValueError("workspace_too_broad")
        self.root = resolved

    def resolve(self, relative: str) -> Path:
        if "\x00" in relative or Path(relative).is_absolute():
            raise ToolError("path_not_allowed")
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError("path_outside_workspace")
        rel_parts = target.relative_to(self.root).parts
        if any(part.startswith(".") or _SECRET_NAMES.search(part) for part in rel_parts):
            raise ToolError("path_not_allowed")
        return target

    def list(self, args: Mapping[str, Any]) -> str:
        folder = self.resolve(args.get("path", "."))
        if not folder.is_dir():
            raise ToolError("not_a_directory")
        rows = []
        for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower())[:200]:
            if entry.name.startswith(".") or _SECRET_NAMES.search(entry.name):
                continue
            rows.append(entry.name + ("/" if entry.is_dir() else ""))
        return "\n".join(rows) or "(empty)"

    def read(self, args: Mapping[str, Any]) -> str:
        path = self.resolve(args["path"])
        if not path.is_file():
            raise ToolError("not_a_file")
        if path.stat().st_size > 256_000:
            raise ToolError("file_too_large")
        try:
            return path.read_text(encoding="utf-8")[:MAX_RESULT_CHARS]
        except UnicodeDecodeError:
            raise ToolError("not_text") from None

    def write_note(self, args: Mapping[str, Any]) -> str:
        name = args["name"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,60}", name):
            raise ToolError("invalid_note_name")
        notes = self.root / "max-notes"
        notes.mkdir(exist_ok=True)
        if notes.is_symlink() or not notes.resolve().is_relative_to(self.root):
            raise ToolError("path_outside_workspace")
        path = notes / f"{name}.md"
        try:
            with path.open("x", encoding="utf-8") as handle:  # never overwrite
                handle.write(args["content"])
        except FileExistsError:
            raise ToolError("note_exists") from None
        return f"saved max-notes/{name}.md"


def builtin_registry(workspace: Path | None = None) -> ToolRegistry:
    registry = ToolRegistry(
        (
            Tool(
                "clock.now",
                "Current local date, time and timezone of the owner's machine.",
                ToolRisk.READ_ONLY,
                (),
                _clock_now,
            ),
            Tool(
                "math.calculate",
                "Evaluate an arithmetic expression, e.g. '(3+4)*sqrt(2)'. "
                "Supports + - * / // % **, sqrt sin cos tan log log10 abs round, pi, e.",
                ToolRisk.READ_ONLY,
                (ToolParam("expression", str, "the expression", max_length=500),),
                _calculate,
            ),
        )
    )
    if workspace is not None:
        ws = Workspace(workspace)
        registry.register(
            Tool(
                "workspace.list",
                "List files in the owner's MAX workspace folder.",
                ToolRisk.READ_ONLY,
                (ToolParam("path", str, "relative folder, default '.'", required=False),),
                ws.list,
            )
        )
        registry.register(
            Tool(
                "workspace.read",
                "Read a UTF-8 text file from the owner's MAX workspace folder.",
                ToolRisk.READ_ONLY,
                (ToolParam("path", str, "relative file path"),),
                ws.read,
            )
        )
        registry.register(
            Tool(
                "notes.write",
                "Save a NEW markdown note in the workspace (never overwrites).",
                ToolRisk.EFFECTFUL,
                (
                    ToolParam("name", str, "note name, letters/digits/space/-/_", max_length=61),
                    ToolParam("content", str, "markdown text", max_length=20_000),
                ),
                ws.write_note,
            )
        )
    return registry
