# M14 — reviewed MCP tool-intent admission

An opt-in internal review function binds each inert gateway tool proposal to a
host-reviewed, owner-scoped MCP discovery snapshot and an exact schema digest.
The host manually sets the risk tier, enabled intent-review status and permitted
argument keys. Remote readOnlyHint metadata and model text grant no permission.

Review denies unknown tools, changed schema hashes, cross-owner tools, malformed
scalar arguments, duplicates and ambiguous registry entries. Result contains
only a binding hash and safe metadata. All intents are blocked_pending_dispatch,
including read-only R0. No MCP calls are made.

This is a TOOL-INTENT REVIEW, not tool execution or an owner approval. The
existing MAX Trust only executes task.status and bounded_worker remains pure.
Do not expose a model-controlled dispatcher on the strength of this library.

Live execution requires pinned endpoint identity, a secure device-bound session,
single-use owner approval with verified WebAuthn for high risk, an isolated
worker with OS egress/filesystem/resource limits, cancellation/reconciliation,
revalidation immediately before dispatch, redacted output retention and durable
task timeline events. Uncertain side effects must never be blindly replayed.
