# MAX: conversational GitHub connection (web orb / Render)

When the owner says or types **"MAX, connect to GitHub"**, MAX:
1. Recognizes the service request on the host (not by trusting arbitrary model output).
2. Presents an **Authorize GitHub** button. The owner clicks it and reviews GitHub's requested access on github.com.
3. Validates a random, single-use OAuth state and PKCE code verifier on the server.
4. Exchanges the authorization code server-side, verifies the GitHub username, connects to GitHub's official remote HTTPS MCP endpoint, and discovers its tool schemas.
5. Offers up to 20 compatible flat-scalar MCP tools to the hosted assistant. Each invocation is marked EFFECTFUL and **needs explicit owner approval** in the orb, regardless of the remote tool's read-only hint.
6. On **"MAX, disconnect GitHub"**, immediately revokes MAX's in-memory connection and invalidates all old tool handlers. Revoke the OAuth grant in GitHub settings to revoke the token itself.

The owner never enters a PAT or MCP server URL in MAX. No OAuth token, client secret or authorization code is returned in the web API response or shown to the language model.

## One-time application setup (operator)

GitHub's official remote MCP server does **not** currently support Dynamic Client Registration for arbitrary third-party OAuth clients. MAX therefore needs its own OAuth application identity to perform browser authorization. This *cannot* be created by a chat prompt or silently borrowed from ChatGPT, and it is not a requirement the end user can approve away.

On github.com, create a dedicated **OAuth App** for this owner-controlled MAX Render deployment. In the application registration:

- Application URL: the HTTPS origin of your Render MAX web orb.
- Callback URL: the same origin, followed by `/api/connect/github/callback`.
- Do not use an unrelated callback, wildcard host or arbitrary redirect.

In the Render service environment settings, provision these secrets/configuration values:

- `MAX_GITHUB_OAUTH_CLIENT_ID` — GitHub OAuth App's client ID.
- `MAX_GITHUB_OAUTH_CLIENT_SECRET` — GitHub OAuth App's confidential client secret.
- `MAX_GITHUB_OAUTH_CALLBACK_URL` — exact registered HTTPS callback URL.

Restart/redeploy MAX after setting them. For example, if MAX is hosted on `https://max.example.onrender.com`, the callback is `https://max.example.onrender.com/api/connect/github/callback`.

**GitHub scope warning:** This version requests `repo read:user`. OAuth App `repo` scope covers private repository access broadly. The GitHub authorization page is the place to review and decline it. MAX additionally requires a separate approval for each GitHub MCP tool action, but that is not a substitute for restricting GitHub app scopes. For tighter per-repository access, use a GitHub App with selected-repository permissions in a later version.

## Render lifecycle, security and limitations

- The token is **in server process memory only**. On free Render, an instance restart, redeploy, autosuspend/recycle or replica change loses the GitHub connection: say "connect to GitHub" again. No long-lived token is saved in a file, browser cookie, Render env, or MAX memory tool.
- This release supports GitHub's official remote Streamable HTTP MCP server only; it does not automatically provision arbitrary MCP hosts, local stdio agents, or browser login for every service.
- Only tools with **flat primitive input schemas** (string/integer/number/boolean) are registered. Unsupported tools are not silently exposed with relaxed validation.
- A GitHub sign-in success is **not** the same as an MCP connection success: MAX must also discover usable tools. If discovery fails, the connection reports failure.
- The web orb is **single-owner** and protected by `MAX_WEB_PASSCODE`; GitHub OAuth state is one-time and short-lived (10 minutes). Keep `MAX_WEB_PASSCODE`, `MAX_WEB_SECRET`, and the OAuth App secret private.
- MAX automatically refreshes the available GitHub tool list at the next assistant turn, and a revoked connection blocks any stale tool handler at execution time.
- Authorizing a tool may send its output to the selected external language model as data. Do not approve a call if its output contains data you do not wish to share.
- Tool outputs are untrusted; they are not commands or instructions to MAX.

Official documentation: https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md
