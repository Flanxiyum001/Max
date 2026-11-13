# Milestone 10 — explicitly configured MCP discovery

## Delivered scope

A Python library can register multiple distinct, host-configured HTTPS MCP
connections, initialize sessions using the official MCP Python SDK, list tool
metadata, handle bounded pagination and close sessions. Both JSON and SSE are
handled by the SDK. Discovery requires an explicit host approval for each
connection. No server is configured or contacted by default. Credentials are
injected by the host, excluded from repr, and never returned in tool descriptors.

This is a review branch, not a deployed personal assistant. It has no public API,
CLI configuration loader, automatic plugin installer, tool execution, device
control, model loop, GitHub write action or live account verification. Existing
MAX Trust still registers only its existing task.status capability. Discovered
schemas are inert canonical JSON strings, qualified by connection ID and tool
name; remote descriptions, annotations and instructions do not become policy.

```python
from max_core.mcp_discovery import MCPConnection, MCPRegistry, discover_tools

# Trusted host configuration only; never accept these fields from a model or
# imported plugin. A trusted caller awaits this function in its async runtime.
registry = MCPRegistry((
    MCPConnection("reviewed-service", "https://service.example/mcp", True),
))
# observations = await discover_tools(registry.resolve("reviewed-service"))
# This lists metadata. It does not execute any discovered tool.
```

## Boundaries and limits

- HTTPS port 443, no URL credentials, query or fragment, no redirects or ambient
  proxies. Endpoint paths may be sensitive and are omitted from configuration repr.
- Host must enforce DNS/IP egress policy with a constrained network or trusted
  transport before exposing this library to remotely supplied configuration.
  Exact-URL checking does not by itself prevent DNS rebinding or a configured
  endpoint resolving to a private network. This is not an SSRF-safe public API.
- 30-second whole-operation deadline, 10-second request/read limits, at most
  16 pages, 512 tools, 64 connections, 1 MB per HTTP response stream and 64 KB
  canonical schema per tool. No automatic retry after ambiguous failures.
- Duplicate names within one server and repeated cursors reject the entire
  observation. The same tool name on different servers remains distinct.
- No roots, model sampling or elicitation capabilities are advertised. No
  tools/call, resources/read or prompts/get is issued by this module.
- OAuth, stdio/subprocess MCP servers, reconnect/resumption, durable registry,
  UI, tool execution grants and plugin installation remain future work.
- Fixed errors suppress nested SDK exceptions at the caller boundary. The host
  must keep SDK/HTTP debug logging disabled and use redacted logging; it must not
  attach raw exceptions, endpoints, schemas or upstream payloads to task logs.
- Untrusted input schemas are retained as data, not executed or independently
  proven valid/safe. A schema digest detects changes; it is not a trust grant.

## Full personal-assistant workflow and remaining implementation

The owner requests: remote instruction -> wake PC -> isolate project -> perform
coding -> tests -> push feature branch -> create PR -> observe exact-head CI ->
preserve evidence -> safely shut down -> send completion or blocker report.

Deliver this through separate owner-reviewed milestones on owner-merged max:

1. This MCP discovery library; then audited plugin manifests and exact scoped
   tool invocation integrated with stronger trusted authorization and audit.
2. Authenticated device enrollment, outbound job channel, revocation, replay
   protection, durable leases, cancellation and restart reconciliation.
3. Isolated coding environment with project allowlists, scoped GitHub identity,
   resource/deadline limits and a bounded agent loop. The everyday PC session is
   not a sandbox. A separate constrained VM or equivalent boundary is needed.
4. PR preparation and independent observers for commit SHA, required check
   coverage, terminal CI state and artifacts. Do not merge or deploy automatically.
   Unknown/queued/skipped/cancelled checks must not be reported as success.
5. Wake relay and safe shutdown with fresh device-local idle/saved-work checks,
   no active owner session/jobs, cancellation handling and independent offline
   observation. A shutdown request is not proof of power-off.
6. Authenticated remote command interface and owner notification delivery with
   durable delivery/reconciliation. Report failure and partial completion too.

Wake requires an always-on reachable controller or relay and hardware/network
support. Confirm OS, firmware wake support, network topology, sleep versus full
power-off behavior and available relay before implementation on actual hardware.
A powered-off PC cannot run its own remote listener. Do not expose a public
unauthenticated wake, shell or shutdown endpoint.

## Validation and rollback

Use the repository CI on Python 3.11 and 3.13, PostgreSQL migrations/integration,
Ruff, mypy, complete pytest and vulnerability audit. Tests use the actual SDK
against in-process HTTP fixtures and never access a personal MCP account.
Local results and any skipped checks must be reported in the PR, separately
from verified GitHub checks. No live PC, wake, shutdown or provider proof exists.

Rollback: revert this milestone commit and dependency addition. No schema,
existing API, existing permissions or production configuration is changed.

Protocol sources checked 2026-11-13:
- https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
