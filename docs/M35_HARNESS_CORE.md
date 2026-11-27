# M35 — Harness core adapted from DeepSeek Harness design

**Status:** Implemented as a Python library with unit and keyless replay tests.
Not wired into `max chat`, the web orb or the desktop yet. Not verified
against a live model.

## Source and licence

The design comes from `deepseek-ai/deepseek-harness` (MIT, TypeScript, built on
Cordis). I copied no code. I read its README, `docs/architecture.md` and
`AGENTS.md` and took these ideas from them:

| DeepSeek Harness | MAX `max_core.harness_core` |
|---|---|
| Append-only `SessionEvent` log; "model-visible means logged" | `SessionLog`, `derive_messages()`; every request is built from the log |
| Versioned `session.vN.jsonl` | `session.v1.jsonl` with monotonic `seq`, unknown kinds rejected |
| Plugins with reversible effects that unwind on unload | `Context.load/unload`; `provide`/`on` return disposers; a failed setup unwinds |
| `turn/*`, `step/*`, `tools/pre-execute`, `tools/post-execute` events | Same names, emitted by `AgentLoop` |
| Keyless recorded-session snapshots | `RecordingGateway` / `ReplayGateway`, `tests/snapshots/*.json` |

I did not port Cordis, profiles/bundles, YAML patch layering, the Web UI, the
SDK/ACP servers, sandbox backends, subagents or compaction.

## Trust difference (deliberate)

In DeepSeek Harness, plugins can change the tool pipeline. In MAX, listeners
can only **observe** or **deny**. No listener return value approves a tool.
Effectful tools still need the owner's approval callback, as in M23. A listener
that raises during `tools/pre-execute` denies the call (`hook_failed`).
Observer listener errors are ignored. Tool results go back to the model as
labelled untrusted data. Tool risk tiers and argument validation are the
existing `max_core.tools` code.

## Risks and unknowns

- The step loop is cooperative. There is no per-turn wall-clock deadline yet,
  only per-tool timeouts and step limits. A caller should wrap `run_turn` in
  `asyncio.wait_for`.
- The loop does not yet use `ToolFailureGuard` from M23, so a model can repeat
  a failing call until the step limit.
- Session files hold conversation text. Keep them in owner-local storage.

## Rollback

Delete `src/max_core/harness_core/`, `tests/test_harness_core.py` and
`tests/snapshots/harness_core_clock.json`. Nothing else imports them.
There are no schema, config or runtime permission changes.
