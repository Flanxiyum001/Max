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

Cloud sessions are sent as `Privacy.PUBLIC`: everything typed **and every tool
result (including workspace files read)** goes to that provider. The CLI prints
this warning at start.

## Decisions

- Text protocol instead of extending `ChatMessage` with a `tool` role: avoids a
  cross-cutting gateway contract change and works with weak local models.
  Native `tools`/`tool` role wiring is a possible later milestone.
- Approval is an in-process typed `y/N` from the owner's own terminal. It is
  not WebAuthn/device approval (issues #32/#33 remain open) and is not used by
  any remote surface.

## Risks and not included

- Prompt injection via workspace files can make the model *propose* calls;
  read-only tools have no effects and effectful tools still need my `y`.
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
