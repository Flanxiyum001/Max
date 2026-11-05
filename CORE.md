# CORE — Operating and Task Execution Model

## Task lifecycle

1. Understand the request and identify the intended outcome.
2. Retrieve relevant memory and project context.
3. Create a plan with dependencies and a definition of done.
4. Choose the smallest suitable agent/team and tools.
5. Check identity, permissions, scope, and approval requirements.
6. Execute in an appropriate isolated environment.
7. Observe outputs and record checkpoints.
8. Classify errors and recover within explicit budgets.
9. Independently verify the result.
10. Store useful outcomes and lessons under the memory policy.
11. Report what happened, evidence, remaining work, and approvals needed.

## Agent orchestration

The main agent owns the objective and delegates bounded tasks to specialist agents when parallelism is useful. Each sub-agent receives only the context and tools required for its assignment. Agents working on shared files must coordinate through ownership or merge/review boundaries.

## Tool selection

Use a capability registry to describe tools, operations, permissions, authentication state, availability, timeouts, rate limits, audit requirements, and authorised alternatives. MCP is supported alongside native API adapters and internal tools.

An alternative integration must never be used to circumvent a denied permission.

## Execution requirements

- Persistent task state and resumable checkpoints.
- Cancellation and deadline support.
- Timeouts, bounded retries, backoff, idempotency where relevant.
- Explicit handling of rate limits and partial success.
- Independent verification of external side effects.
- Clear distinction between reversible and irreversible operations.
