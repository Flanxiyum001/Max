M36: Proactive Triggers + Multi-Step Task Planner

Status: implemented (this milestone)

## Goal

Let MAX act before the owner types, without ever giving up the core
invariant: nothing state-changing happens without a single-use owner
approval. Two capabilities land together:

1. **Bounded proactive triggers** — deterministic, owner-scheduled
   observations (morning briefing, notes digest, repository status) that
   queue a bounded run for review; promoting the recommended follow-up
   goes through the exact same Trust Inbox approval path as any other
   tool call.
2. **Deterministic multi-step task planner** — `/task`-style objectives
   with explicit owner structure become a dependency-ordered graph of
   sub-steps in the Task Journal; R1/R2 boundaries pause in the Trust
   Inbox exactly like every other approval.

## Scheduling: stdlib cron, not APScheduler (deliberate)

The recurring schedule is a strict 5-field cron (`minute hour dom mon
dow`) parsed by `max_core.triggers.parse_cron`: character-class bounded
(`^[0-9*/,-]{1,64}$`), five whitespace-separated fields, per-field ranges
`((0,59),(0,23),(1,31),(1,12),(0,6))`, lists + ranges + single steps
only, and an explicit emptiness check. `cron_matches` evaluates a moment
against pre-parsed fields using Python's Monday-zero `weekday()`.

APScheduler was **evaluated and deliberately deferred** per
[DEPENDENCY_EVALUATION.md](DEPENDENCY_EVALUATION.md): the ticker is a
single daemon `threading.Thread` (`TriggerLoop`) that wakes on an
interval (validated 15..3600 seconds), calls `TriggerService.tick()`,
and swallows nothing silently — a dead loop stops, it never half-runs.
MAX owns its own task state; the dependency adds nothing a ~60-line
matcher does not already cover, and every schedule component is
deterministic and unit-tested. Re-evaluate only when second-resolution,
timezone-aware, or job-persistence scheduling is actually required.

## Trigger rules and runs

- Kinds are a fixed tuple: `briefing.daily`, `notes.digest`, `repo.status`.
  One rule per `(owner_id, kind)` (unique constraint, replay-safe 409); at
  most 10 rules per owner; invalid cron or unknown kinds are rejected 422.
- `tick()` fires every enabled, due rule **once per calendar minute**
  (deduped via `last_fired_minute`), creating a `max_trigger_runs` row:
  - `state` starts `pending_review`.
  - `output` is a bounded, display-safe summary.
  - At most **one** recommended proposal (`proposal_json`, canonical
    JSON, <= 1000 chars) — never auto-executed, never model-authored.
- At most 20 `pending_review` runs are accumulated; further ticks skip
  firing until the owner reviews (promote or dismiss). The loop blocks
  on review instead of flooding the inbox.
- Observations are strictly read-only and deterministic:
  - `briefing.daily`: grouped `Task` state counts for the owner.
  - `notes.digest`: newest `*.txt` names (static titles only) from
    `MAX_NOTES_DIR`; a missing directory fails closed with no proposal.
  - `repo.status`: `git status --porcelain=v1` via the existing bounded
    subprocess runner inside `MAX_WORKSPACE_ROOT`; failures degrade to a
    "not checked" summary with no proposal.

## Promote path (Trust Inbox, never automatic)

`promote_run` is an explicit owner API action:

1. Creates the anchor task `Trigger run <id> follow-up`.
2. Calls `TrustService.request(...)` with the run's stored proposal —
   a standard single-use R1/R2 `ApprovalGrant`, TTL 900 s.
3. Marks the run `promoted` with the `approval_id`; replay while the
   grant is still pending returns the same run (idempotent).
4. Dismissing is its own terminal state; neither path executes anything.

Because the shared default Trust registry now carries a `task.create`
policy (R1), the run's recommended proposal validates identically to a
chat-proposed `task.create`.

## Multi-step task planner

`max_core.planner.decompose()` splits an objective on the owner's *own*
explicit structure — never invents steps, never calls a model:

- Two or more numbered/bulleted lines win over separators.
- Otherwise `;`, newlines, or ` then ` (case variants) split steps.
- Steps are whitespace-normalized, 1..200 chars, control characters
  rejected, deduped case-insensitively; at most 8 steps
  (`plan_too_many_steps`); fewer than 2 steps means "no plan".

`PlanService.create_plan()` stores the parent task plus sub-steps with
idempotency keys `"<key>:step:<NN>"` (replay with the same key returns
the same plan). A step is `ready` only when every earlier step is
terminal (`completed`/`cancelled`/`failed`) — later steps never race
ahead. `complete_step` is an owner-verified finish: it marks the task
completed and returns the parent's full `PlanView` with the next ready
step flagged, so resuming after an R1/R2 approval is just the existing
Trust flow.

## API surface

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/tasks/plan` | Create a plan (200 new / 200 replayed via `Idempotency-Key` header, 8..92 chars) |
| `GET /v1/tasks/{task_id}/plan` | Parent task + ordered steps with `ready` flags |
| `POST /v1/tasks/{task_id}/complete` | Owner-verified step completion |
| `POST /v1/triggers` (201) / `GET /v1/triggers` / `DELETE /v1/triggers/{rule_id}` (204) | Rule CRUD |
| `GET /v1/triggers/runs` | Bounded run history (limit 1..50) |
| `POST /v1/triggers/runs/{run_id}/promote` | Queue the recommendation into the Trust Inbox |
| `POST /v1/triggers/runs/{run_id}/dismiss` | Mark a run reviewed without promoting |

`TriggerLoop` starts only in `__main__` (never inside `create_app`), so
tests and the web orb never fight a background thread. The ticker is
stopped in a `finally` on shutdown.

## Chat tool: `task.plan`

`task.plan` is an R1 requires-approval local tool with the same
validate-as-`task.create` objective schema. Its idempotency key derives
from the approval (`trust-<approval_id>`) or, when unconsumed, from the
call id (`plan-<sha256[:32]>`), so a replayed approval cannot duplicate
the plan. Docstring count stays in sync: the registry now holds 15
bounded tools.

## Migration

`alembic/versions/0008_proactive_triggers.py` adds:

- `max_tasks.parent_id` (+ index, deliberately **no** FK so plans never
  cascade-delete) and `max_tasks.plan_position`.
- `max_trigger_rules` (unique `(owner_id, kind)`, owner+enabled index).
- `max_trigger_runs` (owner+state, rule_id indexes).

`TaskStore.ready()` checks schema version `0008_proactive_triggers` and
verifies both new tables exist.
