# MAX Milestone 7 — bounded local developer worker

**Status:** owner-review implementation branch `feat/phase1-bounded-worker`, based on `max` commit `a1907698d1adfeda588350f5f00c471f704308db`. This is **not** an autonomous agent, task scheduler, production worker, shell, model tool, or external-effects dispatcher.

## 1. Scope and trust boundary

MAX gains a **manually invoked**, single-task trusted development worker. There are **exactly two** immutable built-in operation identifiers, `dev.health` and `dev.add`. No task objective can register a callable, choose an executable, import a Python module, read files, call a URL, or grant permission. Unknown and malformed inputs are denied **before** claiming a queued task.

Permissions for both operations: `local_owner_development` (trusted owner-started local Python process with **separate local PostgreSQL connection credentials** and a valid existing local bootstrap configuration). **No worker HTTP API is exposed**; the authorization boundary remains the owner-scoped TaskStore and locally managed database credential. This is **not** a general-purpose trust grant and does **not** extend the model-callable Trust registry, whose only action remains `task.status`.

No use of model-generated tool proposals, webhook input, web page data or third-party text as instructions. DB objective JSON is *untrusted data* and is parsed against exact object and numeric fields.

## 2. Registered pure operations

| Stable ID | Exact input `params` | Fixed output (canonical, sorted-key JSON bytes) | Time / size bound | Permission | Invalid/fault behavior |
| --- | --- | --- | --- | --- | --- |
| `dev.health` | Empty object `{}` | `{"operation":"dev.health","status":"ok"}` | Fixed-time static response; ≤256-byte output, monitored elapsed ≤1 s | Local owner development | Unknown input/extra key denied before claim; faults quarantined |
| `dev.add` | Exactly `{"a": INT, "b": INT}`, both strict JSON integers within −1,000,000..1,000,000 | `{"operation":"dev.add","result":SUM}`; sum within ±2,000,000 | One constant-time integer addition; ≤256-byte output, monitored elapsed ≤1 s | Local owner development | Floating-point, boolean, oversized/extra fields denied; runtime fault quarantined |

Both operations receive an objective of the form `{"op":"dev.add","params":{"a":2,"b":3}}`, within the existing maximum task-objective length (500 characters) and a stricter worker maximum of 500 UTF-8 bytes. Duplicate JSON keys, freeform commands, unknown operation IDs, nonfinite numbers, scripts, paths, URLs and arbitrary expressions are denied. Output is a small fixed JSON object. **`dev.health` is a static development check, not a measurement of computer/AI/network health**.

**Time-limit qualification:** Because the only registered operations are O(1), pure Python with strictly bounded input, they have bounded computational work; the worker measures the computation's elapsed wall clock and quarantines a result exceeding one second. It **does not** preempt a blocked Python function using an operating-system watchdog. That would be necessary **before** introducing any blocking I/O, untrusted code, dynamically loaded skill, subprocess, browser, model, or long-running operation. This initial limit is safe only while the registry stays hard-coded to the two trivial pure operations; it is **not** a general execution sandbox.

## 3. Task lifecycle sequence (trusted worker)

1. Owner creates a **non-sensitive JSON objective** using the existing authenticated Milestone 6 CLI, which stores the objective in plaintext development PostgreSQL. The worker is launched *manually* with a task UUID; it does not poll, schedule or scan queued tasks.
2. Worker reads through `TaskStore.get("owner", task_id)` and requires `queued`, then validates the operation ID and exact input schema **before** attempting to claim. Invalid inputs remain queued and are not executed.
3. Worker atomically calls existing `TaskStore.claim(...)` with a distinct random worker ID and a **30-second lease**; PostgreSQL row locking ensures only one claim succeeds. The worker never relies on a prior read for claim authorization.
4. It checks the latest state at safe boundaries and durably creates a checkpoint using existing `TaskStore.checkpoint(...)`; the checkpoint stores a SHA-256 digest of the validated objective (no raw output or secrets). This is an **execution-start checkpoint**, not proof that a result succeeded.
5. It evaluates only the fixed pure operation. If the measured operation time/output limit is exceeded, or an internal exception interrupts execution, it calls new **lease-fenced `TaskStore.quarantine_claim(...)`** to move the *existing* `running`/`cancelling` state to `needs_reconciliation` and release the lease. No exception payload, objective, claim token or raw output is logged.
6. For a normal operation, the worker computes SHA-256 of the **actual canonical output bytes**, then uses **existing** `TaskStore.record_outcome(...)`. The DB task moves to `awaiting_verification` and clears the lease. The CLI labels any displayed tiny result as **UNVERIFIED**. It does **not** call `IndependentVerifier`, fabricate evidence, or set the task to `completed`.
7. A separately configured trusted independent `EvidenceReader` is still required to observe real bytes and verify them using existing `IndependentVerifier`. **No live independent observer has been added** in this milestone. A test fixture can exercise the existing gate, but fixture success is not production evidence.

The existing `max_outbox` contains metadata events. The worker does **not** send, lease or acknowledge outbox deliveries. The existing timeline remains read-only.

## 4. Cancellation, interruption, recovery and lease fencing

- **Before claim:** A queued task can be cancelled by the existing Core API and becomes `cancelled`. The worker rejects it; no operation starts.
- **During execution:** Existing Core `cancel()` changes a running task to `cancelling`. That is **not a completed cancellation**. Worker checks state before/after checkpoints and after the pure operation. After local execution has actually stopped, new **trusted internal** `acknowledge_stopped_cancel(owner_id, task_id, worker_id)` requires the same unexpired worker lease, owner scope and `cancelling` state under row lock. Only then may it move `cancelling → cancelled` and emit `task.cancelled`. No HTTP route or model permission is added.
- **Cancel versus result:** If cancellation races with `record_outcome`, PostgreSQL locks serialize them. The winner determines whether the task is `cancelled` (worker stopped and acknowledged) or `awaiting_verification` (worker recorded its unverified outcome first and the later cancel returns 409). The worker does not claim verified success in either case.
- **Worker faults/interruption:** The worker fences failures with `quarantine_claim`. SIGKILL, abrupt power loss and expired leases leave uncertain work in `running` or `cancelling` until the existing `reconcile_expired(...)` quarantines it to `needs_reconciliation`. There is **no automatic retry**, even for the harmless operations. A failed quarantine due to stale lease never permits completion or a new claim.
- **Lease limits:** Existing `claim/record_outcome/checkpoint` require correct worker and unexpired lease; both newly added methods enforce the same. The default lease is 30 s; no lease renewal is implemented. The one-second computation monitor is intentionally well below lease TTL but DB latency or process suspension can still expire the lease, in which case **manual reconciliation** is required.

No generic failure/retry/worker-succeeded state is introduced; `needs_reconciliation` is an honest *uncertain* state. The acknowledged cancellation transition **extends** the existing `cancelling` lifecycle to its existing terminal `cancelled` state without bypassing the lease or verifier.

## 5. Local developer walkthrough (not a deployment)

First follow [Core bootstrap](CORE_BOOTSTRAP.md), [PostgreSQL task persistence](TASK_PERSISTENCE.md) and [lifecycle CLI instructions](TASK_LIFECYCLE.md). Use only a local **disposable** development database at Alembic head `0002_trust_verification`, with a private `MAX_DATABASE_URL` and locally generated `MAX_API_BEARER_TOKEN`. Do not share either credential, use a production database, or expose MAX over LAN/internet. Do **not** store personal, financial or authentication information in task objectives.

With the local Core running, in a second local terminal (the same dev environment and token):

```powershell
python -m max_core.lifecycle_cli create
```

At the hidden prompt paste this **non-sensitive** exact JSON:

```json
{"op":"dev.add","params":{"a":2,"b":3}}
```

Copy the created task UUID from the CLI output. Use a local terminal with the same private dev environment/database:

```powershell
python -m max_core.bounded_worker run YOUR-TASK-UUID
python -m max_core.lifecycle_cli status YOUR-TASK-UUID
python -m max_core.lifecycle_cli history YOUR-TASK-UUID
```

A normal run prints a bounded unverified local result and reports the durable task state `awaiting_verification`, **not completed**. To inspect stale leases from **abruptly interrupted** test runs, after verifying that no other worker is active and the TTL has truly expired:

```powershell
python -m max_core.bounded_worker reconcile-expired
```

This command applies existing `TaskStore.reconcile_expired("owner", limit=50)` to stale *development* tasks, moving them to `needs_reconciliation`. It does **not** run/retry tasks. Any live local worker should be stopped before reconciling. No command in this walkthrough authorizes system shell commands **inside** a task; the local developer is simply launching a fixed Python module.

## 6. Tests and explicit exclusions

Review unit and PostgreSQL concurrency tests for: each operation, schema/unknown-ID rejection, typed numeric and output bounds, deterministic canonical output, worker claim races, owner scope, queued cancel, running cancel/lease-fenced acknowledgment, concurrent cancel versus outcome, late cancel rejection, expired lease, simulated interruption, timeout/fault quarantine, no blind retry, no verified completion without a separately injected EvidenceReader, and no operation-level shell/network/unrestricted-file calls.

**Validation cannot be claimed until CI for this PR actually runs.** Existing Core/Gateway/Trust/lifecycle tests must remain passing on Python 3.11 and 3.13 with PostgreSQL 17, Ruff, strict mypy and pip-audit. Live Windows/Arch owner machine testing and independent real-world observer are not performed by the GitHub-only assignment.

**Explicitly excluded:** custom scripts, arbitary Python or user-supplied expressions, subprocesses, shell, local file access, browser/network/desktop actions, model calls/FreeLLMAPI, autonomous agents, device control, payments, communication, vault/identity documents, remote endpoints, production deployment, background scheduling, plugins and general skill execution.

## 7. Rollback and release gates

No database migration or dependency is introduced. If unmerged, close this single PR. If merged, revert only this scoped worker, tests and two trusted internal store methods after reviewing any local in-flight dev tasks (do not delete real data). Already-recorded task/outbox entries are durable and are not rolled back automatically. **The owner alone reviews and merges**.

**Next gates (not part of Milestone 7):** trusted independent observer for local computations; stronger process-based time isolation before any nontrivial operation; dedicated locally authenticated worker identity/role before running outside a single-user development environment; comprehensive secrets/retention controls and owner-machine validation before any sensitive or remote use.
