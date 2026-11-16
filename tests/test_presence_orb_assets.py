"""Static desktop orb rendering surface checks, not a platform window test."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

FILE = Path(__file__).resolve().parents[1] / "clients" / "orb" / "index.html"


def test_orb_surface_carries_only_presentation_states() -> None:
    html = FILE.read_text(encoding="utf-8")
    assert "<canvas" in html
    assert "prefers-reduced-motion" in html
    for state in ("idle", "listening", "thinking", "speaking", "awaiting_approval"):
        assert state in html
    assert "setView(view)" in html
    assert "visibilitychange" in html
    assert "window.setTimeout" in html
    assert "WebSocket(" not in html
    assert "getUserMedia(" not in html
    assert "fetch(" not in html
    assert "approve(" not in html
    assert "https://" not in html


def test_orb_script_parses_when_node_available(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed on this runner")
    html = FILE.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", html, flags=re.DOTALL)
    assert match is not None
    script = tmp_path / "orb.js"
    script.write_text(match.group(1), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
