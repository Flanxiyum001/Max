"""Built-in tool implementations (all workspace- or policy-sandboxed)."""

from __future__ import annotations

import json

from .tools import ToolContext, ToolError, ToolRegistry, ToolSpec, resolve_within

MAX_FILE_CHARS = 20000


def _read_file(args: dict, ctx: ToolContext) -> str:
    path = resolve_within(ctx.workspace_dir, args.get("path", ""))
    if not path.is_file():
        raise ToolError(f"no such file: {args.get('path')}")
    # Also allow reading registered skill files (read-only roots).
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:MAX_FILE_CHARS]


def _read_any(args: dict, ctx: ToolContext) -> str:
    """Read inside any allowed read root (workspace + skill dirs)."""
    raw = str(args.get("path", "")).strip()
    for root in [ctx.workspace_dir, *ctx.read_roots]:
        root = root.resolve()
        try:
            candidate = resolve_within(root, raw)
        except ToolError:
            continue
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
        # allow paths already relative to repo root
    raise ToolError(f"not readable inside sandbox: {raw}")


def _list_files(args: dict, ctx: ToolContext) -> str:
    raw = str(args.get("path", "") or ".")
    root = resolve_within(ctx.workspace_dir, raw)
    if not root.exists():
        raise ToolError(f"no such directory: {raw}")
    entries = []
    for child in sorted(root.iterdir())[:500]:
        entries.append(("dir/" if child.is_dir() else "file ") + child.name)
    return "\n".join(entries) or "(empty)"


def _write_file(args: dict, ctx: ToolContext) -> str:
    path = resolve_within(ctx.workspace_dir, args.get("path", ""))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if len(content) > 200_000:
        raise ToolError("content too large (max 200000 chars)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path.relative_to(ctx.workspace_dir.resolve())}"


def _memory_add(args: dict, ctx: ToolContext) -> str:
    if ctx.memory is None:
        raise ToolError("memory store not configured")
    try:
        return ctx.memory.add(str(args.get("note", "")))
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def _load_skill(args: dict, ctx: ToolContext) -> str:
    registry = ctx.registry
    if registry is None:
        raise ToolError("skill registry not configured")
    name = str(args.get("name", "")).strip()
    try:
        body = registry.load(name)
    except Exception as exc:  # noqa: BLE001 - skill errors become tool output
        raise ToolError(str(exc)) from None
    record = registry.get(name)
    header = f"[skill: {name} v{record.version} sha256={record.sha256[:12]}]\n"
    return header + body[:12000]


def _search_files(args: dict, ctx: ToolContext) -> str:
    """Regex search inside the workspace — no shell, bounded output."""
    import re as _re

    pattern = str(args.get("pattern") or args.get("query") or "").strip()
    if not pattern:
        raise ToolError("pattern is required")
    try:
        rx = _re.compile(pattern)
    except _re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from None

    scope = resolve_within(ctx.workspace_dir, str(args.get("path") or "."))
    if not scope.exists():
        raise ToolError(f"no such path: {args.get('path')}")
    limit = max(1, min(int(args.get("limit") or 50), 100))

    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    skip_ext = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
        ".gz", ".tar", ".exe", ".so", ".dylib", ".dll", ".bin", ".woff",
        ".woff2", ".ttf", ".mp3", ".mp4", ".pyc",
    }
    files = [scope] if scope.is_file() else sorted(
        p for p in scope.rglob("*")
        if p.is_file() and not (skip_dirs & set(p.parts)) and p.suffix.lower() not in skip_ext
    )

    hits: list[str] = []
    scanned = 0
    for path in files:
        if path.stat().st_size > 1_000_000:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                try:
                    rel = path.relative_to(ctx.workspace_dir)
                except ValueError:
                    rel = path
                hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    return "\n".join(hits) + "\n...[truncated]"
    if not hits:
        return f"no matches for /{pattern}/ in {scanned} file(s)"
    return "\n".join(hits)


def _run_command(args: dict, ctx: ToolContext) -> str:
    """Shell execution. Registered but denied by default policy (category=exec)."""
    raise ToolError("shell execution is unavailable in this configuration")


def register_builtin_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="read_file",
            description="Read a text file inside the MAX workspace. Returns the file content.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "path relative to the workspace"}},
                "required": ["path"],
            },
            category="read",
            handler=_read_any,
        )
    )
    registry.register(
        ToolSpec(
            name="list_files",
            description="List files and directories inside the MAX workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "directory relative to workspace"}},
            },
            category="read",
            handler=_list_files,
        )
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description="Write a text file inside the MAX workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            category="write",
            handler=_write_file,
        )
    )
    registry.register(
        ToolSpec(
            name="memory_add",
            description="Append a short durable note to MAX's long-term memory.",
            parameters={
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
            },
            category="memory",
            handler=_memory_add,
        )
    )
    registry.register(
        ToolSpec(
            name="load_skill",
            description="Load the full instructions of a registered skill by name.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            category="skill",
            handler=_load_skill,
        )
    )
    registry.register(
        ToolSpec(
            name="search_files",
            description=(
                "Search files under the workspace with a regular expression; "
                "returns file:line:content matches (bounded)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "python regex"},
                    "path": {"type": "string", "description": "relative dir/file (default .)"},
                    "limit": {"type": "integer", "description": "max matches (<=100)"},
                },
                "required": ["pattern"],
            },
            category="read",
            handler=_search_files,
        )
    )
    registry.register(
        ToolSpec(
            name="run_command",
            description="Run a shell command on the host (restricted; denied by default policy).",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            category="exec",
            handler=_run_command,
        )
    )
    registry.register(
        ToolSpec(
            name="http_request",
            description="Perform an outbound HTTP request (restricted; denied by default policy).",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            category="net",
            handler=lambda args, ctx: (_ for _ in ()).throw(ToolError("network access is unavailable")),
        )
    )
