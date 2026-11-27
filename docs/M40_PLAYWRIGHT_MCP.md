# M40 — Microsoft Playwright MCP (`@playwright/mcp`)

MAX supports Microsoft's official Playwright MCP server
([microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp)) over the
allowlisted local stdio MCP bridge (`src/max_core/mcp_stdio.py`).

## Security & Isolation Defaults
- **Dedicated profile**: Launches with `--isolated` by default so MAX never touches
  your personal Chrome profile or cookies.
- **Allowlisted binary**: `MAX_MCP_STDIO_BINARIES` must explicitly list the host
  `npx` (or `MAX_PLAYWRIGHT_MCP_BIN`) absolute path.
- **Trust-gated actions**: Read-only inspection tools (`browser_snapshot`,
  `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests`,
  `browser_tab_list`, `browser_wait_for`) are `READ_ONLY`; navigation, clicks,
  typing, form fills, uploads, and `browser_evaluate` are `EFFECTFUL` and require
  explicit MAX Trust owner approval.

## Quick Setup
1. Ensure Node.js (`npx`) is installed (`python -m max_core.mcp_playwright`).
2. Set `MAX_MCP_STDIO_BINARIES` to your resolved `npx` path.
3. Register Playwright MCP into your MAX MCP profile:
   ```powershell
   python -m max_core.mcp_playwright --register
   ```
