# MAX desktop MCP connections

MAX must authenticate to its own MCP servers. GitHub or other ChatGPT integrations are not automatically inherited by the standalone MAX application.

## Supported in this release

- Remote HTTPS Streamable HTTP MCP endpoints.
- Optional bearer-token authorization, supplied through a local environment variable, NEVER a stored token value.
- Explicit Add > Discover > Enable steps. New tools are disabled by default.
- The exact observed JSON input schema is pinned by SHA-256. Re-discovery revokes prior enables.
- Every tool execution requires a fresh one-time owner approval. The full desktop uses MAX Trust Inbox; the default orb asks for explicit permission and records a one-use Trust grant.
- Only scalar schema arguments are allowed: string, integer, number, boolean. Nested array/object schemas are not auto-executable.
- Approved tool results are untrusted data. When a cloud model is used, approved remote tool output may be forwarded to that model as context.

Local stdio child-process MCP servers are supported since M35, but only through the host-pinned binary allowlist (`MAX_MCP_STDIO_BINARIES`), a stripped child environment, and the same per-call Trust approval gate; see [M35](M35_SANDBOXED_CODE_AND_STDIO_MCP.md).

## Playwright browser for local MAX

On your Windows PC, install Node.js 20+ and run from the MAX repository root:

```powershell
.\.venv\Scripts\python.exe .\scripts\install_playwright_mcp.py
```

This installs Microsoft's `@playwright/mcp` version **0.0.86** under `%LOCALAPPDATA%\MAX\playwright-mcp` and installs its Chrome browser. The package and browser downloads happen only when you run this setup command. On Linux/macOS, run `python scripts/install_playwright_mcp.py`; the package goes under `$XDG_DATA_HOME/max/playwright-mcp` or `~/.local/share/max/playwright-mcp`.

Open Dashboard → System → **Connect Playwright**, then select its row, **Discover**, and **Enable tool** on each tool you intend to use. The package version and server entry point are checked at connection time; a changed package is rejected until reviewed. MAX keeps this particular stdio server alive across approved calls so navigate, snapshot, and click can share one isolated browser session. The server starts lazily on the first approved call and closes on revoke or desktop shutdown. The isolated headless profile does not reuse your normal Chrome sign-ins and is discarded when MAX closes. Browser tool input/output is untrusted; each invocation still needs a fresh owner approval. For public Google research, navigate to a public search URL and inspect the returned page snapshot.

Playwright MCP is an external browser automation process, **not** an OS or network security boundary. It can visit URLs, follow redirects, execute site scripts, and access web content. Do not enable tools that submit forms, upload files, or interact with logged-in services until you have reviewed their exact action. If a cloud model is selected, approved browser snapshots may be sent to that provider. Disable the tools in System to revoke them; stopping MAX terminates the child. The package remains installed on disk until you delete its installation directory.

Not in this release: OAuth browser/PKCE login or refresh, resources/prompts, nested argument schemas, unattended calls, and the ability to share ChatGPT's MCP credentials.

## GitHub setup

1. Create a fine-grained GitHub personal access token with only the repository privileges you actually need. Prefer read-only at first, and do not grant workflow or admin permissions by default.
2. In the same PowerShell session that launches MAX, set the variable MAX_MCP_GITHUB_TOKEN to your token. Do not put the token in a Git file or chat.
3. Launch MAX with start-max.cmd; open Dashboard via the orb menu, or run the desktop with --dashboard.
4. In System > MCP Connections, click Add server:
   - ID: github
   - HTTPS endpoint: https://api.githubcopilot.com/mcp/
   - Token environment variable: MAX_MCP_GITHUB_TOKEN
5. Select the github server row and click Discover. MAX will contact the MCP endpoint; discovered tools remain disabled.
6. Select each desired tool and click Enable tool only after reviewing its name, schema SHA-256, allowed scalar fields and permission risk.
7. Ask MAX for a GitHub lookup. MAX offers only reviewed tools. Confirm its exact remote call in Trust Inbox or the orb confirmation screen.

The hosted GitHub MCP server may offer OAuth by default, but this release uses the documented PAT method. If GitHub requires interactive OAuth for your account, this release does not perform that flow.

## Local profile and security

Profile metadata is saved at %LOCALAPPDATA%/MAX/mcp-servers.json on Windows or ~/.config/max/mcp-servers.json on POSIX. The host-only MAX_MCP_CONFIG_PATH variable overrides this location.

The profile stores server URLs, token environment variable names, discovered schemas and SHA-256 values, and the owner's explicit enable flags. It never stores credentials.

MAX caps configured servers at 16 and enabled tools at 20. A missing token, disallowed input, changed schema, denied approval, or unsupported endpoint must fail closed. If you update the token environment, restart MAX. For private repositories, review remote tool output disclosure to the selected LLM provider before consenting.

To revoke a tool: select it in System and click Disable tool. To revoke a credential, remove it from your OS environment/secret store or revoke the token at GitHub. Do not treat disabling the tool as token revocation.

GitHub's official setup guide: https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/set-up-the-github-mcp-server
