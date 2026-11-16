"""Static PWA safety contracts. Not a replacement for iPhone E2E testing."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "clients" / "iphone"


def source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_install_manifest_stays_scoped_to_mobile_shell() -> None:
    data = json.loads(source("manifest.webmanifest"))
    assert data["display"] == "standalone"
    assert data["scope"] == "./"
    assert data["start_url"] == "./index.html"


def test_mobile_has_three_views_and_no_privileged_approval_button() -> None:
    html = source("index.html")
    for view in ("voice", "memory", "approvals"):
        assert f'data-view="{view}"' in html
        assert f'data-tab="{view}"' in html
    assert 'id="ptt"' in html
    assert 'type="button"' in html
    assert "script-src 'self'" in html
    assert "connect-src 'self'" in html
    assert "<script>" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert 'id="approve"' not in html
    assert 'id="reject"' not in html


def test_private_snapshots_are_read_only_and_never_offline_cached() -> None:
    js = source("app.js")
    worker = source("service-worker.js")
    assert 'fetch("/api/mobile/v1/snapshot"' in js
    assert 'method: "GET"' in js
    assert 'credentials: "same-origin"' in js
    assert 'cache: "no-store"' in js
    assert "document.hidden" in js
    assert "clearPrivateData();" in js
    assert "audio: { echoCancellation: true, noiseSuppression: true }" in js
    assert "getTracks()" in js and "track.stop()" in js
    for forbidden in ("localStorage", "sessionStorage", "innerHTML", "eval(", "postMessage("):
        assert forbidden not in js
    assert '"/api/' not in worker
    assert '"push"' not in worker
    assert "SHELL_PATHS.has(url.pathname)" in worker


@pytest.mark.parametrize("filename", ["app.js", "service-worker.js"])
def test_javascript_syntax_when_node_available(filename: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed on this test runner")
    result = subprocess.run(
        [node, "--check", str(ROOT / filename)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
