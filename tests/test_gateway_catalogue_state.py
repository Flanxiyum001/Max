"""Offline catalogue diff, identity and atomic refresh regression tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from max_core.gateway.catalogue import (
    AvailabilityObservation,
    AvailabilityState,
    CapabilityEvidence,
    CapabilityMetadata,
    CapabilitySupport,
    CatalogueSnapshot,
    EvidenceSource,
)
from max_core.gateway.catalogue_discovery import parse_freellmapi_models
from max_core.gateway.catalogue_state import (
    CatalogueStateError,
    InMemoryCatalogue,
    compare_catalogues,
)


def catalog(
    *ids: str, provider: str = "vendor-x", observed: int | None = None
) -> CatalogueSnapshot:
    rows = [
        {
            "id": value,
            "object": "model",
            "owned_by": provider,
            "available": True,
            "execution_status": "ready",
            "context_window": 4096,
        }
        for value in ids
    ]
    return parse_freellmapi_models(
        {"object": "list", "data": rows},
        gateway_id="gateway-A",
        observed_at_epoch=observed,
    ).snapshot


def test_initial_snapshot_marks_all_routes_new_and_review_required() -> None:
    snapshot = catalog("a", "b")
    changes = compare_catalogues(None, snapshot)
    assert len(changes.added) == 2
    assert changes.removed == ()
    assert changes.review_required == changes.added
    assert not changes.unchanged
    assert changes.previous_revision is None


def test_identical_snapshot_is_noop() -> None:
    snapshot = catalog("a")
    changes = compare_catalogues(snapshot, snapshot)
    assert changes.current_revision == snapshot.revision
    assert changes.unchanged
    assert changes.review_required == ()


def test_add_and_remove_change_requires_review() -> None:
    previous = catalog("a", "b")
    current = catalog("a", "c")
    changes = compare_catalogues(previous, current)
    assert {r.model.model_id for r in changes.added} == {"c"}
    assert {r.model.model_id for r in changes.removed} == {"b"}
    assert {r.model.model_id for r in changes.review_required} == {"b", "c"}
    assert changes.metadata_changed == ()


def test_transient_readiness_is_reported_without_policy_promotion() -> None:
    previous = catalog("a")
    route = previous.routes[0]
    current = replace(
        previous,
        revision="other_revision",
        routes=(
            replace(
                route,
                availability=AvailabilityObservation(
                    AvailabilityState.EXHAUSTED, EvidenceSource.GATEWAY_REPORTED
                ),
            ),
        ),
    )
    changes = compare_catalogues(previous, current)
    assert changes.availability_changed == (route.identity,)
    assert changes.metadata_changed == ()
    assert changes.review_required == ()


def test_context_change_requires_owner_review() -> None:
    previous = catalog("a")
    changed = replace(previous.models[0], context_tokens=16384)
    current = replace(previous, revision="context_rev", models=(changed,))
    changes = compare_catalogues(previous, current)
    assert changes.metadata_changed == (previous.routes[0].identity,)
    assert changes.review_required == changes.metadata_changed


def test_capability_claim_change_requires_owner_review() -> None:
    previous = catalog("a")
    capabilities = CapabilityMetadata(
        claims=(
            CapabilityEvidence(
                "tools", CapabilitySupport.SUPPORTED, EvidenceSource.GATEWAY_REPORTED
            ),
        )
    )
    updated_model = replace(previous.models[0], capabilities=capabilities)
    current = replace(previous, revision="capabilities_rev", models=(updated_model,))
    changes = compare_catalogues(previous, current)
    assert changes.review_required == (previous.routes[0].identity,)


def test_region_change_requires_owner_review() -> None:
    previous = catalog("a")
    current = replace(
        previous,
        revision="region_rev",
        routes=(replace(previous.routes[0], region="eu-central-1"),),
    )
    changes = compare_catalogues(previous, current)
    assert changes.review_required == (previous.routes[0].identity,)


def test_observation_timestamp_change_is_not_route_definition_change() -> None:
    previous = catalog("a", observed=1)
    current = catalog("a", observed=2)
    assert previous.revision != current.revision
    changes = compare_catalogues(previous, current)
    assert changes.availability_changed == (previous.routes[0].identity,)
    assert changes.review_required == ()


def test_gateway_scope_mismatch_fail_closed() -> None:
    previous = catalog("a")
    different = parse_freellmapi_models(
        {
            "object": "list",
            "data": [{"id": "a", "object": "model", "owned_by": "vendor-x"}],
        },
        gateway_id="gateway-B",
    ).snapshot
    with pytest.raises(CatalogueStateError, match="catalogue_gateway_mismatch"):
        compare_catalogues(previous, different)


def test_same_revision_with_different_data_rejected() -> None:
    previous = catalog("a")
    current = replace(previous, models=(replace(previous.models[0], context_tokens=99),))
    with pytest.raises(CatalogueStateError, match="catalogue_revision_collision"):
        compare_catalogues(previous, current)


def test_snapshot_store_compare_and_swap() -> None:
    store = InMemoryCatalogue()
    initial = catalog("a")
    assert store.current is None
    first = store.replace(initial, expected_revision=None)
    assert first.added == (initial.routes[0].identity,)
    assert store.current == initial
    second = catalog("a", "b")
    changes = store.replace(second, expected_revision=initial.revision)
    assert {r.model.model_id for r in changes.added} == {"b"}
    assert store.current == second


def test_stale_update_never_replaces_snapshot() -> None:
    store = InMemoryCatalogue()
    first = catalog("a")
    store.replace(first, expected_revision=None)
    with pytest.raises(CatalogueStateError, match="stale_catalogue_revision"):
        store.replace(catalog("b"), expected_revision=None)
    assert store.current == first


def test_idempotent_update_with_exact_revision_and_content() -> None:
    store = InMemoryCatalogue()
    initial = catalog("a")
    store.replace(initial, expected_revision=None)
    unchanged = store.replace(initial, expected_revision=initial.revision)
    assert unchanged.unchanged
    assert store.current is initial


def test_revision_collision_does_not_mutate_store() -> None:
    store = InMemoryCatalogue()
    first = catalog("a")
    store.replace(first, expected_revision=None)
    bad = replace(first, models=(replace(first.models[0], context_tokens=777),))
    with pytest.raises(CatalogueStateError, match="catalogue_revision_collision"):
        store.replace(bad, expected_revision=first.revision)
    assert store.current == first


def test_concurrent_writers_only_one_wins_cas() -> None:
    store = InMemoryCatalogue()
    first = catalog("a")
    store.replace(first, expected_revision=None)
    next_one = catalog("a", "b")
    next_two = catalog("a", "c")

    def writer(snapshot: CatalogueSnapshot) -> str:
        try:
            store.replace(snapshot, expected_revision=first.revision)
            return "committed"
        except CatalogueStateError as exc:
            assert exc.code == "stale_catalogue_revision"
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(writer, (next_one, next_two)))
    assert sorted(outcomes) == ["committed", "rejected"]
    assert store.current in (next_one, next_two)


def test_snapshot_has_no_approval_authority() -> None:
    snapshot = catalog("a")
    assert not hasattr(snapshot, "trusted_policies")
    assert not hasattr(snapshot.routes[0], "approval")
    assert not hasattr(snapshot.routes[0], "inference_destination")
    assert not hasattr(InMemoryCatalogue(), "execute")


def test_empty_catalogue_is_valid_but_not_permission_to_execute() -> None:
    empty = catalog()
    store = InMemoryCatalogue()
    changes = store.replace(empty, expected_revision=None)
    assert changes.added == ()
    assert changes.review_required == ()
    assert changes.unchanged
    assert store.current == empty
