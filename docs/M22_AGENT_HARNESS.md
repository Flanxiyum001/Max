# M22 Stage A — MAX agent harness: honest model activity and inert tool proposals

**Status:** Implemented as an owner-invoked Python library, not a continuously
running coding assistant or background executor.

## Why this boundary comes first

MAX already has Gateway, model admission, signed task-event metadata, reviewed
memory, a limited pure bounded worker, and future Trust/MCP policy contracts.
But there is not yet a reviewed general coding sandbox or per-device WebAuthn
approval for side-effectful tool execution (see open issues #32 and #33).

This stage adds `AgentHarness.run(GatewayRequest, on_activity=...)`, which:

1. Records `run.accepted` and `model.started` when a single real
   `CompletionGateway.complete()` call begins.
2. On a valid model response, records `model.responded`, then either
   `response.ready` (text available) or individual `tool.proposed` events
   followed by `review.required`.
3. On failure or timeout records `run.failed` with a fixed safe error code.
   Cancellation propagates and emits `run.cancelled` to a local display.
4. Emits **metadata only**: sequence, fixed kind and bounded tool name. Prompts,
   model content, arguments, credentials, endpoint URLs, exception text and
   provider details are never copied into activity events or logs.
5. Restricts to one model turn, up to eight inert tool proposals and a bounded
   timeout. Validates request-id association and refuses duplicate tool IDs.
6. Preserves normalized `GatewayResponse` privately for a trusted caller's
   later workflow. Receiving text or a tool proposal is **not** a completed task
   or an approved action.

A local view may show 'Model generating' only while a model call has actually
started; it must show 'Tool proposed (not executed)' rather than 'Using tool'
until a separately authorized executor and independent result observer exist.

## What is not included

- No native-app model connection, hosted provider credentials, automatic tool
  execution, read/write MCP, command execution, code-file edits, iterative
  planning, task-store transitions, automatic retries or self-approval.
- No authorization via model reply, text prompt, local GUI click, WebSocket event
  or display callback. Trust gates remain independent.
- No claimed 'coding now' or successful implementation without real trusted
  worker and observer evidence. Current `DevelopmentTeam` callbacks still
  require a separately isolated host to be trustworthy.
- No future runnable UI can infer active tool calls from signed timeline
  metadata alone: those events record state, not tool arguments/execution.

## Acceptance and rollback

Review mocked success, error, deadline, cancellation, malformed tool, duplicate
tool and observer sanitization tests. Verify all existing CI checks on Python
3.11 and 3.13 with PostgreSQL and dependency audit. This stage introduces no
schema migration, cloud account, external effect or runtime permission change.

**Next independent stage:** connect a trusted local activity display to real
model/worker event sources, with explicit backend truth labels, while keeping
the GUI usable when no AI provider is configured. The user must opt in to real
inference. Later executable tools require issue #32 / #33 gates and new
independent execution verification.

Rollback: revert `agent_harness.py`, its tests and this document. No data
migration or deployment action is required.
