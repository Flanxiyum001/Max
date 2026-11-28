"""Workspace-confined coding tools for MAX Harness.

All file paths are validated by ``Workspace.resolve`` (blocking absolute paths,
path traversal, dotfiles, and secret filenames). Read tools are ``READ_ONLY``;
edits, writes, and test/lint execution are ``EFFECTFUL`` and require explicit
owner approval before running.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from max_core.tools import (
    MAX_RESULT_CHARS,
    Tool,
    ToolError,
    ToolParam,
    ToolRegistry,
    ToolRisk,
    Workspace,
)

_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}
)
_ALLOWED_RUNNERS = frozenset({"pytest", "ruff", "mypy"})
_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9_./:=,@+-]{1,120}$")


def _iter_workspace_files(ws: Workspace, folder: Path) -> list[Path]:
    files: list[Path] = []
    stack = [folder]
    while stack and len(files) < 1000:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                continue
            if entry.is_symlink():
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                try:
                    rel = entry.relative_to(ws.root).as_posix()
                    ws.resolve(rel)
                    files.append(entry)
                except ToolError:
                    continue
    return files


class CodingWorkspace:
    """Bounded coding operations confined to a single ``Workspace``."""

    def __init__(self, root: Path, *, python_executable: str | None = None) -> None:
        self.ws = Workspace(root)
        self._python = python_executable or sys.executable

    def read_lines(self, args: Mapping[str, Any]) -> str:
        path = self.ws.resolve(args["path"])
        if not path.is_file():
            raise ToolError("not_a_file")
        if path.stat().st_size > 512_000:
            raise ToolError("file_too_large")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            raise ToolError("not_text") from None

        start = int(args.get("start_line", 1))
        end = int(args.get("end_line", start + 199))
        if start < 1 or end < start:
            raise ToolError("invalid_line_range")
        end = min(end, start + 249, len(lines))
        if start > len(lines) and lines:
            raise ToolError("line_out_of_range")
        numbered = [f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1)]
        header = f"{args['path']} (lines {start}-{end} of {len(lines)})"
        body = "\n".join(numbered) if numbered else "(empty file)"
        return f"{header}\n{body}"[:MAX_RESULT_CHARS]

    def search(self, args: Mapping[str, Any]) -> str:
        raw_pattern = args["pattern"]
        if not raw_pattern:
            raise ToolError("empty_pattern")
        try:
            regex = re.compile(raw_pattern)
        except re.error:
            regex = re.compile(re.escape(raw_pattern))

        folder = self.ws.resolve(args.get("path", "."))
        if not folder.is_dir():
            raise ToolError("not_a_directory")
        max_results = max(1, min(int(args.get("max_results", 40)), 80))

        hits: list[str] = []
        for file_path in _iter_workspace_files(self.ws, folder):
            if file_path.stat().st_size > 256_000:
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = file_path.relative_to(self.ws.root).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{lineno}: {line[:200]}")
                    if len(hits) >= max_results:
                        return "\n".join(hits)[:MAX_RESULT_CHARS]
        return "\n".join(hits) or "(no matches)"

    def edit(self, args: Mapping[str, Any]) -> str:
        path = self.ws.resolve(args["path"])
        if not path.is_file():
            raise ToolError("not_a_file")
        if path.stat().st_size > 512_000:
            raise ToolError("file_too_large")
        target = args["target"]
        replacement = args["replacement"]
        if not target:
            raise ToolError("empty_target")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError("not_text") from None

        has_crlf = "\r\n" in content
        norm_content = content.replace("\r\n", "\n")
        norm_target = target.replace("\r\n", "\n")
        norm_replacement = replacement.replace("\r\n", "\n")

        count = content.count(target)
        if count == 0:
            norm_count = norm_content.count(norm_target)
            if norm_count == 1:
                norm_updated = norm_content.replace(norm_target, norm_replacement, 1)
                updated = norm_updated.replace("\n", "\r\n") if has_crlf else norm_updated
                path.write_text(updated, encoding="utf-8")
                return f"edited {args['path']}"
            elif norm_count > 1:
                raise ToolError("target_not_unique")
            raise ToolError("target_not_found")
        if count > 1:
            raise ToolError("target_not_unique")
        updated = content.replace(target, replacement, 1)
        path.write_text(updated, encoding="utf-8")
        return f"edited {args['path']}"

    def write(self, args: Mapping[str, Any]) -> str:
        path = self.ws.resolve(args["path"])
        overwrite = bool(args.get("overwrite", False))
        if path.exists() and not overwrite:
            raise ToolError("file_exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"wrote {args['path']} ({len(args['content'])} chars)"

    def run_tests(self, args: Mapping[str, Any]) -> str:
        runner = str(args.get("runner", "pytest")).strip().lower()
        if runner not in _ALLOWED_RUNNERS:
            raise ToolError("runner_not_allowed")
        raw_target = str(args.get("target", "")).strip()
        cmd = [self._python, "-m", runner]
        if runner == "ruff":
            cmd.append("check")
        if raw_target:
            for part in raw_target.split():
                norm_part = part.replace("\\", "/")
                if (
                    not _SAFE_ARG_RE.fullmatch(norm_part)
                    or ".." in norm_part
                    or norm_part.startswith("/")
                ):
                    raise ToolError("invalid_runner_arg")
                cmd.append(norm_part)
        if runner == "pytest" and "-q" not in cmd:
            cmd.append("-q")
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.ws.root,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolError("tool_timeout") from None
        except OSError:
            raise ToolError("runner_unavailable") from None

        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        combined = combined.strip() or "(no output)"
        return f"exit_code={proc.returncode}\n{combined}"[:MAX_RESULT_CHARS]


def register_coding_tools(
    registry: ToolRegistry,
    workspace: Path,
    *,
    python_executable: str | None = None,
) -> ToolRegistry:
    """Register workspace-confined coding tools onto ``registry``."""
    cw = CodingWorkspace(workspace, python_executable=python_executable)
    tools = (
        Tool(
            "code.read",
            "Read a UTF-8 file in the workspace with line numbers (start_line, end_line).",
            ToolRisk.READ_ONLY,
            (
                ToolParam("path", str, "relative file path"),
                ToolParam("start_line", int, "1-based start line", required=False),
                ToolParam("end_line", int, "1-based end line", required=False),
            ),
            cw.read_lines,
        ),
        Tool(
            "code.search",
            "Search UTF-8 workspace files for a text or regex pattern.",
            ToolRisk.READ_ONLY,
            (
                ToolParam("pattern", str, "text or regex pattern", max_length=200),
                ToolParam("path", str, "relative directory, default '.'", required=False),
                ToolParam("max_results", int, "max hits (1-80)", required=False),
            ),
            cw.search,
        ),
        Tool(
            "code.edit",
            "Replace an exact unique target block in a workspace file.",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("path", str, "relative file path"),
                ToolParam("target", str, "exact existing text to replace", max_length=10_000),
                ToolParam("replacement", str, "replacement text", max_length=10_000),
            ),
            cw.edit,
        ),
        Tool(
            "code.write",
            "Create or overwrite a UTF-8 file in the workspace.",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("path", str, "relative file path"),
                ToolParam("content", str, "file contents", max_length=30_000),
                ToolParam("overwrite", bool, "allow overwriting existing file", required=False),
            ),
            cw.write,
        ),
        Tool(
            "code.run_tests",
            "Run pytest, ruff, or mypy inside the workspace.",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("runner", str, "pytest, ruff, or mypy", required=False),
                ToolParam("target", str, "relative path or target args", required=False),
            ),
            cw.run_tests,
        ),
    )
    for tool in tools:
        if registry.get(tool.name) is None:
            registry.register(tool)
    return registry
