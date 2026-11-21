# M22 Stage B — Native MAX activity trace

**Status:** Qt desktop read-only activity surface backed by authenticated MAX
Core state and the existing owner-scoped task-event journal.

## Visible behavior

Open the native MAX app using `start-max.cmd` or
`python -m max_core.native_desktop`. With migrated local PostgreSQL, select
a task on the Tasks screen. Its right-hand activity panel shows:

- **Current authoritative state**, such as QUEUED, RUNNING, VERIFYING,
  CANCELLED, VERIFIED or REVIEW NEEDED.
- **Recorded chronological checkpoints**, with a human-readable label,
  revision and a clarification of what each event actually proves.
- **Automatic refresh every 3.5 seconds while the Tasks page is open**,
  with a single in-flight request and no polling while the app is closed.

It is intentionally explicit that a queued task has not started work, a worker
lease does not prove code was written, a proposed result is not independently
verified, and a generic task journal cannot reveal tool-call arguments or
model thought. The UI does not invent a 'coding...' or 'using tool...' indicator.

The rendering contract is a strict metadata allowlist in
`src/max_core/activity_view.py`: only recognized event kinds get specific
labels. Unknown kinds appear as 'Core event recorded' and arbitrary raw event
content is discarded. Neither objectives nor model/provider text or tool
arguments are copied into the visible activity structures. Invalid events fail
closed with a fixed backend error.

## Internal wiring

- `NativeDesktopCore.activity(task_id)` uses existing, authenticated
  `GET /v1/tasks/{id}` and bounded `/v1/tasks/{id}/events?limit=20`.
- A GUI worker thread fetches read-only data; the Qt main thread renders
  it through the existing completion signal, not by direct network access.
- No public API routes, new migrations, background node agent, or settings
  changes. Works as a standalone native program, not a browser-only mockup.
- When no local database exists, the panel honestly says storage is not ready.

## How this relates to AgentHarness

M22 Stage A (PR #47) separately introduces normalized model-call activity:
`model.started`, `model.responded`, `tool.proposed`,
`review.required` and `response.ready`. Those **runtime events are not
currently persisted in the M5 task journal or connected to the native app**.
The next integration gate is an owner-session-bound event bridge from a real
configured Gateway/harness/isolated executor, with trusted tool observation,
redaction, no replay authorization, bounded history and independent verification.

Until then MAX cannot honestly show that it is editing files or executing
tools simply because a task was added. A model reply is not a completed coding
task; an executable coding tool requires separate isolated MAX Trust review
(issues #32 and #33).

## Tests and rollback

Unit tests cover true queued activity, safe known and unknown event labels,
untrusted event payload suppression, invalid revisions, and authenticated
Core-backed activity retrieval. Native Qt smoke and complete Python matrix
are required. **Physical Windows and Hyprland real-device testing remains
separate**, including focus, resize and responsiveness.

Rollback: revert the pure view, Qt/backend changes, tests and this document.
No schema change, credential, network listener or external effect.
