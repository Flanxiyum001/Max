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

Not in this release: arbitrary local stdio child-process MCP servers, OAuth browser/PKCE login or refresh, resources/prompts, nested argument schemas, unattended calls, and the ability to share ChatGPT's MCP credentials.

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
