# MAX model gateway

**Status:** Proposed typed design, not a deployed proxy or verified account. MAX Gateway is a **MAX-owned policy and normalization service**, even when routing through an external proxy. The owner has now selected **`tashfeenahmed/freellmapi`** and requires **both local and cloud inference**, using two API formats. The specific deployment and version remain **unconfirmed**. The public [tashfeenahmed/freellmapi API reference](https://github.com/tashfeenahmed/freellmapi/blob/main/docs/en/api/01-rest-api.md) **documents** OpenAI-compatible Chat Completions and Anthropic-style Messages interfaces. Earlier research also linked a **different repository/account**, [lov0u/freellmapi](https://github.com/lov0u/freellmapi/blob/main/docs/api.md). Selecting the repository establishes project identity only, not an owner's live instance, release, actual compatibility, inference egress or trustworthiness. See [PHASE0_AUDIT.md](PHASE0_AUDIT.md).

## Responsibility boundary

Core sends an authorized model request (data classification, task, model needs, budgets, deadline, cancellation). Gateway chooses an approved adapter; **model output can propose tool calls but never authorize execution**. MAX Trust alone validates schemas, owner/device scope, approval, audit and effect risk. No provider/adapter can broaden local-only policy, even in failure.

## Canonical request fields

- `request_id`, `owner_id`, `task_id`, `session_id`, `privacy_class` (public/internal/confidential/restricted).
- `messages` as explicitly typed content parts; provenance/source references and content hash, no raw unrestricted vault data.
- `required_capabilities`: text/vision/tools/json/audio, context length, streaming; `allowed_models`/`allowed_regions`.
- `max_input_tokens`, `max_output_tokens`, `max_spend` in known units, token and request budgets, `deadline_ms`, `cancellation_id`, `idempotency_key`.
- `tool_definitions` are descriptions for proposal-generation, not permission tokens.

## Canonical response and streamed events

Response: `provider_id`, `model_id`, `request_id`, `output_parts`, `tool_proposals`, `finish_reason`, `token_usage` (nullable), `estimated_cost` (nullable), `latency_ms`, `provider_trace_id`, `error_class`. Stream normalized events `start`, `content_delta`, `tool_proposal_delta`, `usage`, `complete`, `cancelled`, `error`, monotonic offsets for reconnect/cancellation. Unknown usage/cost stays unknown; do not record fabricated zeros.

## Transport adapters — both required at contract level

| Capability | OpenAI-compatible Chat Completions | Anthropic Messages style | Contract test |
| --- | --- | --- | --- |
| Text turns | role-based messages, choices | messages + content blocks | Preserve speaker/turn ordering |
| System instruction | commonly in role messages | separate top-level system field | Same semantic canonical system, never mix external data into instruction |
| Tools | tool/function proposal shapes | tool_use/tool_result content blocks | Schema validation, IDs, partial streaming and multi-tool ordering |
| Stream | data-event chunks with finish reason | event types/start/delta/end | Deterministic normalized event ordering and terminal state |
| Usage | prompt/completion counters if returned | input/output counters if returned | Missing/partial usage is not zero |
| JSON/schema mode | provider-dependent | provider-dependent | Feature probe, reject unsupported rather than silently degrade |
| Vision/audio | model/provider specific | model/provider specific | Explicit capability registry; unsupported -> typed error |

**Wire compatibility is not capability compatibility.** Proxies may emulate formats unevenly, strip or synthesize usage, or lack robust tool/JSON streaming. Provider-specific conversion belongs in the adapter with golden fixtures, not MAX Core.

## Routing and failure model

1. Authenticate owner and classify prompt, memory attachments and allowed region. Respect explicit local-only mode. **Never infer local inference from a localhost/loopback connection**; require a separately reviewed inference-destination classification.
2. Filter candidates by enabled provider, **verified local vs cloud vs unknown ultimate inference destination**, data category, model permissions, capability and per-request token/currency budget. **Unknown inference destination always denied.** Cloud-forwarding loopback proxies are cloud for authorization, privacy and price checks. No permitted candidate -> fail closed.
3. Rank allowed candidates by current health, measured latency, configured price and quality/task evaluation; no secret sent before privacy decision.
4. Attempt request with per-provider deadline and cancellation. For streaming, record whether the stream already produced user-visible or tool-proposal events.
5. Retry only safe idempotent pre-effect requests within finite budget; after partial stream require correlation and explicit semantics to avoid combining incompatible answer fragments.
6. Fall back only to a provider with **equivalent privacy, capability and approval envelope**; no cloud fallback for local-only.
7. Record model/provider actual identity, usage if known, reason/circuit-breaker, redacted trace and success evidence.
8. A model hallucinated 'I called the tool' is not a tool result.

Errors: `auth_failure`, `rate_limited` (retry-after), `invalid_request`, `unsupported_capability`, `invalid_tool_proposal`, `budget_exceeded`, `privacy_denied`, `timeout`, `provider_unavailable`, `stream_interrupted`. Never retry auth or denied-policy failures via a different provider as a workaround.

## Credential, usage and retention rules

Keys are injected from a trusted OS-managed secret store and rotated separately. Do not send owner secrets to models or write headers/payloads unredacted to logs. Use provider-level spend limits, redacted telemetry and a privacy-class-aware output journal. First-party account quotas, 'free tokens', uptime, model access, geographical routing and stored prompt retention **remain unverified** until actual provider dashboard/API and terms review.

## Adapter selection gate

1. Owner selected source `tashfeenahmed/freellmapi`; independently confirm the actual deployment, version, model routes and each ultimate inference destination before authorizing any request.
2. Verify exact API dialects, authentication, URL, streaming/tool/JSON behavior with mock fixtures.
3. Pin version/license; inspect dependency security, network access and storage of prompts and keys.
4. Obtain explicit owner permission for any sandbox credential smoke test (not a Phase 0 action).
5. Validate direct fallback providers and local Ollama separately.
6. Bench actual model task quality; no 'best model' assertion without evaluation.

## Deterministic acceptance fixtures (future)

- OpenAI response and Anthropic content-block mapping; both streaming and nonstreaming.
- Cancel mid-token and mid-tool proposal; no leaked side effect.
- Invalid/oversized/malicious tool JSON -> proposal rejected before MAX Trust.
- Missing usage, price unknown, budget exhausted, unsupported tool mode.
- Local-only prompt and disabled provider; no network request to cloud.
- Provider 429/5xx and partial stream; finite retry and stable task state.
- Wrong system/tool role mapping rejected; prompt-injection content cannot change developer instructions.
- Provider selection and fallback auditable with actual model/provider IDs.

References: [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md), [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md), [THREAT_MODEL.md](THREAT_MODEL.md), and owner [DECISIONS.md](DECISIONS.md).

## Milestone 3 implemented subset (2026-11-05)

The first-party internal gateway contract is **merged into `max`** under `src/max_core/gateway/`; usage, stream, tool proposal and privacy-negative tests use HTTPX's mock transport. **No provider is connected and no public API/production deployment exists.** The gateway supports two text chat wire formats, bounded SSE streaming, fail-closed cloud authorization, quoted-price preflight and restricted fallback. This is **a narrower subset** than the full architecture described above: neither vision/audio, schema-mode probing, global cost ledgers nor trusted MAX tool authorization are implemented. Refer to [GATEWAY_IMPLEMENTATION.md](GATEWAY_IMPLEMENTATION.md) for accurate scope and implementation-level limitations.

## Milestone 9 Stage 2 — approved project, unverified deployment

The owner selected [`tashfeenahmed/freellmapi`](https://github.com/tashfeenahmed/freellmapi), with both local and cloud inference required. This resolves the repository *name* choice, **not** installation/version, proxy routing, hosting, provider models, model catalog availability, API authentication, usage correctness, billing, or outbound firewall integrity. No live provider integration is authorized in this stage.

The implemented model policy separates `is_local` (**where HTTP connects**) from `inference_destination` (**where actual inference is asserted to run**) with values `VERIFIED_LOCAL`, `CLOUD`, `UNKNOWN` (default). UNKNOWN is ineligible; VERIFIED_LOCAL is limited to explicitly configured loopback endpoints and an owner-reviewed no-cloud-forwarding path; CLOUD includes loopback proxies and requires explicit cloud opt-in, known quoted prices, a request cost ceiling, and non-sensitive classification. Existing unclassified configurations must be explicitly upgraded instead of silently becoming local. All fallback candidates undergo the same gates; no provider claims or route metadata can self-authorize egress. See [GATEWAY_IMPLEMENTATION.md](GATEWAY_IMPLEMENTATION.md) and [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md).
