"""Typed, allowlisted, bounded local tools and Trust-gated executor for MAX."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from max_core.assistant_chat import ToolExecutionSummary, ToolStatus
from max_core.bounded_commands import BoundedCommandError, run_bounded_command
from max_core.bounded_worker import WorkerError, execute_operation, parse_operation
from max_core.container_runner import run_python_sandboxed
from max_core.gateway.contracts import ToolDefinition, ToolProposal
from max_core.local_config import DesktopConfig
from max_core.native_desktop.backend import NativeDesktopCore, NativeDesktopError
from max_core.persistence import _is_expired
from max_core.store import StoreError, TaskStore
from max_core.trust import (
    ActorContext,
    RiskTier,
    ToolPolicy,
    ToolPolicyRegistry,
    TrustError,
    TrustService,
)

_OWNER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_UUID_LOWER_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NOTE_TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,79}$")

_MAX_PATH_ARG_CHARS = 512
_MAX_TASK_LIST_LIMIT = 50
_DEFAULT_TASK_LIST_LIMIT = 20
_MAX_LIST_FILES_LIMIT = 200
_DEFAULT_LIST_FILES_LIMIT = 100
_MAX_NOTES_LIST_LIMIT = 100
_DEFAULT_NOTES_LIST_LIMIT = 50
_MAX_READ_START_LINE = 100_000
_MAX_READ_LINES = 200
_MAX_READ_FILE_BYTES = 262_144
_MAX_READ_OUTPUT_BYTES = 16_384
_MAX_NOTE_CONTENT_BYTES = 32_768
_MAX_NOTE_CONTENT_LINES = 1_000
_MAX_DEV_ADD_ABS = 1_000_000
_ANCHOR_OBJECTIVE = "MAX Assistant local tool session"
_GIT_TIMEOUT_SECONDS = 30
_PYTEST_TIMEOUT_SECONDS = 300
_CODE_TIMEOUT_SECONDS = 120
_MAX_SCRIPT_CHARS = 32_000

_EXCLUDED_DIR_NAMES = frozenset(
    {".git", ".venv", "__pycache__", "secrets", "private", ".max-scratch"}
)
_EXCLUDED_FILE_NAMES = frozenset(
    {".env", ".server.log", "id_rsa", "id_ed25519", "credentials.json"}
)
_EXCLUDED_FILE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
)
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)
_ALLOWED_NOTE_EXTENSIONS = (".md", ".txt")


class LocalToolError(Exception):
    """Fixed safe error code; never embeds raw paths, file contents, or secrets."""

    def __init__(self, code: str, *, status: Literal["denied", "failed"] = "failed") -> None:
        self.code = code
        self.status: Literal["denied", "failed"] = status
        super().__init__(code)


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _has_disallowed_text_controls(value: str) -> bool:
    return any((ord(ch) < 0x20 and ch not in "\t\n\r") or ord(ch) == 0x7F for ch in value)


def _require_valid_utf8(value: str, code: str = "invalid_action_parameters") -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise LocalToolError(code, status="failed") from None


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str) or _has_control_chars(value):
        return False
    if _UUID_LOWER_RE.fullmatch(value) is None:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _is_excluded_rel_path(rel_parts: tuple[str, ...]) -> bool:
    for part in rel_parts:
        lower = part.lower()
        if lower in _EXCLUDED_DIR_NAMES:
            return True
        if lower == ".env" or lower.startswith(".env."):
            return True
        if lower in _EXCLUDED_FILE_NAMES:
            return True
        if any(lower.endswith(suffix) for suffix in _EXCLUDED_FILE_SUFFIXES):
            return True
    return False


def _is_reparse_point(path: Path) -> bool:
    return bool(path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()))


def _resolve_workspace_path(
    workspace_root: Path,
    raw_path: str,
    *,
    allow_root: bool,
) -> tuple[Path, Path, str]:
    if not isinstance(raw_path, str) or _has_control_chars(raw_path):
        raise LocalToolError("invalid_action_parameters", status="denied")
    _require_valid_utf8(raw_path, "invalid_action_parameters")
    if len(raw_path) > _MAX_PATH_ARG_CHARS:
        raise LocalToolError("invalid_action_parameters", status="failed")
    root_str = str(workspace_root)
    if _has_control_chars(root_str):
        raise LocalToolError("workspace_unavailable", status="failed")

    normalized = raw_path.replace("\\", "/")
    if normalized.strip() in {"", "."}:
        if not allow_root:
            raise LocalToolError("invalid_action_parameters", status="denied")
        try:
            root = workspace_root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise LocalToolError("workspace_unavailable", status="failed") from None
        if not root.is_dir():
            raise LocalToolError("not_a_directory", status="failed")
        return root, root, "."

    if (
        normalized.startswith("/")
        or normalized.startswith("~")
        or ":" in raw_path
        or os.path.splitdrive(raw_path)[0]
        or Path(raw_path).is_absolute()
        or PureWindowsPath(raw_path).is_absolute()
    ):
        raise LocalToolError("path_outside_root", status="denied")

    parts = [part for part in normalized.split("/") if part]
    if not parts:
        raise LocalToolError("invalid_action_parameters", status="denied")

    for part in parts:
        if part in {".", ".."}:
            raise LocalToolError("path_outside_root", status="denied")
        if part.endswith((".", " ")):
            raise LocalToolError("invalid_action_parameters", status="denied")
        stem = part.split(".", 1)[0].lower()
        if stem in _WINDOWS_RESERVED_STEMS:
            raise LocalToolError("invalid_action_parameters", status="denied")

    if _is_excluded_rel_path(tuple(parts)):
        raise LocalToolError("excluded_secret_path", status="denied")

    try:
        root = workspace_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise LocalToolError("workspace_unavailable", status="failed") from None
    if not root.is_dir():
        raise LocalToolError("not_a_directory", status="failed")

    cur = root
    for part in parts:
        cur = cur / part
        try:
            cur_resolved = cur.resolve(strict=True)
        except FileNotFoundError:
            code = "not_found" if allow_root else "file_not_found"
            raise LocalToolError(code, status="failed") from None
        except (OSError, RuntimeError, ValueError):
            raise LocalToolError("access_denied", status="denied") from None
        if _is_reparse_point(cur) and cur_resolved == cur:
            raise LocalToolError("symlink_not_allowed", status="denied")
        if not cur_resolved.is_relative_to(root):
            raise LocalToolError("path_outside_root", status="denied")
        cur_rel = cur_resolved.relative_to(root)
        if _is_excluded_rel_path(cur_rel.parts):
            raise LocalToolError("excluded_secret_path", status="denied")
        cur = cur_resolved

    resolved = cur
    resolved_rel = resolved.relative_to(root)
    return root, resolved, resolved_rel.as_posix() or "."


def _parse_note_title(raw_title: object) -> tuple[str, str]:
    if not isinstance(raw_title, str) or _has_control_chars(raw_title):
        raise LocalToolError("invalid_note_title", status="denied")
    _require_valid_utf8(raw_title, "invalid_note_title")
    stripped = raw_title.strip()
    if not stripped or len(stripped) > 84:
        raise LocalToolError("invalid_note_title", status="denied")
    if (
        "/" in stripped
        or "\\" in stripped
        or ".." in stripped
        or ":" in stripped
        or stripped.startswith(".")
        or stripped.endswith((".", " "))
    ):
        raise LocalToolError("invalid_note_title", status="denied")

    lower = stripped.lower()
    if lower.endswith(".md"):
        stem = stripped[:-3]
        ext = ".md"
    elif lower.endswith(".txt"):
        stem = stripped[:-4]
        ext = ".txt"
    elif "." in stripped:
        raise LocalToolError("invalid_note_title", status="denied")
    else:
        stem = stripped
        ext = ".md"

    if not stem or stem != stem.strip() or stem.endswith((".", " ")):
        raise LocalToolError("invalid_note_title", status="denied")
    if not _NOTE_TITLE_RE.fullmatch(stem):
        raise LocalToolError("invalid_note_title", status="denied")
    if stem.lower() in _WINDOWS_RESERVED_STEMS:
        raise LocalToolError("invalid_note_title", status="denied")
    filename = f"{stem}{ext}"
    if _is_excluded_rel_path((filename,)):
        raise LocalToolError("excluded_secret_path", status="denied")
    return stem, filename


def _resolve_note_target(
    notes_dir: Path,
    filename: str,
    *,
    create_dir: bool,
) -> tuple[Path, Path]:
    if _has_control_chars(str(notes_dir)):
        raise LocalToolError("notes_dir_unavailable", status="failed")
    if _is_reparse_point(notes_dir):
        raise LocalToolError("symlink_not_allowed", status="denied")
    if create_dir:
        try:
            notes_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise LocalToolError("notes_dir_unavailable", status="failed") from None
    try:
        if _is_reparse_point(notes_dir):
            raise LocalToolError("symlink_not_allowed", status="denied")
        resolved_root = notes_dir.resolve(strict=True)
    except LocalToolError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise LocalToolError("notes_dir_unavailable", status="failed") from None
    if not resolved_root.is_dir():
        raise LocalToolError("notes_dir_unavailable", status="failed")

    target = resolved_root / filename
    if _is_reparse_point(target):
        raise LocalToolError("symlink_not_allowed", status="denied")
    try:
        resolved_target = target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise LocalToolError("path_outside_root", status="denied") from None
    if resolved_target.parent != resolved_root or not resolved_target.is_relative_to(resolved_root):
        raise LocalToolError("path_outside_root", status="denied")
    return resolved_root, resolved_target


ToolValidator = Callable[[dict[str, Any]], dict[str, Any]]
ToolHandler = Callable[[dict[str, Any], str | None, str | None], dict[str, object]]


@dataclass(frozen=True, slots=True)
class LocalToolSpec:
    """Host-defined tool specification with strict schema, risk tier, and bounded handler."""

    name: str
    description: str
    parameters: dict[str, Any]
    risk_tier: RiskTier
    requires_approval: bool
    validate: ToolValidator
    handler: ToolHandler

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


def _validate_no_args(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise LocalToolError("invalid_action_parameters", status="failed")
    return {}


def _validate_dev_add(arguments: dict[str, Any]) -> dict[str, Any]:
    if set(arguments) != {"a", "b"}:
        raise LocalToolError("invalid_action_parameters", status="failed")
    a = arguments["a"]
    b = arguments["b"]
    if (
        type(a) is not int
        or type(b) is not int
        or not -_MAX_DEV_ADD_ABS <= a <= _MAX_DEV_ADD_ABS
        or not -_MAX_DEV_ADD_ABS <= b <= _MAX_DEV_ADD_ABS
    ):
        raise LocalToolError("invalid_action_parameters", status="failed")
    payload = json.dumps({"op": "dev.add", "params": {"a": a, "b": b}}, separators=(",", ":"))
    try:
        parse_operation(payload)
    except WorkerError:
        raise LocalToolError("invalid_action_parameters", status="failed") from None
    return {"a": a, "b": b}


def _validate_task_status(arguments: dict[str, Any]) -> dict[str, Any]:
    if set(arguments) != {"task_id"} or not _is_canonical_uuid(arguments["task_id"]):
        raise LocalToolError("invalid_action_parameters", status="failed")
    return {"task_id": arguments["task_id"]}


def _validate_task_list(arguments: dict[str, Any]) -> dict[str, Any]:
    if not set(arguments) <= {"limit"}:
        raise LocalToolError("invalid_action_parameters", status="failed")
    limit = arguments.get("limit", _DEFAULT_TASK_LIST_LIMIT)
    if type(limit) is not int or not 1 <= limit <= _MAX_TASK_LIST_LIMIT:
        raise LocalToolError("invalid_action_parameters", status="failed")
    return {"limit": limit}


def _validate_notes_list(arguments: dict[str, Any]) -> dict[str, Any]:
    if not set(arguments) <= {"limit"}:
        raise LocalToolError("invalid_action_parameters", status="failed")
    limit = arguments.get("limit", _DEFAULT_NOTES_LIST_LIMIT)
    if type(limit) is not int or not 1 <= limit <= _MAX_NOTES_LIST_LIMIT:
        raise LocalToolError("invalid_action_parameters", status="failed")
    return {"limit": limit}


def _validate_task_create(arguments: dict[str, Any]) -> dict[str, Any]:
    if set(arguments) != {"objective"}:
        raise LocalToolError("invalid_action_parameters", status="failed")
    raw_obj = arguments["objective"]
    if not isinstance(raw_obj, str) or _has_control_chars(raw_obj):
        raise LocalToolError("invalid_action_parameters", status="failed")
    _require_valid_utf8(raw_obj, "invalid_action_parameters")
    cleaned = raw_obj.strip()
    if not 1 <= len(cleaned) <= 500:
        raise LocalToolError("invalid_action_parameters", status="failed")
    return {"objective": cleaned}


def _validate_notes_save(arguments: dict[str, Any]) -> dict[str, Any]:
    if not {"title", "content"} <= set(arguments) or not set(arguments) <= {
        "title",
        "content",
        "overwrite",
    }:
        raise LocalToolError("invalid_action_parameters", status="failed")
    stem, filename = _parse_note_title(arguments["title"])
    content = arguments["content"]
    if not isinstance(content, str) or _has_disallowed_text_controls(content):
        raise LocalToolError("invalid_action_parameters", status="failed")
    _require_valid_utf8(content, "invalid_action_parameters")
    if not content.strip():
        raise LocalToolError("invalid_action_parameters", status="failed")
    if (
        len(content.encode("utf-8")) > _MAX_NOTE_CONTENT_BYTES
        or len(content.splitlines()) > _MAX_NOTE_CONTENT_LINES
    ):
        raise LocalToolError("note_content_too_large", status="failed")
    overwrite = arguments.get("overwrite", False)
    if type(overwrite) is not bool:
        raise LocalToolError("invalid_action_parameters", status="failed")
    canonical_title = filename if filename.lower().endswith(".txt") else stem
    return {"title": canonical_title, "content": content, "overwrite": overwrite}


def _run_bounded(
    workspace_root: Path, argv: Sequence[str], timeout_seconds: int
) -> dict[str, object]:
    try:
        return run_bounded_command(workspace_root, argv, timeout_seconds)
    except BoundedCommandError as error:
        raise LocalToolError(error.code, status="failed") from None


def _validate_execute_python(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"script"}:
        raise LocalToolError("invalid_action_parameters", status="denied")
    raw = arguments["script"]
    if not isinstance(raw, str) or not 1 <= len(raw) <= _MAX_SCRIPT_CHARS:
        raise LocalToolError("invalid_action_parameters", status="denied")
    if "\x00" in raw:
        raise LocalToolError("invalid_action_parameters", status="denied")
    _require_valid_utf8(raw, "invalid_action_parameters")
    return {"script": raw}


class LocalToolRegistry:
    """Fixed host registry of 14 bounded, deterministic local tools."""

    def __init__(
        self,
        config: DesktopConfig | None = None,
        *,
        core: NativeDesktopCore | None = None,
        task_store: TaskStore | None = None,
        workspace_root: Path | None = None,
        notes_dir: Path | None = None,
        owner_id: str = "owner",
    ) -> None:
        if not isinstance(owner_id, str) or _OWNER_RE.fullmatch(owner_id) is None:
            raise ValueError("invalid_owner_id")
        resolved_config = (
            config if config is not None else (core.config if core is not None else None)
        )
        self._config = resolved_config
        self._core = core
        self._owner_id = owner_id
        self._workspace_root = (
            workspace_root
            if workspace_root is not None
            else (
                resolved_config.workspace_root
                if resolved_config is not None
                else Path.cwd().resolve()
            )
        )
        self._notes_dir = (
            notes_dir
            if notes_dir is not None
            else (
                resolved_config.notes_dir
                if resolved_config is not None
                else (Path.cwd().resolve() / "notes")
            )
        )
        if task_store is not None:
            self._task_store: TaskStore | None = task_store
        elif core is not None and core.engine is not None:
            self._task_store = TaskStore(core.engine)
        else:
            self._task_store = None

        specs = (
            LocalToolSpec(
                name="dev.health",
                description="Run a deterministic bounded developer health check.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                risk_tier=RiskTier.R0,
                requires_approval=False,
                validate=_validate_no_args,
                handler=self._handle_dev_health,
            ),
            LocalToolSpec(
                name="dev.add",
                description="Add two bounded integers between -1,000,000 and 1,000,000.",
                parameters={
                    "type": "object",
                    "properties": {
                        "a": {
                            "type": "integer",
                            "minimum": -_MAX_DEV_ADD_ABS,
                            "maximum": _MAX_DEV_ADD_ABS,
                        },
                        "b": {
                            "type": "integer",
                            "minimum": -_MAX_DEV_ADD_ABS,
                            "maximum": _MAX_DEV_ADD_ABS,
                        },
                    },
                    "required": ["a", "b"],
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R0,
                requires_approval=False,
                validate=_validate_dev_add,
                handler=self._handle_dev_add,
            ),
            LocalToolSpec(
                name="system.status",
                description="Inspect local storage, provider configuration, and task counts.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                risk_tier=RiskTier.R0,
                requires_approval=False,
                validate=_validate_no_args,
                handler=self._handle_system_status,
            ),
            LocalToolSpec(
                name="task.status",
                description="Inspect safe status metadata for a single task by UUID.",
                parameters={
                    "type": "object",
                    "properties": {"task_id": {"type": "string", "minLength": 36, "maxLength": 36}},
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R0,
                requires_approval=False,
                validate=_validate_task_status,
                handler=self._handle_task_status,
            ),
            LocalToolSpec(
                name="task.list",
                description="List recent local tasks from the MAX task journal.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _MAX_TASK_LIST_LIMIT,
                        }
                    },
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R0,
                requires_approval=False,
                validate=_validate_task_list,
                handler=self._handle_task_list,
            ),
            LocalToolSpec(
                name="workspace.list_files",
                description="List non-sensitive files under the configured workspace root.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "maxLength": _MAX_PATH_ARG_CHARS},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _MAX_LIST_FILES_LIMIT,
                        },
                    },
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R0,
                requires_approval=False,
                validate=self._validate_workspace_list_files,
                handler=self._handle_workspace_list_files,
            ),
            LocalToolSpec(
                name="workspace.read_file",
                description="Read bounded UTF-8 text lines from a non-sensitive workspace file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_PATH_ARG_CHARS,
                        },
                        "start_line": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _MAX_READ_START_LINE,
                        },
                        "max_lines": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _MAX_READ_LINES,
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R0,
                requires_approval=False,
                validate=self._validate_workspace_read_file,
                handler=self._handle_workspace_read_file,
            ),
            LocalToolSpec(
                name="notes.list",
                description="List saved personal markdown/text notes in the notes directory.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _MAX_NOTES_LIST_LIMIT,
                        }
                    },
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R0,
                requires_approval=False,
                validate=_validate_notes_list,
                handler=self._handle_notes_list,
            ),
            LocalToolSpec(
                name="task.create",
                description="Create a queued task in the MAX task journal (requires Trust approval).",
                parameters={
                    "type": "object",
                    "properties": {
                        "objective": {"type": "string", "minLength": 1, "maxLength": 500}
                    },
                    "required": ["objective"],
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R1,
                requires_approval=True,
                validate=_validate_task_create,
                handler=self._handle_task_create,
            ),
            LocalToolSpec(
                name="notes.save",
                description="Save a personal note in the notes directory (requires Trust approval).",
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 84},
                        "content": {"type": "string", "minLength": 1},
                        "overwrite": {"type": "boolean"},
                    },
                    "required": ["title", "content"],
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R2,
                requires_approval=True,
                validate=_validate_notes_save,
                handler=self._handle_notes_save,
            ),
            LocalToolSpec(
                name="git.status",
                description="Show bounded working-tree status of the MAX workspace (requires Trust approval).",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                risk_tier=RiskTier.R1,
                requires_approval=True,
                validate=_validate_no_args,
                handler=self._handle_git_status,
            ),
            LocalToolSpec(
                name="git.diff",
                description="Show a bounded unified diff of uncommitted workspace changes (requires Trust approval).",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": _MAX_PATH_ARG_CHARS}
                    },
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R1,
                requires_approval=True,
                validate=self._validate_git_diff,
                handler=self._handle_git_diff,
            ),
            LocalToolSpec(
                name="code.execute_python",
                description=(
                    "Run a Python script in an ephemeral, network-disabled, read-only "
                    "container sandbox with a scratch /work directory (requires Trust approval)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "script": {"type": "string", "minLength": 1, "maxLength": _MAX_SCRIPT_CHARS}
                    },
                    "required": ["script"],
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R2,
                requires_approval=True,
                validate=_validate_execute_python,
                handler=self._handle_execute_python,
            ),
            LocalToolSpec(
                name="test.run_pytest",
                description="Run bounded pytest inside the MAX workspace (executes owner-authored tests; requires Trust approval).",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": _MAX_PATH_ARG_CHARS}
                    },
                    "additionalProperties": False,
                },
                risk_tier=RiskTier.R1,
                requires_approval=True,
                validate=self._validate_run_pytest,
                handler=self._handle_test_run_pytest,
            ),
        )
        self._specs: MappingProxyType[str, LocalToolSpec] = MappingProxyType(
            {spec.name: spec for spec in specs}
        )

    def with_task_store(self, task_store: TaskStore) -> LocalToolRegistry:
        return LocalToolRegistry(
            config=self._config,
            core=self._core,
            task_store=task_store,
            workspace_root=self._workspace_root,
            notes_dir=self._notes_dir,
            owner_id=self._owner_id,
        )

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(spec.to_definition() for spec in self._specs.values())

    def get_spec(self, name: str) -> LocalToolSpec | None:
        if not isinstance(name, str):
            return None
        return self._specs.get(name)

    def get(self, name: str) -> LocalToolSpec | None:
        return self.get_spec(name)

    def risk_tier(self, name: str) -> RiskTier:
        spec = self.get_spec(name)
        if spec is None:
            raise LocalToolError("capability_denied", status="denied")
        return spec.risk_tier

    def policy_registry(self) -> ToolPolicyRegistry:
        def _make_validator(
            spec: LocalToolSpec,
        ) -> Callable[[ActorContext, dict[str, Any]], dict[str, Any]]:
            def _validate_for_trust(_ctx: ActorContext, args: dict[str, Any]) -> dict[str, Any]:
                try:
                    return spec.validate(args)
                except LocalToolError as exc:
                    raise TrustError(exc.code) from None

            return _validate_for_trust

        return ToolPolicyRegistry(
            tuple(
                ToolPolicy(
                    name=spec.name,
                    risk=spec.risk_tier,
                    validate_arguments=_make_validator(spec),
                )
                for spec in self._specs.values()
            )
        )

    def validate(self, name: str, arguments: object) -> dict[str, Any]:
        spec = self.get_spec(name)
        if spec is None:
            raise LocalToolError("capability_denied", status="denied")
        if not isinstance(arguments, dict):
            raise LocalToolError("invalid_action_parameters", status="failed")
        return spec.validate(dict(arguments))

    def execute(
        self,
        name: str,
        arguments: object,
        *,
        call_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, object]:
        normalized = self.validate(name, arguments)
        spec = self._specs[name]
        return spec.handler(normalized, call_id, approval_id)

    def _validate_workspace_list_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not set(arguments) <= {"path", "limit"}:
            raise LocalToolError("invalid_action_parameters", status="failed")
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise LocalToolError("invalid_action_parameters", status="failed")
        limit = arguments.get("limit", _DEFAULT_LIST_FILES_LIMIT)
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST_FILES_LIMIT:
            raise LocalToolError("invalid_action_parameters", status="failed")
        _root, resolved, rel_posix = _resolve_workspace_path(
            self._workspace_root, raw_path, allow_root=True
        )
        if not resolved.is_dir():
            raise LocalToolError("not_a_directory", status="failed")
        return {"path": rel_posix, "limit": limit}

    def _validate_workspace_read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if "path" not in arguments or not set(arguments) <= {"path", "start_line", "max_lines"}:
            raise LocalToolError("invalid_action_parameters", status="failed")
        raw_path = arguments["path"]
        if not isinstance(raw_path, str):
            raise LocalToolError("invalid_action_parameters", status="failed")
        start_line = arguments.get("start_line", 1)
        max_lines = arguments.get("max_lines", _MAX_READ_LINES)
        if (
            type(start_line) is not int
            or type(max_lines) is not int
            or not 1 <= start_line <= _MAX_READ_START_LINE
            or not 1 <= max_lines <= _MAX_READ_LINES
        ):
            raise LocalToolError("invalid_action_parameters", status="failed")
        _root, resolved, rel_posix = _resolve_workspace_path(
            self._workspace_root, raw_path, allow_root=False
        )
        if not resolved.is_file():
            raise LocalToolError("not_a_file", status="failed")
        return {"path": rel_posix, "start_line": start_line, "max_lines": max_lines}

    def _handle_dev_health(
        self,
        _args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        op = parse_operation('{"op":"dev.health","params":{}}')
        raw = execute_operation(op)
        parsed = json.loads(raw.decode("ascii"))
        return dict(parsed)

    def _handle_dev_add(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        payload = json.dumps(
            {"op": "dev.add", "params": {"a": args["a"], "b": args["b"]}},
            separators=(",", ":"),
        )
        op = parse_operation(payload)
        raw = execute_operation(op)
        parsed = json.loads(raw.decode("ascii"))
        return dict(parsed)

    def _handle_system_status(
        self,
        _args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        core_status = "Core offline"
        storage_status = "Storage unconfigured"
        task_store_ready = False
        task_count = 0
        pending_approvals = 0

        if self._core is not None:
            try:
                snap = self._core.snapshot()
            except NativeDesktopError as exc:
                raise LocalToolError(exc.code, status="failed") from None
            core_status = snap.core_status
            storage_status = snap.storage_status
            task_store_ready = "ready" in storage_status.lower()
            task_count = len(snap.tasks)
            pending_approvals = len(snap.approvals)
        elif self._task_store is not None:
            core_status = "Core online (in-process)"
            try:
                with self._task_store.engine.connect() as conn:
                    conn.execute(text("SELECT id FROM max_tasks LIMIT 1"))
                    conn.execute(text("SELECT id FROM max_approval_grants LIMIT 1"))
                    conn.execute(text("SELECT id FROM max_trust_audit LIMIT 1"))
                if self._config is not None and not self._config.is_sqlite_local:
                    storage_status = (
                        "Local PostgreSQL ready"
                        if self._task_store.ready()
                        else "Database migrations incomplete"
                    )
                    task_store_ready = self._task_store.ready()
                else:
                    storage_status = "Local SQLite ready"
                    task_store_ready = True
                task_count = len(self._task_store.list_recent(self._owner_id, limit=50))
                pending_approvals = len(
                    TrustService(self._task_store.engine).list_pending(self._owner_id)
                )
            except SQLAlchemyError:
                raise LocalToolError("task_store_not_ready", status="failed") from None

        active_provider_cfg = self._config.active_provider if self._config is not None else None
        provider_configured = (
            active_provider_cfg is not None
            and isinstance(active_provider_cfg.api_key, str)
            and bool(active_provider_cfg.api_key.strip())
        )
        workspace_exists = (
            not _has_control_chars(str(self._workspace_root)) and self._workspace_root.is_dir()
        )
        notes_dir_exists = not _has_control_chars(str(self._notes_dir)) and self._notes_dir.is_dir()

        return {
            "assistant": "MAX",
            "core_status": core_status,
            "storage_status": storage_status,
            "task_store_ready": task_store_ready,
            "task_count": task_count,
            "pending_approvals": pending_approvals,
            "provider_configured": provider_configured,
            "active_provider": active_provider_cfg.provider if active_provider_cfg else None,
            "active_model": active_provider_cfg.model_id if active_provider_cfg else None,
            "workspace_exists": workspace_exists,
            "notes_dir_exists": notes_dir_exists,
            "voice_locale": self._config.voice_locale if self._config is not None else "en-GB",
            "tts_enabled_default": (
                self._config.tts_enabled_default if self._config is not None else False
            ),
            "registered_tools_count": len(self._specs),
        }

    def _handle_task_status(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        if self._task_store is None:
            raise LocalToolError("task_store_not_ready", status="failed")
        try:
            task = self._task_store.get(self._owner_id, args["task_id"])
        except StoreError as exc:
            if exc.status_code == 404:
                raise LocalToolError("task_not_found", status="failed") from None
            raise LocalToolError("task_store_not_ready", status="failed") from None
        return {
            "task_id": task.id,
            "state": task.state,
            "revision": task.revision,
            "updated_at": task.updated_at.isoformat(),
            "last_checkpoint_id": task.last_checkpoint_id,
        }

    def _handle_task_list(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        limit: int = args["limit"]
        if self._task_store is not None:
            tasks = self._task_store.list_recent(self._owner_id, limit=limit)
            items: list[dict[str, object]] = [
                {
                    "id": item.id,
                    "task_id": item.id,
                    "objective": item.objective,
                    "state": item.state,
                    "revision": item.revision,
                    "created_at": item.created_at.isoformat(),
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in tasks
            ]
            return {"count": len(items), "tasks": items}
        if self._core is not None:
            try:
                snap = self._core.snapshot()
            except NativeDesktopError as exc:
                raise LocalToolError(exc.code, status="failed") from None
            sliced = list(snap.tasks[:limit])
            items = [
                {
                    "id": item.get("id"),
                    "task_id": item.get("id"),
                    "objective": item.get("objective"),
                    "state": item.get("state"),
                    "revision": item.get("revision"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
                for item in sliced
            ]
            return {"count": len(items), "tasks": items}
        raise LocalToolError("task_store_not_ready", status="failed")

    def _handle_workspace_list_files(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        root, target_dir, rel_posix = _resolve_workspace_path(
            self._workspace_root, args["path"], allow_root=True
        )
        if not target_dir.is_dir():
            raise LocalToolError("not_a_directory", status="failed")
        limit: int = args["limit"]

        collected: list[dict[str, object]] = []
        truncated = False
        visited_dirs: set[Path] = set()
        stack: list[Path] = [target_dir]
        while stack and not truncated:
            current = stack.pop()
            if current in visited_dirs:
                continue
            visited_dirs.add(current)
            try:
                children = sorted(current.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            dirs_to_push: list[Path] = []
            for entry in children:
                try:
                    rel_parts = entry.relative_to(root).parts
                except ValueError:
                    continue
                if _is_excluded_rel_path(rel_parts):
                    continue
                try:
                    resolved_entry = entry.resolve(strict=True)
                except (OSError, RuntimeError, ValueError):
                    continue
                if _is_reparse_point(entry) and resolved_entry == entry:
                    continue
                if not resolved_entry.is_relative_to(root):
                    continue
                try:
                    resolved_rel_parts = resolved_entry.relative_to(root).parts
                except ValueError:
                    continue
                if _is_excluded_rel_path(resolved_rel_parts):
                    continue

                if resolved_entry.is_dir():
                    dirs_to_push.append(resolved_entry)
                elif resolved_entry.is_file():
                    if len(collected) >= limit:
                        truncated = True
                        break
                    try:
                        size_bytes = resolved_entry.stat().st_size
                    except OSError:
                        continue
                    collected.append(
                        {
                            "path": entry.relative_to(root).as_posix(),
                            "kind": "file",
                            "size_bytes": size_bytes,
                        }
                    )
            for subdir in reversed(dirs_to_push):
                stack.append(subdir)

        collected.sort(key=lambda item: str(item["path"]))
        return {
            "path": rel_posix,
            "count": len(collected),
            "truncated": truncated,
            "files": [item["path"] for item in collected],
            "entries": collected,
        }

    def _handle_workspace_read_file(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        _root, resolved, rel_posix = _resolve_workspace_path(
            self._workspace_root, args["path"], allow_root=False
        )
        if not resolved.is_file():
            raise LocalToolError("not_a_file", status="failed")
        try:
            size_bytes = resolved.stat().st_size
        except OSError:
            raise LocalToolError("access_denied", status="denied") from None
        if size_bytes > _MAX_READ_FILE_BYTES:
            raise LocalToolError("file_too_large", status="failed")

        try:
            raw_bytes = resolved.read_bytes()
        except OSError:
            raise LocalToolError("access_denied", status="denied") from None
        if len(raw_bytes) > _MAX_READ_FILE_BYTES:
            raise LocalToolError("file_too_large", status="failed")
        if b"\x00" in raw_bytes:
            raise LocalToolError("binary_or_non_utf8_file", status="failed")
        try:
            text_content = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise LocalToolError("binary_or_non_utf8_file", status="failed") from None

        all_lines = text_content.splitlines()
        total_lines = len(all_lines)
        start_line: int = args["start_line"]
        max_lines: int = args["max_lines"]
        start_idx = start_line - 1
        selected = all_lines[start_idx : start_idx + max_lines]

        kept_lines: list[str] = []
        used_bytes = 0
        truncated = start_idx + max_lines < total_lines
        for line in selected:
            line_bytes = len((line + "\n").encode("utf-8"))
            if kept_lines and used_bytes + line_bytes > _MAX_READ_OUTPUT_BYTES:
                truncated = True
                break
            if not kept_lines and line_bytes > _MAX_READ_OUTPUT_BYTES:
                encoded = line.encode("utf-8")[:_MAX_READ_OUTPUT_BYTES].decode(
                    "utf-8", errors="ignore"
                )
                kept_lines.append(encoded)
                used_bytes = len(encoded.encode("utf-8"))
                truncated = True
                break
            kept_lines.append(line)
            used_bytes += line_bytes

        joined = "\n".join(kept_lines)
        return {
            "path": rel_posix,
            "start_line": start_line,
            "returned_lines": len(kept_lines),
            "total_lines": total_lines,
            "bytes": len(joined.encode("utf-8")),
            "truncated": truncated,
            "content": joined,
        }

    def _handle_notes_list(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        if _has_control_chars(str(self._notes_dir)):
            raise LocalToolError("notes_dir_unavailable", status="failed")
        if _is_reparse_point(self._notes_dir):
            raise LocalToolError("symlink_not_allowed", status="denied")
        if not self._notes_dir.exists():
            return {"count": 0, "truncated": False, "notes": []}
        try:
            resolved_root = self._notes_dir.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise LocalToolError("notes_dir_unavailable", status="failed") from None
        if not resolved_root.is_dir():
            raise LocalToolError("notes_dir_unavailable", status="failed")

        limit: int = args["limit"]
        notes: list[dict[str, object]] = []
        truncated = False
        try:
            entries = sorted(resolved_root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            raise LocalToolError("notes_dir_unavailable", status="failed") from None

        for entry in entries:
            if _is_reparse_point(entry):
                continue
            try:
                resolved_entry = entry.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if (
                resolved_entry.parent != resolved_root
                or not resolved_entry.is_relative_to(resolved_root)
                or not resolved_entry.is_file()
            ):
                continue
            if _is_excluded_rel_path((entry.name,)):
                continue
            if not entry.name.lower().endswith(_ALLOWED_NOTE_EXTENSIONS):
                continue
            try:
                stem, _ = _parse_note_title(entry.name)
            except LocalToolError:
                continue
            if len(notes) >= limit:
                truncated = True
                break
            try:
                size_bytes = entry.stat().st_size
            except OSError:
                continue
            notes.append(
                {
                    "title": stem,
                    "filename": entry.name,
                    "size_bytes": size_bytes,
                    "bytes": size_bytes,
                }
            )
        return {"count": len(notes), "truncated": truncated, "notes": notes}

    def _handle_task_create(
        self,
        args: dict[str, Any],
        call_id: str | None,
        approval_id: str | None,
    ) -> dict[str, object]:
        if self._task_store is None:
            raise LocalToolError("task_store_not_ready", status="failed")
        if approval_id is not None:
            idempotency_key = f"trust-{approval_id}"
        else:
            digest = hashlib.sha256((call_id or uuid4().hex).encode("utf-8")).hexdigest()[:32]
            idempotency_key = f"tool-{digest}"
        try:
            task, created = self._task_store.create(
                self._owner_id, args["objective"], idempotency_key
            )
        except StoreError:
            raise LocalToolError("task_create_failed", status="failed") from None
        return {
            "task_id": task.id,
            "objective": task.objective,
            "state": task.state,
            "revision": task.revision,
            "created": created,
        }

    def _handle_notes_save(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        stem, filename = _parse_note_title(args["title"])
        _resolved_root, target = _resolve_note_target(self._notes_dir, filename, create_dir=True)
        existed = target.exists()
        if existed and not args["overwrite"]:
            raise LocalToolError("note_already_exists", status="failed")
        encoded = args["content"].encode("utf-8")
        try:
            target.write_bytes(encoded)
        except OSError:
            raise LocalToolError("note_write_failed", status="failed") from None
        return {
            "title": stem,
            "filename": filename,
            "bytes": len(encoded),
            "bytes_written": len(encoded),
            "overwritten": existed,
        }

    def _resolve_dev_path_argument(self, raw_path: object) -> str | None:
        if raw_path is None:
            return None
        if not isinstance(raw_path, str):
            raise LocalToolError("invalid_action_parameters", status="denied")
        normalized = raw_path.replace("\\", "/")
        if normalized.split("/", 1)[0].startswith("-"):
            raise LocalToolError("invalid_action_parameters", status="denied")
        _root, resolved, rel_posix = _resolve_workspace_path(
            self._workspace_root, raw_path, allow_root=True
        )
        if rel_posix.split("/", 1)[0].startswith("-"):
            raise LocalToolError("invalid_action_parameters", status="denied")
        return rel_posix

    def _validate_git_diff(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict) or not set(arguments) <= {"path"}:
            raise LocalToolError("invalid_action_parameters", status="denied")
        rel_posix = self._resolve_dev_path_argument(arguments.get("path"))
        return {} if rel_posix is None else {"path": rel_posix}

    def _validate_run_pytest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict) or not set(arguments) <= {"path"}:
            raise LocalToolError("invalid_action_parameters", status="denied")
        rel_posix = self._resolve_dev_path_argument(arguments.get("path"))
        return {} if rel_posix is None else {"path": rel_posix}

    def _handle_git_status(
        self,
        _args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        result = _run_bounded(
            self._workspace_root,
            ("git", "status", "--porcelain=v1"),
            _GIT_TIMEOUT_SECONDS,
        )
        result["tool"] = "git.status"
        return result

    def _handle_git_diff(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        argv: tuple[str, ...] = ("git", "--no-pager", "diff", "--no-color", "--unified=3")
        rel_path = args.get("path")
        if isinstance(rel_path, str):
            argv = argv + ("--", rel_path)
        result = _run_bounded(self._workspace_root, argv, _GIT_TIMEOUT_SECONDS)
        result["tool"] = "git.diff"
        return result

    def _handle_execute_python(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        try:
            return run_python_sandboxed(
                self._workspace_root,
                args["script"],
                timeout_seconds=_CODE_TIMEOUT_SECONDS,
            )
        except BoundedCommandError as error:
            raise LocalToolError(error.code, status="failed") from None

    def _handle_test_run_pytest(
        self,
        args: dict[str, Any],
        _call_id: str | None,
        _approval_id: str | None,
    ) -> dict[str, object]:
        argv: tuple[str, ...] = (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        )
        rel_path = args.get("path")
        if isinstance(rel_path, str):
            argv = argv + ("--", rel_path)
        result = _run_bounded(self._workspace_root, argv, _PYTEST_TIMEOUT_SECONDS)
        result["tool"] = "test.run_pytest"
        return result


class LocalToolExecutor:
    """Thread-safe ToolExecutor bridging LocalToolRegistry, TrustService, and TaskStore."""

    def __init__(
        self,
        registry: LocalToolRegistry | None = None,
        *,
        core: NativeDesktopCore | None = None,
        trust: TrustService | None = None,
        trust_service: TrustService | None = None,
        task_store: TaskStore | None = None,
        config: DesktopConfig | None = None,
        owner_id: str = "owner",
        actor_id: str = "desktop_owner",
        session_id: str = "desktop-session",
        device_id: str = "local-desktop",
        anchor_task_id: str | None = None,
        approval_ttl_seconds: int = 300,
        lifecycle_guard: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> None:
        if not isinstance(owner_id, str) or _OWNER_RE.fullmatch(owner_id) is None:
            raise ValueError("invalid_owner_id")
        for ident in (actor_id, session_id, device_id):
            if not isinstance(ident, str) or _OWNER_RE.fullmatch(ident) is None:
                raise ValueError("invalid_actor_scope")
        if anchor_task_id is not None and not _is_canonical_uuid(anchor_task_id):
            raise ValueError("invalid_anchor_task_id")
        if type(approval_ttl_seconds) is not int or not 15 <= approval_ttl_seconds <= 900:
            raise ValueError("invalid_approval_ttl")

        resolved_config = (
            config if config is not None else (core.config if core is not None else None)
        )
        raw_trust = trust if trust is not None else trust_service
        if task_store is not None:
            resolved_store: TaskStore | None = task_store
        elif core is not None and core.engine is not None:
            resolved_store = TaskStore(core.engine)
        elif raw_trust is not None:
            resolved_store = TaskStore(raw_trust.engine)
        else:
            resolved_store = None

        if resolved_store is None:
            raise ValueError("LocalToolExecutor requires trust, core.engine, or task_store")

        if registry is not None:
            resolved_registry = (
                registry
                if registry._task_store is not None
                else registry.with_task_store(resolved_store)
            )
        else:
            resolved_registry = LocalToolRegistry(
                config=resolved_config,
                core=core,
                task_store=resolved_store,
                owner_id=owner_id,
            )

        if raw_trust is not None:
            self._trust = (
                raw_trust
                if raw_trust.has_explicit_registry
                else raw_trust.with_registry(resolved_registry.policy_registry())
            )
        else:
            self._trust = TrustService(
                resolved_store.engine, registry=resolved_registry.policy_registry()
            )

        self._registry = resolved_registry
        self._task_store = resolved_store
        self._owner_id = owner_id
        self._actor_id = actor_id
        self._session_id = session_id
        self._device_id = device_id
        self._anchor_task_id = anchor_task_id
        self._anchor_lock = threading.Lock()
        self._approval_ttl_seconds = approval_ttl_seconds
        self._lifecycle_guard = lifecycle_guard

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return self._registry.tool_definitions()

    def _ensure_anchor_task_id(self) -> str:
        with self._anchor_lock:
            if self._anchor_task_id is not None:
                return self._anchor_task_id
            key = f"tool-session:{self._session_id}"
            task, _ = self._task_store.create(self._owner_id, _ANCHOR_OBJECTIVE, key)
            self._anchor_task_id = task.id
            return task.id

    def _safe_audit_denial(self, task_id: str | None, action: str, reason: str) -> None:
        try:
            resolved_task_id = task_id if _is_canonical_uuid(task_id) else None
            if resolved_task_id is None:
                resolved_task_id = self._ensure_anchor_task_id()
            ctx = ActorContext(
                self._owner_id,
                resolved_task_id,
                self._actor_id,
                self._session_id,
                self._device_id,
            )
            self._trust.record_denial(ctx, action, reason)
        except (TrustError, StoreError, SQLAlchemyError):
            pass

    def _summary(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        status: ToolStatus,
        output: dict[str, object] | str,
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> ToolExecutionSummary:
        return ToolExecutionSummary(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            status=status,
            output=output,
            approval_id=approval_id,
            task_id=task_id,
        )

    def execute(
        self,
        proposal: ToolProposal,
        *,
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> ToolExecutionSummary:
        raw_call_id = (
            proposal.call_id
            if isinstance(proposal, ToolProposal) and isinstance(proposal.call_id, str)
            else ""
        )
        raw_name = (
            proposal.name
            if isinstance(proposal, ToolProposal) and isinstance(proposal.name, str)
            else ""
        )
        raw_args: dict[str, object] = (
            dict(proposal.arguments)
            if isinstance(proposal, ToolProposal) and isinstance(proposal.arguments, dict)
            else {}
        )
        guard = self._lifecycle_guard() if self._lifecycle_guard is not None else nullcontext()
        try:
            with guard:
                return self._execute_locked(
                    proposal,
                    raw_call_id=raw_call_id,
                    raw_name=raw_name,
                    raw_args=raw_args,
                    approval_id=approval_id,
                    task_id=task_id,
                )
        except NativeDesktopError as exc:
            return self._summary(
                call_id=raw_call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status="failed",
                output=exc.code,
                approval_id=approval_id,
                task_id=task_id,
            )
        except SQLAlchemyError:
            return self._summary(
                call_id=raw_call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status="failed",
                output="tool_storage_unavailable",
                approval_id=approval_id,
                task_id=task_id,
            )

    def _execute_locked(
        self,
        proposal: ToolProposal,
        *,
        raw_call_id: str,
        raw_name: str,
        raw_args: dict[str, object],
        approval_id: str | None,
        task_id: str | None,
    ) -> ToolExecutionSummary:
        if not isinstance(proposal, ToolProposal):
            return self._summary(
                call_id=raw_call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status="denied",
                output="invalid_proposal",
                approval_id=approval_id,
                task_id=task_id,
            )
        if not isinstance(proposal.call_id, str) or _CALL_ID_RE.fullmatch(proposal.call_id) is None:
            self._safe_audit_denial(task_id, "execute", "invalid_proposal")
            return self._summary(
                call_id=raw_call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status="denied",
                output="invalid_proposal",
                approval_id=approval_id,
                task_id=task_id,
            )

        spec = self._registry.get_spec(proposal.name)
        if spec is None:
            self._safe_audit_denial(task_id, "execute", "capability_denied")
            return self._summary(
                call_id=proposal.call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status="denied",
                output="capability_denied",
                approval_id=approval_id,
                task_id=task_id,
            )

        try:
            normalized_args = self._registry.validate(proposal.name, proposal.arguments)
        except LocalToolError as exc:
            self._safe_audit_denial(task_id, "execute", exc.code)
            return self._summary(
                call_id=proposal.call_id,
                tool_name=proposal.name,
                arguments=raw_args,
                status=exc.status,
                output=exc.code,
                approval_id=approval_id,
                task_id=task_id,
            )

        canonical_proposal = ToolProposal(
            call_id=proposal.call_id,
            name=proposal.name,
            arguments=normalized_args,
        )

        if approval_id is not None:
            if not _is_canonical_uuid(approval_id):
                self._safe_audit_denial(task_id, "execute", "invalid_approval_identifier")
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status="denied",
                    output="invalid_approval_identifier",
                    approval_id=approval_id,
                    task_id=task_id,
                )
            try:
                grant = self._trust.get_grant(self._owner_id, approval_id)
            except TrustError as err:
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status="denied",
                    output=err.code,
                    approval_id=approval_id,
                    task_id=task_id,
                )

            if grant.state == "pending" and not _is_expired(grant.expires_at):
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status="awaiting_approval",
                    output="owner_approval_required",
                    approval_id=grant.id,
                    task_id=grant.task_id,
                )

            ctx = ActorContext(
                self._owner_id,
                grant.task_id,
                self._actor_id,
                self._session_id,
                self._device_id,
            )
            try:
                output = self._trust.consume_and_execute(
                    ctx,
                    approval_id,
                    canonical_proposal,
                    lambda args: self._registry.execute(
                        spec.name, args, call_id=proposal.call_id, approval_id=approval_id
                    ),
                )
            except TrustError as err:
                mapped_code = err.code
                if err.code == "approval_not_active":
                    if grant.state == "denied":
                        mapped_code = "owner_denied"
                    elif grant.state == "consumed":
                        mapped_code = "approval_already_consumed"
                    elif grant.state == "expired":
                        mapped_code = "approval_expired"
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status="denied",
                    output=mapped_code,
                    approval_id=approval_id,
                    task_id=grant.task_id,
                )
            except LocalToolError as exc:
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status=exc.status,
                    output=exc.code,
                    approval_id=approval_id,
                    task_id=grant.task_id,
                )

            raw_out_task_id = output.get("task_id")
            out_task_id = raw_out_task_id if isinstance(raw_out_task_id, str) else grant.task_id
            return self._summary(
                call_id=proposal.call_id,
                tool_name=proposal.name,
                arguments=raw_args,
                status="completed",
                output=output,
                approval_id=approval_id,
                task_id=out_task_id,
            )

        # No approval_id provided: either request approval (R1/R2) or execute immediately (R0).
        context_task_id: str | None = None
        if task_id is not None:
            if not _is_canonical_uuid(task_id):
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status="failed",
                    output="invalid_task_identifier",
                    task_id=task_id,
                )
            try:
                self._task_store.get(self._owner_id, task_id)
            except StoreError:
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status="failed",
                    output="task_not_found",
                    task_id=task_id,
                )
            context_task_id = task_id
        elif spec.name == "task.status":
            target_task_id = str(normalized_args["task_id"])
            try:
                self._task_store.get(self._owner_id, target_task_id)
            except StoreError:
                self._safe_audit_denial(None, "execute", "task_not_found")
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status="failed",
                    output="task_not_found",
                )
            context_task_id = target_task_id
        else:
            context_task_id = self._ensure_anchor_task_id()

        ctx = ActorContext(
            self._owner_id,
            context_task_id,
            self._actor_id,
            self._session_id,
            self._device_id,
        )

        if spec.requires_approval:
            try:
                grant = self._trust.request(
                    ctx, canonical_proposal, ttl_seconds=self._approval_ttl_seconds
                )
            except TrustError as err:
                return self._summary(
                    call_id=proposal.call_id,
                    tool_name=proposal.name,
                    arguments=raw_args,
                    status="denied",
                    output=err.code,
                    task_id=context_task_id,
                )
            return self._summary(
                call_id=proposal.call_id,
                tool_name=proposal.name,
                arguments=raw_args,
                status="awaiting_approval",
                output="owner_approval_required",
                approval_id=grant.id,
                task_id=grant.task_id,
            )

        try:
            output = self._trust.execute_r0(
                ctx,
                canonical_proposal,
                lambda args: self._registry.execute(
                    spec.name, args, call_id=proposal.call_id, approval_id=None
                ),
            )
        except TrustError as err:
            return self._summary(
                call_id=proposal.call_id,
                tool_name=proposal.name,
                arguments=raw_args,
                status="denied",
                output=err.code,
                task_id=context_task_id,
            )
        except LocalToolError as exc:
            return self._summary(
                call_id=proposal.call_id,
                tool_name=proposal.name,
                arguments=raw_args,
                status=exc.status,
                output=exc.code,
                task_id=context_task_id,
            )

        raw_out_task_id = output.get("task_id")
        out_task_id = raw_out_task_id if isinstance(raw_out_task_id, str) else context_task_id
        return self._summary(
            call_id=proposal.call_id,
            tool_name=proposal.name,
            arguments=raw_args,
            status="completed",
            output=output,
            approval_id=None,
            task_id=out_task_id,
        )


ToolExecutor = LocalToolExecutor
