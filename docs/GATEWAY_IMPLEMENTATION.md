# MAX Model Gateway — Milestone 3 implementation

**Status:** First-party, internal Python gateway library. This is **not a deployed model service, public API endpoint, connected FreeLLMAPI instance, working autonomous agent or permission-enforcing tool runner**. Nothing is configured automatically or connected to any owner's provider accounts.

## What was implemented

The independent gateway lives under `src/max_core/gateway/` and exposes:

- `GatewayRequest` and `ModelSpec`: typed privacy classification, explicit approved endpoint/model, **independently configured inference egress destination**, region, capabilities, token ceilings, quoted token prices and per-request optional USD ceiling.
- `Gateway`: deterministic eligible-model routing (priority, explicit model/provider/region/capability allowlists, context-window check and cost preflight); **cloud denied by default**, `confidential`/`restricted` always denied for cloud inference, and unknown inference egress denied universally, **even when HTTP connects to loopback**. Neither local-only requests nor denied candidates reach the network.
- `HTTPAdapter`: OpenAI-compatible **Chat Completions** and Anthropic **Messages** transports, with different system-message placement and authentication headers. HTTPS is mandatory for remote endpoints; local endpoints must use explicit loopback hosts. No credential-bearing redirect follows.
- `normalize.py`: strict normalization of text, inert tool proposals, token usage and estimated reported costs. Invalid JSON/oversized tool arguments are rejected; missing token usage stays **unknown**, never forged as zero.
- `streaming.py`: bounded Server-Sent Events parsing for OpenAI data chunks and Anthropic event types; normalized start, text delta, tool proposal, usage and completion events, with monotonic event IDs. Incomplete/oversized frames are rejected.
- Safe fallback: **only** explicit non-streaming fallback after a 429 or 503 HTTP response; no fallback on authentication errors, malformed responses, timeouts, privacy denials or any streaming response.
- Errors use generic internal codes and never surface raw model/provider bodies or secret headers.

## Example: offline verification with no real accounts

Only a test fixture is registered, with an injectable HTTPX mock transport. This example makes **no external network requests**:

```python
import asyncio
from decimal import Decimal

import httpx
from max_core.gateway import (
    ChatMessage, Dialect, Gateway, GatewayRequest, HTTPAdapter, InferenceDestination, ModelSpec, Privacy,
)

async def example():
    def mocked_provider(req):
        return httpx.Response(200, json={
            "model": "demo-local",
            "choices": [{"message": {"role": "assistant", "content": "Hello, MAX."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        })

    spec = ModelSpec(
        provider_id="local-fixture", model_id="demo-local",
        dialect=Dialect.OPENAI_CHAT,
        endpoint="http://127.0.0.1:14000/v1/chat/completions",
        is_local=True, context_tokens=4096,
        inference_destination=InferenceDestination.VERIFIED_LOCAL,
        input_usd_per_million=Decimal("0"),
        output_usd_per_million=Decimal("0"),
    )
    request = GatewayRequest(
        request_id="demo-001", messages=(ChatMessage("user", "Hello"),),
        privacy=Privacy.PUBLIC, max_input_tokens=100, max_output_tokens=50,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(mocked_provider)) as client:
        result = await Gateway([spec], HTTPAdapter(client)).complete(request)
        print(result.content)

asyncio.run(example())
```

This is an **internal library example**, not a new `/v1/chat` endpoint. The existing local MAX Core API and PostgreSQL task routes remain unchanged.

## Explicit privacy and billing policy

1. **Cloud inference** (including a loopback proxy that forwards to cloud) requires `cloud_allowed=True` **and** a non-negative `max_cost_usd` **and** both input/output prices configured. If any is absent, admission fails closed. Transport locality is a separate field and never grants cloud authorization.
2. `CONFIDENTIAL` and `RESTRICTED` can use only **explicitly verified local inference**, irrespective of cloud opt-in; `local_only=True` overrides other routing preferences. A model with `inference_destination=UNKNOWN` is never eligible. The requester can narrow allowed model/provider/region lists.
3. Admission compares declared input/output maxima against configured context windows and configured USD pricing. The input maximum must cover a conservative UTF-8 message envelope. **This is not an accurate tokenizer, billing guarantee, reserved budget, global spend limit or protection from a provider charging for a failed request.** Actual usage is checked after the response when reported; in a stream, output may already have been emitted before an over-budget report arrives. A future milestone needs trusted per-model token counting, billing reconciliation and global ledger/quotas before production.
4. Unknown actual usage or unknown computed cost remains `None`. Unknown cloud pricing fails admission; unknown inference destination is **not free**, even when the endpoint is loopback. Never claim billing accuracy or token balance without the provider's authoritative account data.
5. Only text messages and inert tool proposals are supported here. There is **no image/audio support, JSON schema mode, execution of tools, server-side web tools, OpenAI Responses API, provider capability probing, permanent key management or model quality ranking**.
6. Configured endpoints and inference-destination assertions must be supplied by **trusted local owner configuration**: URL validation controls scheme/host, rejects user-info/query credentials, and the adapter forbids redirects, but this is **not a DNS-rebinding/SSRF firewall or runtime proof of upstream routing**. A dedicated outbound network policy and audited provider egress remain later requirements.

## OpenAI/Anthropic compatibility boundary

The adapters implement a conservative **Chat Completions** and **Messages** subset, respectively—not a promise that all 'OpenAI-compatible' proxies have identical behavior. Some providers use alternative output-limit fields or streaming usage options. An exact-provider smoke test must establish dialect, auth headers, body schema, streaming, tool semantics, billed model ID and account policies before enabling it. The owner has selected **`tashfeenahmed/freellmapi` as the intended project**, but the actual running instance, release, upstream destinations, credentials, pricing, and protocol behavior remain unverified.

Primary documentation: [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat/create), [Anthropic Messages API](https://docs.anthropic.com/en/api/messages), [Anthropic streaming](https://docs.anthropic.com/en/api/messages-streaming). Decisions and desired capability matrix: [MODEL_GATEWAY.md](MODEL_GATEWAY.md).

## Testing and quality gates

Run these after installing `pip install -e '.[dev]'` in a private local virtual environment:

```sh
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src/max_core
python -m pip_audit --progress-spinner off
```

Mock-provider tests explicitly cover both wire formats, tool-call JSON validation, authorization-header isolation, no credential forwarding on redirects, model-ID mismatch, malformed bodies, size limits, missing usage, private/local-only routing, cost ceilings, 429-only guarded fallback, no streaming fallback, truncated streams, and stream tool fragments. The existing GitHub CI matrix runs Python 3.11/3.13 and PostgreSQL migrations/integration tests as well. **Record the actual GitHub results in the PR; do not claim a successful live provider smoke test.**

## Security and rollback

This PR changes no migrations, cloud service, production DB, existing bots or remote authentication. No real credentials are committed or requested. Keep the Phase 1 API bound to loopback; no public endpoint is added.

To undo, revert/close the PR. No data migrations or external side effects require rollback. The next owner-reviewed milestone is **MAX Trust and independent verification**, so model-produced tool proposals cannot become privileged actions without typed checks, user approval and audit.

## Milestone 9 Stage 2 — explicit ultimate-inference destination (owner review branch)

`ModelSpec.is_local` retains its existing **HTTP endpoint locality** meaning, not the location of inference. The new `ModelSpec.inference_destination` enum has a **fail-closed default** of `InferenceDestination.UNKNOWN`. Unspecified existing model registrations are intentionally ineligible until their upstream path is separately reviewed and classified; **this is a deliberate safe compatibility break**. Three values are supported:

| Classification | Meaning | Admission |
|---|---|---|
| `UNKNOWN` | Proxy/router/actual inference location is unverified | **Never eligible** even on loopback or with an advertised zero price |
| `VERIFIED_LOCAL` | Trusted owner-reviewed direct local inference path with **no cloud forwarding** | Only for explicitly configured loopback HTTP endpoint; cloud opt-in unnecessary. Manual verification is a config assertion, not cryptographic attestation |
| `CLOUD` | External inference or any proxy forwarding prompt content to cloud | Explicit `cloud_allowed=True`, non-`local_only`, privacy other than confidential/restricted, non-null request ceiling and both quoted input/output prices. All checks rerun before fallback |

For a FreeLLMAPI instance on `127.0.0.1` that may relay requests upstream, set `CLOUD` for explicitly cloud-routed models or leave `UNKNOWN` until routing is independently reviewed. **Never mark an unverified proxy `VERIFIED_LOCAL` merely because its HTTP address is loopback.** The app does not inspect the FreeLLMAPI deployment, pin upstream egress routes, authenticate forwarded model identity, classify prompt sensitivity for callers, or validate live prices: these are mandatory later security gates.

Redaction scope is limited to ordinary `repr()`/`str()` of `ChatMessage`, `GatewayRequest`, `GatewayResponse`, `ToolProposal`, `StreamEvent` and existing `ModelSpec`, plus suppressed exception chaining for expected HTTPX and malformed-provider/tool-JSON failures. **Explicit serialization such as `dataclasses.asdict`, arbitrary caller logging, introspection of exception `__context__`, third-party transport diagnostics and compromised infrastructure remain outside this guarantee.** Do not log gateway payload objects or attach arbitrary exception details to telemetry. No credential loader or live FreeLLMAPI connection is introduced. Existing HTTPX mock tests remain the validation boundary.
