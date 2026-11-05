# TOOLS — Registry and Integrations

MAX uses a central capability registry. Each capability entry should include:

- Name, version, description, and owner.
- Supported operations and input/output schemas.
- Required scopes and approval level.
- Authentication and connection status.
- Health, rate limits, and timeouts.
- Data sensitivity and retention classification.
- Audit requirements.
- Authorised fallback implementations.

## Integration standards

Support MCP for interoperable tool servers, native APIs for reliable service-specific operations, and internal functions for core capabilities. MCP is a transport/integration standard, not a substitute for planning, authorization, isolation, or verification.

## Candidate integrations

GitHub, Railway, Cloudflare, Google Drive, email, WhatsApp Business Platform, telephony provider, Spotify, browser/search, home automation, local filesystem, terminal, development tools, and device management.

All integrations are candidates until their capabilities, licensing, account permissions, regional availability, and costs are verified.

## Browser and research

Use a dedicated MAX-controlled browser profile separate from the owner's personal browser. Explore self-hosted metasearch (such as SearXNG) and direct website access before paid search APIs. Respect website terms, robots controls, rate limits, access controls, and copyright. Never represent scraping as unlimited or guaranteed free.
