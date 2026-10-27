"""Tree-sitter repository intelligence: symbols, limits, path safety, fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from max.codesearch import (
    backend_status,
    find_symbol,
    index_workspace,
    language_for,
    symbol_source,
)
from max.core.tools import ToolError

PY_FILE = '''"""module docstring with the word calculate in it."""
import os

class Calculator:
    def calculate(self, a, b):
        return a + b

def calculate_total(items):
    # calculate is mentioned in this comment too
    return sum(items)
'''

JS_FILE = """export function fetchUser(id) {
  return fetch('/users/' + id);
}
export class Widget {
  render() { return null; }
}
"""


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(PY_FILE, encoding="utf-8")
    (tmp_path / "ui.js").write_text(JS_FILE, encoding="utf-8")
    (tmp_path / "notes.md").write_text("# calculate notes\n", encoding="utf-8")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03")
    (tmp_path / "broken.py").write_text("def unfinished(:\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_KEY=abc\n")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    return tmp_path


def test_backend_status_reports_truth():
    status = backend_status()
    assert isinstance(status["tree_sitter"], bool)
    assert "javascript" in status["fallback_regex_languages"]
    assert "python" in status["fallback_regex_languages"]


def test_lists_symbols_with_line_ranges(ws):
    result = index_workspace(ws)
    names = {(s["name"], s["kind"]) for s in result["symbols"]}
    assert ("Calculator", "class") in names
    assert ("calculate_total", "function") in names
    assert ("fetchUser", "function") in names  # regex fallback for js
    assert ("helper", "function") in names
    calc = next(s for s in result["symbols"] if s["name"] == "calculate_total")
    assert calc["path"] == "app.py"
    assert calc["line_start"] >= 1 and calc["line_end"] >= calc["line_start"]


def test_malformed_source_is_reported_not_silently_valid(ws):
    result = index_workspace(ws)
    assert any("broken.py" in entry for entry in result["parse_errors"])


def test_unsupported_and_binary_and_secret_files_are_skipped(ws):
    result = index_workspace(ws)
    # .env and data.bin are excluded outright; notes.md has no grammar
    assert result["files_skipped_unsupported"] >= 1
    scanned_paths = [s["path"] for s in result["symbols"]]
    assert ".env" not in scanned_paths and "data.bin" not in scanned_paths


def test_symbol_search_beats_plain_text_search(ws):
    """Plain text search hits comments/docstrings/uses; symbol search returns definitions."""
    plain_hits = []
    for line_no, line in enumerate((ws / "app.py").read_text().splitlines(), 1):
        if "calculate" in line:
            plain_hits.append(line_no)
    found = find_symbol(ws, "calculate_total")
    assert found["exact"], "symbol search must find the definition"
    assert len(found["exact"]) == 1
    # the text hits include prose/comment lines the symbol query ignores
    assert len(plain_hits) >= 3
    assert found["exact"][0]["line_start"] not in (
        next(n for n, l in enumerate((ws / "app.py").read_text().splitlines(), 1) if l.startswith('"""') or "docstring" in l),
    )


def test_find_symbol_partial_when_no_exact_match(ws):
    found = find_symbol(ws, "Calculat")
    assert found["exact"] == []
    assert any(s["name"] == "Calculator" for s in found["partial"])


def test_symbol_source_returns_bounded_snippet(ws):
    result = symbol_source(ws, "app.py", "Calculator", context_lines=20)
    assert result["symbol"]["name"] == "Calculator"
    assert "class Calculator" in result["snippet"]
    assert len(result["snippet"]) <= 6000 + 200
    assert "calculate_total" in [s for s in result["available_symbols"]]


def test_path_traversal_and_symlink_escape_are_rejected(ws, tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def evil():\n    pass\n", encoding="utf-8")
    link = ws / "escape.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ToolError):
        find_symbol(ws, "evil", "../..")
    with pytest.raises(ToolError):
        symbol_source(ws, "../../../etc/passwd", "x")
    # a symlink pointing outside the workspace must not be indexed
    result = index_workspace(ws)
    assert not any(s["path"] == "escape.py" for s in result["symbols"])


def test_large_files_are_skipped(ws):
    big = ws / "big.py"
    big.write_text("x = 1\n" * 120000, encoding="utf-8")  # > 512 KiB
    result = index_workspace(ws)
    assert not any(s["path"] == "big.py" for s in result["symbols"])


def test_missing_symbol_raises_actionable_error(ws):
    with pytest.raises(ToolError, match="not found"):
        symbol_source(ws, "app.py", "does_not_exist")


def test_degrades_gracefully_without_tree_sitter(ws, monkeypatch):
    """Core must keep working when the optional binding is absent."""
    import max.codesearch as cs

    monkeypatch.setattr(cs, "_TS_AVAILABLE", False)
    result = index_workspace(ws)
    assert result["backend"] == "regex-fallback"
    names = {s["name"] for s in result["symbols"]}
    assert {"Calculator", "calculate_total", "helper"} <= names
    found = find_symbol(ws, "calculate_total")
    assert found["exact"]


def test_language_detection():
    assert language_for(Path("a/b.py")) == "python"
    assert language_for(Path("a/b.ts")) == "typescript"
    assert language_for(Path("a/b.png")) is None
