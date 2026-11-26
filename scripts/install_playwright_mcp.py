"""Owner-run setup for MAX's pinned Playwright MCP browser (Node.js 20+)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from max_core.playwright_mcp import PLAYWRIGHT_MCP_VERSION, install_dir


def main() -> int:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        print("Install Node.js 20+ first; node and npm must be on PATH.", file=sys.stderr)
        return 2
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=10, check=False
    )
    try:
        major = int(version.stdout.strip().lstrip("v").split(".")[0])
    except (ValueError, IndexError):
        major = 0
    if version.returncode or major < 20:
        print("Playwright MCP requires Node.js 20 or newer.", file=sys.stderr)
        return 2
    destination = install_dir()
    destination.mkdir(parents=True, exist_ok=True)
    package = f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"
    print(f"Installing {package} for MAX into {destination}")
    try:
        subprocess.run(
            [npm, "install", "--prefix", str(destination), "--save-exact", package],
            check=True,
            timeout=300,
        )
        entry = destination / "node_modules" / "@playwright" / "mcp" / "cli.js"
        subprocess.run(
            [node, str(Path(entry)), "install-browser", "chrome"], check=True, timeout=600
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Playwright MCP setup failed; inspect npm output and retry.", file=sys.stderr)
        return 1
    print(
        "Installed. Open MAX Dashboard > System > Connect Playwright, then Discover and review tools."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
