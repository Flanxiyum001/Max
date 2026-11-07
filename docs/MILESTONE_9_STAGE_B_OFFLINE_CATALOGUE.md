# MAX Milestone 9 — Stage B: Offline Dynamic Model Catalogue

**Base commit:** `9385eb39347618ffd354aed5c79d6b9b54d6fecf` (`max`, Stage A owner-merged).  
**Scope:** Offline, typed catalogue parsing, identity, diff, and snapshot staging.
**Not included:** Live FreeLLMAPI access, credentials, runtime discovery scheduling,
provider health probes, route approval, model ranking, model inference, database
migrations, new APIs, deploys, or automatic fallback.

## Architecture

The existing Stage A contracts in `gateway/catalogue.py` separate untrusted
catalogue observations from owner-reviewed `TrustedRoutePolicy`. Stage B adds:

- `gateway/catalogue_discovery.py`: strict bounded importer for **already
  decoded** FreeLLMAPI-shaped `GET /v1/models` JSON. It makes **zero HTTP
  calls** and supports an arbitrary number of provider IDs (bounded to 4096
  model records per snapshot), not a fixed list of current providers.
- `gateway/catalogue_state.py`: immutable version diff and thread-safe,
  compare-and-swap **in-memory** snapshot staging with stale-write detection.
  No persistence or policy mutation.
- Offline tests covering malformed values, virtual router aliases, ambiguous
  model names, identity stability, untrusted metadata, quotas/size bounds,
  provenance, revision conflicts, concurrency, and routing-security boundaries.

The new modules are deliberately pure metadata functions; they are not invoked
by `Gateway.complete()`, `Gateway.stream()`, or `Gateway.eligible()`.
The current privacy/cost admission, fail-closed UNKNOWN destination, and 429/503
fallback policy remain unchanged.

## Public source reference and actual runtime unknowns

Reviewed the public upstream source (not a live instance):
`tashfeenahmed/freellmapi` commit
`e701dc898f870d6a127c4a309eda981c520d54d7`:

- `server/src/routes/proxy.ts`: `GET /v1/models` returns
  `{"object":"list","data":[...]}`. Concrete rows advertise an
  `id`, `owned_by`, `context_window`, `available`,
  `execution_status` and `supported_parameters`.
- `server/src/services/model-listing.ts`: model rows can be grouped and
  `ready` does **not** imply an independently successful health probe.
- Virtual `auto`, `fusion`, `auto:<profile>`, and Claude-family alias rows
  are router *aliases*, not guaranteed upstream inference routes.

Neither this public revision nor the parser proves the owner's deployment
version, upstream routes, provider identity, model health, price, retention, or
privacy compatibility. Review deployed behavior and explicit route confinement
under a separate Stage C authorization.

## Trust rules

1. **Never trust advertised readiness as approval.** Parsed readiness is
   `GATEWAY_REPORTED`, even if `ready`. Missing, unrecognized, and
   contradictory statuses become `UNKNOWN`.
2. **Never infer local inference** from gateway address, model name, provider
   name, availability, or claimed zero price. `DiscoveredRoute` has no
   destination or approval fields. `TrustedRoutePolicy` stays independent.
3. **Never synthesize pricing.** The model list provides no trusted USD quote.
   Both token rates remain `None`; structured-output/tools claims are
   gateway-reported only, and vision/reasoning stay UNKNOWN unless separately
   verified.
4. **Never forward unknown raw fields.** API keys, endpoints, headers,
   credentials, spending permissions, and arbitrary JSON extensions are dropped.
   Errors use fixed codes and suppress underlying payload exception chains.
5. **Stable route identity.** Compute a SHA-256 route ID from canonical
   `[gateway, provider, model, dialect]`. Names are not an execution grant;
   the route ID is unique across providers/models/dialects within a gateway.
   Identical wire-model IDs under different advertised providers are reported
   as ambiguous instead of implicitly selecting a winner.
6. **No invisible trust promotion.** Adding or changing route identity, context,
   declared capability evidence, region, or pricing is flagged for review.
   Availability-only changes are separately reported; no policy is changed.
7. **No rollback by racing readers.** `InMemoryCatalogue.replace()` checks
   an expected revision and atomically swaps only a complete valid snapshot.
   Same revision with changed data is rejected.
8. **Resource bounds.** Validate a max 2 MB encoded payload, at most 4096
   entries, at most 64 parameter names per model, bounded identifiers,
   and JSON-serializable values. Reject malformed snapshots as a whole.
   Do not log raw data.

### A deliberate limitation: FreeLLMAPI aggregation

Some `owned_by: freellmapi` rows are aggregated routes whose upstream can
change behind a stable public ID. Stage B retains such informational rows
(except documented router/Claude aliases) but never marks them executable.
A generated route ID is *not* proof that upstream egress is unchanged.
Stage C must verify upstream route pinning/confinement and revalidate on drift.

## Programmatic example (offline fixture only)

```python
from max_core.gateway.catalogue_discovery import parse_freellmapi_models
from max_core.gateway.catalogue_state import InMemoryCatalogue

decoded_fixture = {
    "object": "list",
    "data": [{
        "object": "model",
        "id": "example-model",
        "owned_by": "example-provider",
        "context_window": 8192,
        "available": True,
        "execution_status": "ready",
        "supported_parameters": ["tools"],
    }],
}
imported = parse_freellmapi_models(decoded_fixture, gateway_id="max-local")
store = InMemoryCatalogue()
changes = store.replace(imported.snapshot, expected_revision=None)
assert len(changes.review_required) == 1
assert store.current == imported.snapshot
# No model is approved or called; a model's presence does not imply eligibility.
```

## Validation gates

Against the dedicated feature branch, run in CI on Python 3.11 and 3.13:

```bash
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src/max_core
python -m alembic upgrade head
python -m pytest -q
python -m pip_audit --progress-spinner off
```

The PostgreSQL integration suite still runs even though Stage B adds no
schema migrations. Redaction and Stage A security tests must remain green.
Report actual results from the PR workflow rather than assuming success.

## Recommended next stages (not implemented)

**Stage C:** Verify the owner's *specific* FreeLLMAPI deployment and obtain
explicit authorization for bounded, authenticated, **read-only** catalogue GET
access. Add response timeout/size limits and egress policy; retain snapshot
validation. Never inspect credentials in selector code.

**Stage D:** Introduce task requirements and capability-based selection of
independently approved, gateway-admissible routes. Require cost reservations,
verified upstream confinement, exact route-qualified identity and owner policy.
Dynamic discovery must never modify the trusted execution registry.
