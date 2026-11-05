# MAX model gateway

**Status:** Proposed internal contract.

MAX owns a vendor-neutral request interface. Provider adapters are optional interchangeable implementations; model output is never permission to execute a tool.

## Request
`request_id`, `task_id`, `privacy_class`, `messages`, `capabilities`, `deadline`, `max_output_tokens`, `cost_budget`, `stream`, `cancellation_id`.

## Response
`provider_id`, `model_id`, `output`, `tool_proposals`, `usage`, `latency_ms`, `finish_reason`, `error_class`, `trace_id`. Tool proposals must be schema-validated and independently authorized.

## Routing
Use capability match, privacy boundary, latency, budget, availability and explicit local-only policy. Do not silently send local-only content to cloud fallback. Track per-provider health and circuit-breaker state. Separate authentication material from prompts and logs.

## Candidate adapters
Local model runtime and HTTPS model endpoints, including OpenAI-compatible and Anthropic Messages-style formats when documented by the provider. Streaming, tools, JSON output, usage accounting and error mapping need contract tests per adapter; format compatibility does not prove feature compatibility.

## Acceptance tests
Deterministic mock provider; streaming cancellation; malformed tool calls; rate-limit fallback; budget exhaustion; local-only denial; provider outage; missing usage; secret redaction; retry idempotency. Provider availability, account quota and cost are unverified until tested with approved credentials.
