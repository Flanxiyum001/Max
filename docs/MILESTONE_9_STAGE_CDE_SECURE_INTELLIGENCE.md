# MAX Milestone 9 — Stages C/D/E: Verified Discovery Boundary and Task Intelligence

**Base:** `max` @ `52f7ad5b6809826fcf527f9c22bec7b3ebd17702` (Stage B owner-merged).  
**Status:** Implementation for review, not a deployment authorization.

MAX now has an **opt-in trusted catalogue transport**, a **credential-free
task-aware planning interface**, and a **reviewed offline performance-evidence
contract**. These layers are modular and do not change existing completion,
streaming, tool execution, public APIs, or runtime selection.

## Stage C — guarded catalogue access

`src/max_core/gateway/catalogue_reader.py` introduces:

- `ReadOnlyCatalogueScope`: disabled by default. A caller needs an explicit
  owner approval revision and `enabled=True` before `fetch()` may access a
  catalogue. No MAX configuration path or scheduled job enables it.
- Exact `https://<reviewed-host>/v1/models` URL, no URL credentials, query,
  fragment, localhost/local-domain hosts or private literal IPs. Never follow
  HTTP redirects. Revalidate hostname routing externally at deployment, since
  Python URL validation cannot prevent DNS rebinding or an altered upstream.
- HTTP GET only, bounded timeout and decoded body size, strict JSON content type
  and safe fixed error codes. No response headers, raw body, transport exception
  or credential material becomes selection metadata or an error string.
- A **trusted transport-only** authorization callback. Credentials stay outside
  the catalogue, ranking, evaluation and task-plan types. The callback is not
  invoked while the scope is disabled. No real credentials are configured.
- Existing `parse_freellmapi_models()` performs additional structural checks
  and deliberately drops untrusted approval/destination/price claims.

The client is not wired to MAX startup, scheduled discovery, Railway or an
inference endpoint; tests use `httpx.MockTransport` exclusively. The owner's
real deployed FreeLLMAPI gateway has **not** been contacted or verified.
Live access, even read-only, requires separate owner-approved host, egress,
credential boundary, version review and DNS/network restrictions.

## Stage D — trustworthy advisory task plan

`Gateway.plan_task(request, bindings, *, catalogue_revision, task,
verified_capabilities, evaluations=None, exact_route=None)` is a new
**read-only advisory API**. It does not invoke the adapter or run the resulting
plan. It performs the following trusted-boundary steps:

1. Reject legacy `request.allowed_models` because it contains unqualified
   model IDs which can refer to different providers.
2. Produce only owner-approved `SelectionMetadata` via the existing
   `Gateway.selection_metadata()` contract. Validate full route bindings.
3. Revalidate each candidate through **`Gateway.eligible(request)`** using
   trusted execution configuration. This enforces privacy, local/cloud permission,
   region, context and conservative cost ceilings. Secrets never leave the
   gateway in the resulting candidate views.
4. Require independently owner-reviewed/protocol-tested capabilities for the
   task; advertised/provider-reported claims do not qualify.
5. Optionally filter to exactly one qualified `RouteIdentity` using
   `exact_route`, without falling back silently to another route.
6. Rank admitted, credential-free views and return immutable route IDs,
   catalogue revision and sanitized summary metrics, **not credentials,
   endpoints, prompts, adapters or execution handles**.

Supported task *planning*: chat, coding, reasoning, research (text research
planning only), inert tool proposals. Real web research, tool execution, vision,
audio and structured-output guarantee are **not implemented**.
Structured extraction, vision and audio remain explicitly blocked until
their transport, output validation and independent capability verification
exist. Tool planning requires `GatewayRequest.required_capabilities` to
include `tools`, and tools remain inert proposals.

A route with zero valid evidence or one excluded by cloud opt-in, restricted
privacy, cost ceiling, unknown inference destination, owner disablement, or
request context returns an empty candidate plan (not an execution grant).
`Gateway.complete()` and `Gateway.stream()` retain existing logic.

## Stage E — audited, offline quality evidence

`src/max_core/gateway/evaluation.py` defines frozen, credential-free
`QualityObservation` and `OfflineEvaluationBook` with:

- Qualified route, task class and exact catalogue revision.
- Reviewed or protocol-tested provenance only; never a gateway-advertised score.
- Validated decimal quality score from 0–1, sample count >=5, and measured
  p95 latency range.
- Duplicate record rejection; stale revision measurements never influence
  current ranking.
- Deterministic preference for reviewed task quality, then p95 latency,
  owner priority and stable fully-qualified identity. Untested routes do not
  receive made-up quality scores.
- **No telemetry collection, automatic evaluation, web search, self-learning,
  model execution, or benchmark-provider calls.** Supplying real evidence in
  a future implementation requires owner-controlled evaluation and privacy
  review; quality scoring is advisory, not an execution authorization.

## Security review and remaining gaps

- `InferenceDestination.UNKNOWN` remains ineligible; Cloud requires
  `cloud_allowed=True`, PUBLIC/INTERNAL privacy and a known price/budget.
  No Stage C/D/E module can promote those permissions from discovery metadata.
- Live FreeLLMAPI `auto`, `fusion` and aggregate routes can route through
  other upstreams. Without independently verified, pinned upstream and route
  confinement, there is **no justified VERIFIED_LOCAL attestation**.
- `ModelSpec` still carries legacy transport keys internally; Stage C's
  callback is trusted I/O only and cannot be passed into `TaskPlan`.
- The advisory plan cannot atomically reserve spending or guarantee provider
  quotas, availability, latency, tool safety or billing reconciliation.
  These require separately scoped implementation and integration tests.
- Current `GatewayRequest.allowed_models` is legacy unqualified; planner
  refuses it, but existing direct gateway callers remain unchanged for API
  compatibility. Do not interpret a plan as authorization for those callers.
- A URL allow-list alone cannot defend against DNS-rebinding or legitimate
  proxy routes that unexpectedly use cloud inference. Use egress-layer network
  enforcement and owner-reviewed destination classification at deployment.

## CI gates

Run all existing project checks with Python 3.11 / Python 3.13 and PostgreSQL 17:

```bash
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src/max_core
python -m alembic upgrade head
python -m pytest -q
python -m pip_audit --progress-spinner off
```

New tests exercise mock-only transport, negative security cases,
route-qualified overrides, privacy, budget, tools, unsupported modalities,
reviewed-evidence validation and stale revisions. **Do not claim CI green
without checking the actual GitHub Actions run.**

## Next approvals needed before real use

1. Review this implementation and ensure the deployed gateway's actual
   security contract, proxy egress, redirect/DNS policy and upstream pinning
   meet owner policy.
2. Authorize the exact hostname, credential-source isolation and network
   controls for read-only catalogue GETs. No configuration is currently enabled.
3. Implement audited budget reservation, quotas, concurrency controls and
   explicit execution binding before moving from **advisory planning** to
   model-driven task execution.
4. Provide benchmark provenance and owner review before enabling production
   quality-based task ranking or autonomous feedback loops.
