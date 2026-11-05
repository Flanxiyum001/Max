# MAX Milestone 9 — Stage A: Secure Model Contracts

**Baseline:** `max` at `226d95b50aade08f5620c8789d767748093c95b2`.

## Implemented (contracts only)

- `src/max_core/gateway/catalogue.py`: immutable schema-v1 identities and
  metadata for arbitrary gateway instances, providers, models and routes.
  A route is keyed by gateway/provider/model/route/dialect; a selection view
  carries the catalogue revision separately. A duplicate opaque route ID within
  the same gateway is rejected even if the model/provider differs.
- Capability support has explicit `unknown`, `supported` and `unsupported`
  states with provenance. New capability names require no provider-specific
  selection logic. Model names do not imply capability verification.
- Untrusted `CatalogueSnapshot` contains only discovery observations.
  `TrustedRoutePolicy` is a separate owner-reviewed contract with default
  `UNREVIEWED` and `UNKNOWN`. It must **never** be deserialized from the
  FreeLLMAPI catalogue.
- `Gateway.selection_metadata()` projects configured models through explicit,
  route-qualified policy bindings. It never copies the legacy API key or
  endpoint to `SelectionMetadata`. It does not select or execute a model.
- Existing gateway `eligible()`, streaming, cloud authorization, cost preflight
  and guarded fallback behavior are unchanged.

## Trust boundary and limitations

The legacy `ModelSpec` still contains `api_key` for the existing internal
transport. Only trusted gateway/adapter code may access that object; any future
selection or ranking layer must receive `SelectionMetadata` **only**.
Python dataclasses are not a process or privilege sandbox: arbitrary introspection
of trusted gateway objects is out of scope. Future live use requires an isolated
secret resolver and strict application integration boundary.

Trusted route policy requires independently reviewed evidence of destination
and a policy revision before approval. `VERIFIED_LOCAL` is still a trusted
configuration assertion, **not** verification of live upstream routing. No
discovery payload or `ready` observation may promote a route. Approval alone
does not execute anything; `Gateway.eligible()` must revalidate every request.

Missing price remains `None`; no advertised `ready` state proves health;
legacy capability labels are not automatically counted as verified evidence.
No endpoint is stored in selection metadata. No migrations or environment
variables are introduced.

## Local validation and deployment

New tests cover arbitrary provider count, same-name models, ambiguous routes,
missing references, unknown capability/pricing defaults, destination approval,
credential-free projections and revision validation. Existing gateway security
regressions must remain green. CI runs Python 3.11/3.13, Ruff, strict mypy,
PostgreSQL integration tests and pip-audit when a PR is created.

This stage does **not** authorize FreeLLMAPI requests, account credentials,
provider calls, model ranking/selection, database changes, new public endpoints,
production deployment, or a pull request before required validation.

## Stage B recommendation

Add bounded **offline fixture-only** discovery normalization, schema-v1 snapshot
refresh/diff, versioned route matching and provenance preservation. Unknown
routes must stay UNREVIEWED/UNKNOWN; never carry owner approvals across changed
upstream routes without an explicit review. Verify the deployed catalogue
contract and receive owner approval before any authenticated read-only GET.
