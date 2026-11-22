# M30 — live MCP tool execution behind single-use Trust approvals

M14 decided that reviewed MCP tool intents are always inert: the Trust stage
only executed `task.status`. This stage closes that gap in the same doctrine
without an unrestricted `shell.run` style surface: every executable remote
tool becomes exactly one dotted, host-allowlisted Trust policy.

## Naming and registration

`max_core.mcp_execute` maps every owner-reviewed, digest-pinned MCP tool to a
single dotted canonical name `mcp.<connection_id>.<tool_name>` registered in
a `ToolPolicyRegistry` via `build_mcp_policy_registry(...)` or
`MCPToolExecutor.policy_registry`. The host merges (or dedicates) a
`TrustService` registry; tool names are never derived from model output,
plugin manifests, or a live `tools/list` call. Ambiguous dotted names
(connection ids whose dot-splitting collides) are rejected at construction
with `ambiguous_mcp_tool_name`; dot-free connection identities
(`[A-Za-z0-9_-]{1,64}`) are required for live execution. Disabled reviewed
policies never register; executing them returns `capability_denied`.

## Risk tiers

The remote, untrusted `readOnlyHint` only labels a tool R1 (`r1_local`)
instead of R2 (`r2_write`) — `auto_risk`. Both tiers require a fresh
single-use owner approval, so the hint never establishes a permission by
itself. Reviewed policies pinned at R0 or R3 are refused
(`invalid_mcp_live_policy`): remote MCP execution is never auto-approved and
never high-risk-unsandboxed.

## Approval and dispatch flow

1. Model proposals naming `mcp.<conn>.<tool>` flow through the normal
   gateway; `MCPToolExecutor` validates the proposal against the reviewed
   schema snapshot with `mcp_policy.validated_arguments` (exact digest, exact
   argument allowlist, scalar bounds) — every prior MAX gate still applies.
2. Without an `approval_id` the executor requests one grant from Trust for
   the proposal's canonicalized arguments (binding digest covers tool name,
   owner, actor, session, device, task, call id and canonical args) and
   makes zero network calls (`test_no_network_egress_before_owner_approval`).
3. The owner approves through the existing owner-only Trust flow. On resume,
   `trust.consume_and_execute` re-validates and consumes the single grant
   under row locks, re-running the same validator.
4. Only then does the handler run: a fresh `tools/list` observation over the
   pinned endpoint must reproduce the exact canonical schema bytes, digest,
   and read-only hint (`schema_drifted` denies otherwise), and
   `tools/call` executes with the same validated arguments.
   Both validation-before-dispatch steps can never be skipped.

## Egress and output bounds

Dispatch reuses the shared `_EndpointTransport` from `mcp_transport`:
HTTPS-only, pinned endpoint identity, no redirects, no compressed responses,
whole-operation 30 s deadline, response bytes bounded before decode. SDK
exception groups are unwrapped to fixed codes (`mcp_call_timeout`,
`mcp_call_failed`, `mcp_tool_failed`) so upstream payloads, URLs or tokens
never reach callers or logs. Tool output keeps only text blocks, strips
control characters and truncates with an explicit marker. Tool result text is
model input, never instruction: the chat loop treats it as data exactly like
local tool output.

## Scoped developer tools (local, not MCP)

`git.status`, `git.diff` and `test.run_pytest` join `LocalToolRegistry` as
R1 tools that always require owner approval. They run through
`max_core.bounded_commands` — fixed argv, no shell, scrubbed environment,
hard timeout, bounded and sanitized output. Workspace path arguments are
resolved inside the configured workspace root; argv-leading `-` prefixes are
denied before resolution so tool output can never be turned into host
invocation. `run_pytest` executes owner-authored tests in the workspace and
is treated as R1 with mandatory approval, not R0.

## Composition

`CompositeToolExecutor` routes local names to the primary executor and
dotted `mcp.*` names to `MCPToolExecutor`, so chat, Trust Inbox and approval
resume work unchanged.
