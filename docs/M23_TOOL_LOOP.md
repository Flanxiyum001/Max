# M23 — Bounded tool calling and interactive `max chat`

**Status:** Implemented and unit/mock tested. **Not yet verified against a real
Ollama install or a real free cloud API** — that needs my own machine/keys.

## What it adds

- `src/max_core/tools.py` — tool registry with trusted-code permission tiers:
  - `READ_ONLY` tools run automatically: `clock.now`, `math.calculate`
    (AST-restricted arithmetic, no `eval`), and — only if `MAX_WORKSPACE_DIR`
    is set — `workspace.list` / `workspace.read`.
  - `EFFECTFUL` tools need my typed `y` for each exact call: `notes.write`
    (creates a new note in `<workspace>/max-notes/`, never overwrites).
    With no approver, effectful tools are always denied.
- `src/max_core/tool_loop.py` — up to 6 model↔tool steps, max 4 calls per step,
  10 s per tool, 90 s per turn. Uses a portable text protocol
  (`<tool_call>{"name":..., "arguments":{...}}</tool_call>`) so small local
  models work; native provider tool proposals are also accepted.
  Results go back as labelled **untrusted data**. Unknown tools, extra/mistyped
  arguments, malformed JSON, approver exceptions and tool crashes all fail
  closed with fixed error codes.
- `src/max_core/chat.py` — `python -m max_core.chat`, an interactive terminal
  session with conversation history (`/reset`, `/exit`).

## Setup

Local (Ollama, after `ollama pull qwen2.5:7b` or any tool-capable model):

```
MAX_TRIAL_DESTINATION=verified_local
MAX_TRIAL_LOCAL_EGRESS_REVIEWED=yes
MAX_TRIAL_ENDPOINT_LOCAL=yes
MAX_TRIAL_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
MAX_TRIAL_MODEL=qwen2.5:7b
MAX_TRIAL_CONTEXT_TOKENS=8192
MAX_TRIAL_PERSONA=yes
MAX_WORKSPACE_DIR=C:\Users\me\MaxWorkspace   # optional
```

Free OpenAI-compatible cloud API (any provider with a `/chat/completions` URL):

```
MAX_TRIAL_DESTINATION=cloud
MAX_TRIAL_CLOUD_ALLOWED=yes
MAX_TRIAL_ENDPOINT=https://<provider>/v1/chat/completions
MAX_TRIAL_MODEL=<model id>
MAX_TRIAL_CONTEXT_TOKENS=8192
MAX_TRIAL_API_KEY=<key — set in the shell only, never commit>
MAX_TRIAL_MAX_COST_USD=0
MAX_TRIAL_INPUT_USD_PER_MILLION=0
MAX_TRIAL_OUTPUT_USD_PER_MILLION=0
```

Cloud sessions are sent as `Privacy.PUBLIC`: text the owner types and
results from `clock.now` / `math.calculate` may go to the provider.
**Workspace reads, file listings and note writes are deliberately unavailable
on cloud routes**, even when `MAX_WORKSPACE_DIR` is set. Merely showing an
egress warning is not consent to disclose local files.

## Decisions

- Text protocol instead of extending `ChatMessage` with a `tool` role: avoids a
  cross-cutting gateway contract change and works with weak local models.
  Native `tools`/`tool` role wiring is a possible later milestone.
- Approval is an in-process typed `y/N` from the owner's own terminal.
  The complete arguments (including note content) must be visible before a
  decision; shortened previews never authorize hidden text. This remains a
  local development consent gesture, **not** WebAuthn/device authorization
  (issues #32/#33), and must never be reused by a remote surface.
- Effectful handlers run synchronously in the invoking thread; they are
  never detached into a timed-out background worker that could later write.
  A typed-approval delay beyond the deadline prevents the subsequent write.
  This is **not** process isolation or an OS hard-kill timeout. File creation
  is not rolled back if an OS write stalls after it began; verify the result
  before retrying.

## Risks and not included

- Prompt injection via workspace files can make the model *propose* calls;
  read-only tools have no filesystem writes and effectful tools still need my
  explicit terminal confirmation. Only use owner-controlled, non-secret
  workspaces and genuinely owner-verified local model routes. Path checks do
  not provide adversarial host filesystem isolation against concurrent
  symlink/path replacement.
- No shell, browser, network, email or arbitrary file-write tools.
- No persistence of chat history; not wired into the Qt desktop app yet.
- Secret-looking and dot-files are hidden, but this is a name heuristic.

## Tests

`tests/test_tool_loop.py`: execution + result feedback, denial without approver,
exact-argument approval, no overwrite, approver exceptions, unknown/malformed
calls, step limit, sanitized model failure, calculator escapes, workspace
traversal/symlink/secret-name confinement, local request flags.

Rollback: revert the three modules, the test file and this document. No
migration, secret, listener or deployment.
