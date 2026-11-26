# Tool recovery in MAX

This milestone adapts the **per-turn tool-call guardrail** idea from NousResearch's
[Hermes Agent](https://github.com/NousResearch/hermes-agent/blob/main/agent/tool_guardrails.py)
(MIT). MAX does not vendor Hermes's tool registry or runtime: its three existing
entry points still use MAX's trusted tool schemas, exact owner approval and
provider egress policy.

## Behavior

- `MaxHarness` (native orb), `ToolLoop` (local trial chat) and
  `AssistantChatService` (dashboard cloud chat) now track failed tool signatures
  within each turn. The signature is a local hash of the name and JSON arguments.
- A sanitized error code gives the model an action to consider: fix schema
  arguments, inspect tool availability, check network/service state, or explain
  an owner denial. An available `web.search` may be used to research generic
  error terms; private arguments, paths and outputs must not be placed in a
  search query. Search is **not automatic**, so MAX does not leak private
  diagnostic context to a third party.
- Two failures with the same name and arguments block another attempt; an
  owner-denied action blocks immediately. Four failures for one tool also
  stop that tool even when arguments differ. The model can switch tools or
  explain the blocker. The existing turn deadline, cost cap and call budgets
  remain in force.
- MAX does not claim a fix or repeat effectful actions automatically. A timed
  out read may keep running briefly in a thread; the harness no longer retries
  timeouts immediately. These guardrails do not inspect arbitrary exception
  messages and cannot infer every root cause.

## Rollback and scope

Revert this PR to remove the guardrail. No new network endpoint, tool
permission, credential, or stored diagnostic log is introduced. Upstream
Hermes has a much broader runtime; exact behavioral parity would conflict
with MAX's Trust and egress model and is outside this milestone.
