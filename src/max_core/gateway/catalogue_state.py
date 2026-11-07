"""Offline versioned catalogue diff and atomic in-memory snapshot staging.

No database, background polling, model selection, trust-policy mutation, or
credential access. Every result is advisory metadata, not execution approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from max_core.gateway.catalogue import (
    CatalogueSnapshot,
    DiscoveredRoute,
    ModelIdentity,
    ModelMetadata,
    RouteIdentity,
)


class CatalogueStateError(ValueError):
    """Safe conflict code without catalogue contents or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _route_sort(identity: RouteIdentity) -> tuple[str, str, str, str, str]:
    model = identity.model
    return model.gateway_id, identity.route_id, model.provider_id, model.model_id, identity.dialect


@dataclass(frozen=True, slots=True)
class CatalogueChanges:
    previous_revision: str | None
    current_revision: str
    added: tuple[RouteIdentity, ...]
    removed: tuple[RouteIdentity, ...]
    metadata_changed: tuple[RouteIdentity, ...]
    availability_changed: tuple[RouteIdentity, ...]
    review_required: tuple[RouteIdentity, ...]

    @property
    def unchanged(self) -> bool:
        return not (
            self.added
            or self.removed
            or self.metadata_changed
            or self.availability_changed
        )


def compare_catalogues(
    previous: CatalogueSnapshot | None, current: CatalogueSnapshot
) -> CatalogueChanges:
    """Separate structural drift from transient readiness changes.

    A structural change demands independent owner review before any old
    approval is relied on. This function never copies or elevates policies.
    """
    if not isinstance(current, CatalogueSnapshot) or (
        previous is not None and not isinstance(previous, CatalogueSnapshot)
    ):
        raise CatalogueStateError("invalid_catalogue_snapshot")
    if previous is not None and previous.gateway_id != current.gateway_id:
        raise CatalogueStateError("catalogue_gateway_mismatch")
    if (
        previous is not None
        and previous.revision == current.revision
        and previous != current
    ):
        raise CatalogueStateError("catalogue_revision_collision")

    before_routes: dict[RouteIdentity, DiscoveredRoute] = (
        {r.identity: r for r in previous.routes} if previous else {}
    )
    after_routes: dict[RouteIdentity, DiscoveredRoute] = {
        r.identity: r for r in current.routes
    }
    before_models: dict[ModelIdentity, ModelMetadata] = (
        {m.identity: m for m in previous.models} if previous else {}
    )
    after_models: dict[ModelIdentity, ModelMetadata] = {
        m.identity: m for m in current.models
    }
    added = set(after_routes) - set(before_routes)
    removed = set(before_routes) - set(after_routes)
    shared = set(before_routes) & set(after_routes)
    metadata = {
        identity
        for identity in shared
        if (
            before_models[identity.model] != after_models[identity.model]
            or before_routes[identity].pricing != after_routes[identity].pricing
            or before_routes[identity].region != after_routes[identity].region
            or before_routes[identity].schema_version != after_routes[identity].schema_version
        )
    }
    availability = {
        identity
        for identity in shared
        if before_routes[identity].availability != after_routes[identity].availability
    }
    review = added | removed | metadata
    return CatalogueChanges(
        previous_revision=previous.revision if previous else None,
        current_revision=current.revision,
        added=tuple(sorted(added, key=_route_sort)),
        removed=tuple(sorted(removed, key=_route_sort)),
        metadata_changed=tuple(sorted(metadata, key=_route_sort)),
        availability_changed=tuple(sorted(availability, key=_route_sort)),
        review_required=tuple(sorted(review, key=_route_sort)),
    )


class InMemoryCatalogue:
    """Bounded to a single snapshot; CAS prevents stale updates.

    No persistence or approval store. Owner-reviewed TrustedRoutePolicy values
    are intentionally NEVER accepted or updated here.
    """

    __slots__ = ("_snapshot", "_lock")

    def __init__(self) -> None:
        self._snapshot: CatalogueSnapshot | None = None
        self._lock = RLock()

    @property
    def current(self) -> CatalogueSnapshot | None:
        with self._lock:
            return self._snapshot

    def replace(
        self, snapshot: CatalogueSnapshot, *, expected_revision: str | None
    ) -> CatalogueChanges:
        with self._lock:
            actual = self._snapshot.revision if self._snapshot else None
            if actual != expected_revision:
                raise CatalogueStateError("stale_catalogue_revision")
            changes = compare_catalogues(self._snapshot, snapshot)
            self._snapshot = snapshot
            return changes
