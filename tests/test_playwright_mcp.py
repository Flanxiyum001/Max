"""Pinned browser package is only connected after explicit local setup."""

import json
import sys
from pathlib import Path

import pytest

from max_core.mcp_desktop import MCPDesktopManager, MCPSettingsError
from max_core.playwright_mcp import installed_connection


def test_playwright_connection_requires_pinned_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("max_core.playwright_mcp.install_dir", lambda _env=None: tmp_path)
    monkeypatch.setattr("max_core.playwright_mcp.shutil.which", lambda _name: sys.executable)
    root = tmp_path / "node_modules" / "@playwright" / "mcp"
    root.mkdir(parents=True)
    (root / "cli.js").write_text("// fixture", encoding="utf-8")
    manifest = root / "package.json"
    manifest.write_text(json.dumps({"name": "@playwright/mcp", "version": "0.0.85"}))
    with pytest.raises(ValueError, match="playwright_install_invalid"):
        installed_connection()
    manifest.write_text(json.dumps({"name": "@playwright/mcp", "version": "0.0.86"}))
    connection = installed_connection()
    assert connection.persistent and connection.discovery_approved
    assert "--isolated" in connection.args and "--headless" in connection.args
    manager = MCPDesktopManager(tmp_path / "profile.json", env={})
    manager.add_playwright()
    assert manager.connections()[0]["persistent"] is True
    assert manager.connections()[0]["tools"] == ()
    manifest.write_text(json.dumps({"name": "@playwright/mcp", "version": "unreviewed"}))
    with pytest.raises(MCPSettingsError, match="playwright_install_invalid"):
        manager.discover("playwright")


def test_playwright_package_symlink_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("max_core.playwright_mcp.install_dir", lambda _env=None: tmp_path)
    monkeypatch.setattr("max_core.playwright_mcp.shutil.which", lambda _name: sys.executable)
    root = tmp_path / "node_modules" / "@playwright" / "mcp"
    root.parent.mkdir(parents=True)
    root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="playwright_install_invalid"):
        installed_connection()
