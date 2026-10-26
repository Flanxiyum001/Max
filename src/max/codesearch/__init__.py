"""Syntax-aware repository intelligence (optional).

Uses tree-sitter when its binding and a grammar are installed; otherwise it
falls back to a bounded regex extractor for common languages and reports
``fallback: true``. Unsupported files degrade to the existing plain-text
``search_files`` tool rather than failing.

Limits: file size, file count, symbols per file and total output are all
capped, no source file is ever executed, and every path is resolved inside
the authorized workspace (symlink escapes rejected).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

from ..core.tools import ToolError, resolve_within

try:  # optional dependency
    from tree_sitter import Language, Parser
    import tree_sitter_python as _tsp

    _PY_LANGUAGE = Language(_tsp.language())
    _TS_AVAILABLE = True
except Exception:  # noqa: BLE001 - absence must not break the assistant
    _TS_AVAILABLE = False
    _PY_LANGUAGE = None

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 2000
MAX_SYMBOLS = 500
MAX_OUTPUT_CHARS = 6000
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".max-tmp",
    ".tox", ".eggs", "vendor",
}
SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".gz", ".tar",
    ".whl", ".so", ".dylib", ".dll", ".exe", ".bin", ".pyc", ".pyo", ".woff",
    ".woff2", ".ttf", ".ico", ".db", ".sqlite", ".parquet",
}
SECRET_NAME_HINTS = ("id_rsa", "id_ed25519", ".pem", ".key", ".p12", ".env")

_LANG_BY_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript",
                ".ts": "typescript", ".tsx": "typescript", ".go": "go",
                ".java": "java", ".rb": "ruby", ".rs": "rust", ".c": "c",
                ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".sh": "shell"}

# Fallback patterns for languages without an installed grammar.
_REGEX_RULES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    # Used when the tree-sitter binding (and its python grammar) is absent.
    "python": (
        ("function", re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)")),
        ("class", re.compile(r"^class\s+([A-Za-z_]\w*)")),
    ),
    "javascript": (
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("method", re.compile(r"^\s{2,}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")),
    ),
    "typescript": (
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
        ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=")),
    ),
    "go": (
        ("function", re.compile(r"^func\s+(?:\([^)]*\)\s+)?([A-Za-z_]\w*)\s*\(")),
        ("type", re.compile(r"^type\s+([A-Za-z_]\w*)\s")),
    ),
    "java": (
        ("class", re.compile(r"^\s*(?:public\s+|final\s+|abstract\s+)*class\s+(\w+)")),
        ("method", re.compile(r"^\s{2,}(?:public|private|protected)\s+[\w<>\[\]]+\s+(\w+)\s*\(")),
    ),
    "shell": (("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{")),),
}


@dataclass
class Symbol:
    name: str
    kind: str
    path: str
    line_start: int
    line_end: int

    def as_dict(self) -> dict:
        return asdict(self)


def backend_status() -> dict:
    """What is actually available in this environment (reported, not assumed)."""
    return {
        "tree_sitter": _TS_AVAILABLE,
        "grammars": ["python"] if _TS_AVAILABLE else [],
        "fallback_regex_languages": sorted(_REGEX_RULES),
    }


def language_for(path: Path) -> str | None:
    return _LANG_BY_EXT.get(path.suffix.lower())


def _iter_files(root: Path, max_files: int) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= max_files:
            break
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in SKIP_EXTS:
            continue
        if any(hint in path.name.lower() for hint in SECRET_NAME_HINTS):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def _read(path: Path) -> bytes | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _python_symbols(data: bytes, rel: str) -> tuple[list[Symbol], int]:
    parser = Parser(_PY_LANGUAGE)
    tree = parser.parse(data)
    symbols: list[Symbol] = []
    errors = 0
    stack = [tree.root_node]
    while stack and len(symbols) < MAX_SYMBOLS:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            errors += 1
        if node.type in {"function_definition", "class_definition"}:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                symbols.append(
                    Symbol(
                        name=data[name_node.start_byte : name_node.end_byte].decode("utf-8", "replace"),
                        kind="class" if node.type == "class_definition" else "function",
                        path=rel,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                    )
                )
        stack.extend(reversed(node.children))
    if tree.root_node.has_error:
        errors += 1
    return symbols, errors


def _regex_symbols(text: str, rel: str, language: str) -> tuple[list[Symbol], int]:
    rules = _REGEX_RULES.get(language, ())
    symbols: list[Symbol] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in rules:
            match = pattern.match(line)
            if match:
                symbols.append(
                    Symbol(name=match.group(1), kind=kind, path=rel, line_start=lineno, line_end=lineno)
                )
                break
        if len(symbols) >= MAX_SYMBOLS:
            break
    return symbols, 0


def index_workspace(workspace: Path, rel_dir: str = ".") -> dict:
    """Parse every supported file under ``rel_dir`` and collect symbols."""
    root = workspace.resolve()
    base = resolve_within(root, rel_dir) if rel_dir not in {"", "."} else root
    if not base.is_dir():
        raise ToolError(f"not a directory: {rel_dir}")
    files = _iter_files(base, MAX_FILES)
    symbols: list[Symbol] = []
    parse_errors: list[str] = []
    scanned = 0
    unsupported = 0
    used_fallback = False
    for path in files:
        lang = language_for(path)
        if lang is None:
            unsupported += 1
            continue
        data = _read(path)
        if data is None:
            continue
        rel = str(path.relative_to(root))
        scanned += 1
        try:
            if lang == "python" and _TS_AVAILABLE:
                found, errors = _python_symbols(data, rel)
            elif lang == "python":
                found, errors = _regex_symbols(data.decode("utf-8", "replace"), rel, lang)
                used_fallback = True
            else:
                found, errors = _regex_symbols(data.decode("utf-8", "replace"), rel, lang)
                used_fallback = True
        except Exception as exc:  # noqa: BLE001 - a bad file must not break indexing
            parse_errors.append(f"{rel}: {exc.__class__.__name__}")
            continue
        symbols.extend(found)
        if errors:
            parse_errors.append(f"{rel}: parse errors ({errors})")
        if len(symbols) >= MAX_SYMBOLS:
            break
    return {
        "backend": "tree-sitter" if _TS_AVAILABLE and not used_fallback else "regex-fallback",
        "files_scanned": scanned,
        "files_skipped_unsupported": unsupported,
        "symbols": [s.as_dict() for s in symbols[:MAX_SYMBOLS]],
        "parse_errors": parse_errors[:20],
        "truncated": len(symbols) > MAX_SYMBOLS,
    }


def find_symbol(workspace: Path, name: str, rel_dir: str = ".") -> dict:
    name = str(name or "").strip()
    if not name or len(name) > 120:
        raise ToolError("symbol name is required (max 120 chars)")
    index = index_workspace(workspace, rel_dir)
    exact = [s for s in index["symbols"] if s["name"] == name]
    partial = [s for s in index["symbols"] if name in s["name"] and s not in exact]
    return {
        "query": name,
        "backend": index["backend"],
        "exact": exact,
        "partial": (partial if not exact else [])[:20],
        "parse_errors": index["parse_errors"],
        "files_scanned": index["files_scanned"],
    }


def symbol_source(workspace: Path, rel_path: str, symbol: str, context_lines: int = 40) -> dict:
    root = workspace.resolve()
    path = resolve_within(root, rel_path)
    if not path.is_file():
        raise ToolError(f"file not found: {rel_path}")
    if path.suffix.lower() in SKIP_EXTS:
        raise ToolError("binary or excluded file type")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ToolError("file exceeds the 512 KiB symbol limit")
    data = _read(path)
    if data is None:
        raise ToolError("file could not be read")
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if language_for(path) == "python" and _TS_AVAILABLE:
        found, _ = _python_symbols(data, rel_path)
    else:
        found, _ = _regex_symbols(text, rel_path, language_for(path) or "")
    match = next((s for s in found if s.name == symbol), None)
    if match is None:
        raise ToolError(f"symbol '{symbol}' not found in {rel_path}")
    start = max(0, match.line_start - 1 - context_lines // 4)
    end = min(len(lines), match.line_end + context_lines)
    snippet = "\n".join(lines[start:end])
    if len(snippet) > MAX_OUTPUT_CHARS:
        snippet = snippet[:MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return {
        "symbol": match.as_dict(),
        "start_line": start + 1,
        "snippet": snippet,
        "available_symbols": [s.name for s in found[:50]],
    }


def register_symbol_tools(registry, workspace: Path) -> None:
    """Register ``list_symbols`` / ``find_symbol`` / ``get_symbol_source``."""
    from ..core.tools import ToolContext, ToolSpec

    def _list(args: dict, ctx: ToolContext) -> str:
        import json

        result = index_workspace(workspace, args.get("path") or ".")
        return json.dumps(result, indent=2)

    def _find(args: dict, ctx: ToolContext) -> str:
        import json

        return json.dumps(
            find_symbol(workspace, args.get("name", ""), args.get("path") or "."), indent=2
        )

    def _source(args: dict, ctx: ToolContext) -> str:
        import json

        return json.dumps(
            symbol_source(
                workspace,
                args.get("path", ""),
                args.get("symbol", ""),
                int(args.get("context_lines") or 40),
            ),
            indent=2,
        )

    registry.register(
        ToolSpec(
            name="list_symbols",
            description="Parse supported source files and list functions/classes with file and line ranges.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "directory relative to workspace"}},
            },
            category="read",
            handler=_list,
        )
    )
    registry.register(
        ToolSpec(
            name="find_symbol",
            description="Find a function/class/interface by exact name (then partial matches) in the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string", "description": "restrict to a directory"},
                },
                "required": ["name"],
            },
            category="read",
            handler=_find,
        )
    )
    registry.register(
        ToolSpec(
            name="get_symbol_source",
            description="Return the bounded source snippet for a symbol, with its exact line range.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "file path relative to workspace"},
                    "symbol": {"type": "string"},
                    "context_lines": {"type": "integer"},
                },
                "required": ["path", "symbol"],
            },
            category="read",
            handler=_source,
        )
    )
