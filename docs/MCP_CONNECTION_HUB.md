# MAX MCP Connections — one hub, no per-app token variables

MAX's hosted web orb now includes a built-in **Connections** drawer and
natural-language commands for GitHub, Cloudflare, Railway, Notion, Linear and
Sentry. These are *curated official remote MCP endpoints*, not services that
a language model is allowed to invent or auto-install.

## How to connect

1. Sign in to MAX's web orb as the owner.
2. Say **"MAX, connect to Cloudflare"** or **"MAX, connect to Railway"**, or
   click **Apps** and choose a provider.
3. MAX discovers the MCP server's OAuth protected-resource and authorization
   server metadata. If supported, it registers its client automatically
   using a Client ID Metadata Document (CIMD) or Dynamic Client Registration (DCR).
4. MAX shows **Authorize [Provider]**. You sign in on the provider's *own*
   HTTPS domain and approve/reject its permissions.
5. MAX verifies a fresh one-use OAuth state and PKCE verifier, exchanges the
   code on its server, discovers the available MCP tool schemas and pins them.
6. MAX offers only supported flat-scalar tools to its model. Each invocation
   is always explicitly owner-approved with its **full exact arguments**,
   regardless of the remote tool's read-only hint.

"MAX, disconnect Railway" disables all Railway handlers immediately and
deletes its saved credential record. To revoke the grant at the provider,
also remove MAX from that provider's authorized-apps settings.

No token value, password, endpoint URL or per-app environment-variable name
needs to be typed into MAX's conversation or added to Render for
**auto-registering** providers.

## Supported connection methods

| Provider | Official remote MCP | Registration |
|---|---|---|
| Cloudflare | `https://mcp.cloudflare.com/mcp` | Discovered CIMD or DCR, if offered |
| Railway | `https://mcp.railway.com` | Discovered CIMD or DCR, if offered |
| Notion | `https://mcp.notion.com/mcp` | Discovered CIMD or DCR, if offered |
| Linear | `https://mcp.linear.app/mcp` | Discovered CIMD or DCR, if offered |
| Sentry | `https://mcp.sentry.dev/mcp` | Discovered CIMD or DCR, if offered |
| GitHub | `https://api.githubcopilot.com/mcp/` | **Exception**: separate OAuth App registration |

The provider catalog is intentionally small at first. Supporting arbitrary
untrusted URLs without egress/SSRF protection would be unsafe. Add new
providers only after verifying their official endpoint and OAuth issuer
domains. An OAuth metadata response cannot redirect MAX to another provider.

**Providers differ:** Some support CIMD, some DCR, some only a pre-registered
app, or may require administrator approval. MAX does not bypass these
requirements. If the official OAuth metadata has neither CIMD nor DCR, MAX
reports `mcp_oauth_registration_not_supported` instead of asking you to
paste a token. GitHub still needs a one-time application client ID, secret and
callback set on the MAX host as documented in
[WEB_GITHUB_OAUTH.md](WEB_GITHUB_OAUTH.md). These are *MAX's app registration*,
not a personal GitHub access token for every session.

## One credential vault for all apps

MAX uses the existing **MAX_WEB_SECRET** (already generated in
`render.yaml`) as the basis of a separately derived Fernet encryption key.
Every supported provider's OAuth access and refresh tokens and pinned schema
snapshots can be encrypted and saved in one local SQLite database or in the
configured PostgreSQL database.

There are **no app-specific token environment variables**. Authentication
happens at the provider sign-in screen and the tokens remain in MAX's
server-side encrypted vault, never browser localStorage, HTML, a model
prompt, or a committed .env file.

Important storage detail: on Render's free web instances,
`MAX_DATA_DIR=/tmp/max-data` is ephemeral and disappears when the instance
is recreated or redeployed. In that configuration MAX can reconnect via
OAuth but may need approval again after a restart. For durable encrypted
credentials, point MAX's existing `MAX_DATABASE_URL` at managed PostgreSQL
(or optionally use `MAX_MCP_VAULT_DATABASE_URL` for the vault only). This is
**one shared database configuration, not one environment variable per app**.
A lost/rotated `MAX_WEB_SECRET` makes old ciphertext unreadable by design.

Render automatically supplies `RENDER_EXTERNAL_URL`, which MAX uses
to publish its own safe OAuth client metadata at
`/.well-known/oauth-client-metadata/max.json` and receive authorization
at `/api/connect/mcp/callback`.
For non-Render deployments, the operator may configure *one*
`MAX_MCP_PUBLIC_URL` to the HTTPS public MAX origin.

## Security and functional boundaries

- Hosted web sessions use the owner's existing passcode gate.
- Only official catalog hosts, HTTPS port 443, and no redirects are allowed
  for metadata, authorization and token endpoints.
- Remote tool descriptions and schemas are untrusted. MAX pins their
  canonical input schema; unsupported types do not execute.
- Only up to 20 tools total are offered to the model across providers.
- An explicit approval is required **for every remote tool call**, even reads.
- No shell subprocess/stdio MCP servers are automatically installed.
- OAuth registration may be rate-limited, restricted by an administrator,
  or unsupported by individual providers.
- Connections are server-side, single-owner. Multi-user isolation, token
  refresh/reconnect for every provider, and cross-device desktop OAuth UI
  need separate acceptance testing. Do not deploy to production without a
  live owner sign-in test of each provider used.

Protocol: [MCP OAuth authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
and [client registration](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization#client-registration).
