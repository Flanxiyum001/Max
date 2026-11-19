"""Fail-closed desktop static asset and API integration checks."""

from __future__ import annotations

import shutil
import subprocess
from importlib.resources import files

import pytest
from fastapi.testclient import TestClient

from max_core.desktop.assets import desktop_asset


@pytest.mark.parametrize(
    ("route", "content_type"),
    [
        ("/desktop", "text/html"),
        ("/desktop/app.css", "text/css"),
        ("/desktop/app.js", "text/javascript"),
    ],
)
def test_local_desktop_assets_have_browser_defenses(
    client: TestClient, route: str, content_type: str
) -> None:
    response = client.get(route)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    csp = response.headers["content-security-policy"]
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
    ):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


def test_untrusted_web_host_rejected_and_no_remote_asset_request(client: TestClient) -> None:
    assert client.get("/desktop", headers={"Host": "untrusted.example"}).status_code == 400
    assert client.get("/desktop/anything-else").status_code == 404
    for route in ("/desktop", "/desktop/app.css", "/desktop/app.js"):
        assert client.get(route).status_code == 200


def test_desktop_asset_allowlist() -> None:
    with pytest.raises(ValueError, match="unsupported_desktop_asset"):
        desktop_asset("../config.py")  # type: ignore[arg-type]


def test_ui_is_honest_and_does_not_persist_credentials_or_render_api_html() -> None:
    bundle = files("max_core.desktop")
    markup = bundle.joinpath("index.html").read_text(encoding="utf-8")
    script = bundle.joinpath("app.js").read_text(encoding="utf-8")
    assert 'href="/desktop/app.css"' in markup
    assert 'src="/desktop/app.js"' in markup
    assert 'id="connect-form"' in markup
    assert "No AI chat or recording upload" in markup
    assert "not an approval surface" in markup
    for required in (
        'api("/v1/session")',
        'api("/v1/tasks")',
        'api("/v1/approvals")',
        'api("/v1/tasks", { method: "POST"',
        'credentials: "omit"',
        'cache: "no-store"',
        'getUserMedia',
        'stopMic',
        'disconnect',
        'textContent',
    ):
        assert required in script
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "innerHTML",
        "outerHTML",
        "eval(",
        "new Function(",
        "document.cookie",
        "XMLHttpRequest",
    ):
        assert forbidden not in script
    for forbidden in ("<script>", "onclick=", "onload=", "http://cdn.", "https://cdn."):
        assert forbidden not in markup


def test_optional_javascript_syntax_validation() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js parser unavailable")
    path = files("max_core.desktop").joinpath("app.js")
    result = subprocess.run(
        [node, "--check", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr[:500]
