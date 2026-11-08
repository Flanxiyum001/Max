"""Offline, bounded FreeLLMAPI /v1/models catalogue normalization.

Untrusted input only. No HTTP, credentials, configuration writes, approvals,
inference destinations, or gateway execution are available from this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, cast

from max_core.gateway.catalogue import (
    AvailabilityObservation,
    AvailabilityState,
    CapabilityEvidence,
    CapabilityMetadata,
    CapabilitySupport,
    CatalogueSnapshot,
    DiscoveredRoute,
    EvidenceSource,
    ModelIdentity,
    ModelMetadata,
    PricingProvenance,
    ProviderMetadata,
    RouteIdentity,
)
from max_core.gateway.contracts import Dialect

MAX_CATALOGUE_BYTES = 2_000_000
MAX_CATALOGUE_ROWS = 4096
MAX_PARAMETERS_PER_MODEL = 64

_SAFE_PROVIDER = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
_SAFE_MODEL = re.compile(r"[A-Za-z0-9_.:/-]{1,150}\Z")
_SAFE_PARAMETER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SUSPECT_SECRET = re.compile(r"(?i)^(?:sk[-_]|gh[pousr]_|AIza|bearer|api[-_]key)")


class CatalogueImportError(ValueError):
    """Sanitized validation failure. Never includes the upstream payload."""

    def __init__(self, code: str = "invalid_catalogue_payload") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CatalogueImport:
    snapshot: CatalogueSnapshot
    ignored_virtual_entries: int
    ambiguous_model_ids: tuple[str, ...]
    source: str = "freellmapi_models_v1"


def _safe_name(value: object, pattern: re.Pattern[str]) -> str:
    if (
        not isinstance(value, str)
        or not pattern.fullmatch(value)
        or _SUSPECT_SECRET.match(value) is not None
    ):
        raise CatalogueImportError()
    return value


def _context(row: dict[str, Any]) -> int | None:
    first = row.get("context_window")
    second = row.get("context_length")
    for value in (first, second):
        if value is not None and (type(value) is not int or not 1 <= value <= 10_000_000):
            raise CatalogueImportError()
    if first is not None and second is not None and first != second:
        raise CatalogueImportError("conflicting_catalogue_context")
    return cast(int | None, first if first is not None else second)


def _capabilities(row: dict[str, Any]) -> CapabilityMetadata:
    parameters = row.get("supported_parameters")
    if parameters is None:
        return CapabilityMetadata()
    if not isinstance(parameters, list) or len(parameters) > MAX_PARAMETERS_PER_MODEL:
        raise CatalogueImportError()
    if any(not isinstance(p, str) or _SAFE_PARAMETER.fullmatch(p) is None for p in parameters):
        raise CatalogueImportError()
    if len(parameters) != len(set(parameters)):
        raise CatalogueImportError("duplicate_catalogue_parameter")
    capabilities: set[str] = set()
    if "tools" in parameters or "tool_choice" in parameters:
        capabilities.add("tools")
    if "response_format" in parameters:
        capabilities.add("structured_output")
    # These are gateway-reported claims, NOT independently tested support.
    return CapabilityMetadata(
        claims=tuple(
            CapabilityEvidence(name, CapabilitySupport.SUPPORTED, EvidenceSource.GATEWAY_REPORTED)
            for name in sorted(capabilities)
        )
    )


def _availability(row: dict[str, Any], observed_at_epoch: int | None) -> AvailabilityObservation:
    available = row.get("available")
    if available is not None and type(available) is not bool:
        raise CatalogueImportError()
    status = row.get("execution_status")
    if status is not None and not isinstance(status, str):
        raise CatalogueImportError()
    states = {
        "ready": AvailabilityState.READY,
        "needsKey": AvailabilityState.NEEDS_KEY,
        "exhausted": AvailabilityState.EXHAUSTED,
    }
    state = AvailabilityState.UNKNOWN
    if status is not None:
        state = states.get(status, AvailabilityState.UNKNOWN)
    if status is None and available is False:
        state = AvailabilityState.UNAVAILABLE
    # An inconsistent status is NOT proof of readiness.
    if (available is False and state == AvailabilityState.READY) or (
        available is True and state in {AvailabilityState.NEEDS_KEY, AvailabilityState.EXHAUSTED}
    ):
        state = AvailabilityState.UNKNOWN
    return AvailabilityObservation(
        state=state,
        source=EvidenceSource.GATEWAY_REPORTED,
        observed_at_epoch=observed_at_epoch,
    )


def _is_virtual(model_id: str, provider_id: str) -> bool:
    # Documented synthetic identifiers, not executable concrete model routes.
    return (
        model_id in {"auto", "fusion"}
        or model_id.startswith("auto:")
        or (provider_id == "freellmapi" and model_id.startswith("claude-"))
    )


def _route_id(gateway_id: str, provider_id: str, model_id: str, dialect: Dialect) -> str:
    # Use canonical tuple encoding; never concatenate ambiguous unescaped fields.
    normalized = json.dumps(
        [gateway_id, provider_id, model_id, dialect.value],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return "r_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _revision(
    gateway_id: str, models: tuple[ModelMetadata, ...], routes: tuple[DiscoveredRoute, ...]
) -> str:
    # Generated from safe, normalized fields, not raw external JSON (which may
    # contain unknown credential fields). A different observation changes revision.
    payload = {
        "gateway": gateway_id,
        "models": [
            [
                model.identity.provider_id,
                model.identity.model_id,
                model.context_tokens,
                [
                    [claim.name, claim.support.value, claim.source.value]
                    for claim in model.capabilities.claims
                ],
            ]
            for model in models
        ],
        "routes": [
            [
                route.identity.route_id,
                route.identity.dialect.value,
                route.availability.state.value,
                route.availability.observed_at_epoch,
                route.pricing.input_usd_per_million,
                route.pricing.output_usd_per_million,
                route.region,
            ]
            for route in routes
        ],
    }
    canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return "v1_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_freellmapi_models(
    payload: object,
    *,
    gateway_id: str,
    dialect: Dialect = Dialect.OPENAI_CHAT,
    observed_at_epoch: int | None = None,
) -> CatalogueImport:
    """Parse supplied decoded JSON ONLY. Does not request /v1/models.

    Opaque provider IDs and model IDs are validated; no provider list is
    embedded. A READY observation is never an approval, healthy-probe proof,
    cost quotation, or inference destination assertion.
    """
    if not isinstance(gateway_id, str) or _SAFE_PROVIDER.fullmatch(gateway_id) is None:
        raise CatalogueImportError("invalid_gateway_id")
    if not isinstance(dialect, Dialect):
        raise CatalogueImportError("invalid_catalogue_dialect")
    if observed_at_epoch is not None and (
        type(observed_at_epoch) is not int or observed_at_epoch < 0
    ):
        raise CatalogueImportError("invalid_observation_timestamp")
    try:
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise CatalogueImportError() from None
    if len(encoded.encode("utf-8")) > MAX_CATALOGUE_BYTES:
        raise CatalogueImportError("catalogue_too_large")
    if not isinstance(payload, dict) or payload.get("object") != "list":
        raise CatalogueImportError()
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise CatalogueImportError()
    if len(rows) > MAX_CATALOGUE_ROWS:
        raise CatalogueImportError("too_many_catalogue_entries")

    providers: dict[str, ProviderMetadata] = {}
    models: dict[ModelIdentity, ModelMetadata] = {}
    routes: list[DiscoveredRoute] = []
    seen_ids: Counter[str] = Counter()
    ignored = 0
    try:
        for raw in rows:
            if not isinstance(raw, dict) or raw.get("object") != "model":
                raise CatalogueImportError()
            model_id = _safe_name(raw.get("id"), _SAFE_MODEL)
            provider_id = _safe_name(raw.get("owned_by"), _SAFE_PROVIDER)
            if _is_virtual(model_id, provider_id):
                ignored += 1
                continue
            identity = ModelIdentity(gateway_id, provider_id, model_id)
            if identity in models:
                raise CatalogueImportError("duplicate_catalogue_model")
            providers[provider_id] = ProviderMetadata(gateway_id, provider_id)
            models[identity] = ModelMetadata(
                identity=identity,
                context_tokens=_context(raw),
                capabilities=_capabilities(raw),
            )
            routes.append(
                DiscoveredRoute(
                    identity=RouteIdentity(
                        identity,
                        _route_id(gateway_id, provider_id, model_id, dialect),
                        dialect,
                    ),
                    availability=_availability(raw, observed_at_epoch),
                    pricing=PricingProvenance(),  # No authoritative USD quote in /v1/models.
                )
            )
            seen_ids[model_id] += 1
        ordered_models = tuple(
            sorted(
                models.values(),
                key=lambda item: (item.identity.provider_id, item.identity.model_id),
            )
        )
        ordered_routes = tuple(sorted(routes, key=lambda item: item.identity.route_id))
        snapshot = CatalogueSnapshot(
            gateway_id=gateway_id,
            revision=_revision(gateway_id, ordered_models, ordered_routes),
            providers=tuple(sorted(providers.values(), key=lambda item: item.provider_id)),
            models=ordered_models,
            routes=ordered_routes,
        )
    except CatalogueImportError:
        raise
    except (ValueError, TypeError, AttributeError):
        # Suppress untrusted payloads in exception chains, including a malformed
        # dataclass construction. The issue code is safe to expose.
        raise CatalogueImportError() from None

    return CatalogueImport(
        snapshot=snapshot,
        ignored_virtual_entries=ignored,
        ambiguous_model_ids=tuple(sorted(k for k, count in seen_ids.items() if count > 1)),
    )
