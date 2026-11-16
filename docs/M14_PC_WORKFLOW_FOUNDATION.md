# M14 — Personal PC workflow: authenticated policy foundation

**Status:** Offline, testable contracts only. **No live Wake-on-LAN sender, device agent,
remote API, OS suspend, Git subprocess/container executor, phone dispatch, GitHub
write adapter, or persistent signed job queue has been deployed or enabled.**

## What this milestone actually changes

- `magic_packet` creates precisely formatted WoL bytes for a valid unicast MAC
  without opening a socket. Transmission is a separate owner-approved capability.
- `HeartbeatRegistry` checks device-specific 256-bit-minimum HMAC keys, device
  enrollment, timestamp skew, nonce replay, monotonic receive clock and TTL. It
  denies unknown/offline/stale/unauthorized nodes. Its in-memory replay cache is
  **not** sufficient for reconnect/restart authorization or production use.
- `PCWorkflow` enforces the order: owner-authorized wake request → authenticated
  heartbeat → observed rootless/container isolation → draft PR and exact head SHA
  → six independently observed CI checks → report → separately authorized
  suspend eligibility → finish. Failure is not treated as success.
- `IsolationEvidence` and `CheckEvidence` must come from **trusted host
  observers**, never model output, a PR description or the remote worker itself.
  These typed values do not themselves cryptographically attest a host.
- A user-active desktop, unsaved work, a running job, stale heartbeat or missing
  explicit authorization blocks suspend eligibility. Reporting and finishing
  **without** suspending remains possible.

## Next implementation stages (separate owner-reviewed PRs)

1. **Authenticated Core ↔ node channel:** outbound mTLS or scoped signatures over
   Tailscale; hardened enrollment and revocation; a persistent nonce/idempotency
   store, lease expiry and a heartbeat independent of worker/model output. Do
   not mistake Tailscale connectivity for application authorization.
2. **WoL adapter:** only an owner-approved, allowlisted local relay can send
   magic packets to an explicitly enrolled MAC/interface/broadcast destination.
   Test BIOS/NIC/UEFI WoL settings and Tailscale subnet/wake-relay topology;
   a VPS cannot magically L2-broadcast onto a sleeping home LAN.
3. **Coding sandbox:** enrolled, host-local rootless container with verified
   isolation, constrained CPU/memory/disk/network/credentials, allowlisted repo
   and workspace. `git worktree` alone is **not** a sandbox. No model-provided
   arbitrary shell commands on the host; no mounting SSH agents or personal
   credentials into the container.
4. **GitHub adapter:** short-lived repo-scoped GitHub App token; branch and
   **draft** PR only; trusted GitHub Actions check runs collected from the exact
   head SHA (including missing/skipped/failed checks); source and egress review.
   Never merge or deploy automatically.
5. **Evidence & recovery:** durable state machine, crash-resume reconciliation,
   signed event log, bounded retries, cancellation, PR/evidence report to the
   owner, phone authentication and device-specific approval. Power transitions
   require recent owner confirmation, fresh idle/saved-work status and verified
   OS shutdown/suspend readiness. Never force power-off.

## Security decisions / rollback

Wake and suspend are tier-2 external/device actions. MAX Trust currently only
implements the hardcoded `task.status` read-only capability, so **this
milestone does not register wake, coding or shutdown permissions**. The
`owner_authorized` argument is a policy input for trusted adapters, not an
approval mechanism: passing `True` without real MAX Trust verification would
be an authorization bypass. Production integrations must fail closed until the
tier-2 exact-action, short-lived, single-use, task/device-bound grants exist.

Do not insert enrollment keys into source, environment examples, tests, logs,
PRs or model prompts. Provision them in an OS secret store and rotate on
device revocation.

Rollback: revert this feature PR; it adds no migration, CLI, API route, secret,
OS hook or background service.

## Acceptance checklist

- [x] Deterministic packet bytes without network I/O
- [x] Replay, signature, stale-device and invalid-MAC rejection covered by tests
- [x] Missing/wrong-head CI, fake isolation and unsaved-work denial covered by tests
- [ ] Run CI and verify all checks against the PR head
- [ ] Enrolled physical-device signed heartbeat and revocation
- [ ] Approved WoL relay wakes physical PC over the actual home network
- [ ] Bounded sandboxed coding job and authenticated GitHub draft PR
- [ ] Exact SHA Actions verification and owner-visible report
- [ ] Real safe-suspend checks on Windows and Hyprland
- [ ] Crash recovery and end-to-end negative security test

No item marked as device-validated is fulfilled by a fake/injected callback.
