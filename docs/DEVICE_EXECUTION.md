# MAX device execution

**Status:** Proposed, no remote control enabled.

An enrolled node makes an authenticated outbound connection and accepts only scoped signed jobs. Device-local policy decides whether to execute. Each job declares target device, allowed operations, workspace, deadline, resource limits, approval, audit and cancellation.

Use isolated project workspaces and a minimal command policy. No unrestricted model shell, hidden credential extraction, security bypass or arbitrary desktop takeover. Device wake and shutdown require owner policy, idle/saved-work checks and verified state.

External side effects must be reconciled before retries. An offline device may hold safe work, but high-risk operations must not silently execute after reconnection.

**Tests:** revoked device, replay, unauthorized command, lost network, restart checkpoint, partial side effect, unsafe shutdown, isolated workspace and audited result.
