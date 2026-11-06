"""Versioned credential-free contracts for model catalogue and route policy.

Stage A only: no model discovery, model selection, provider requests or credential lookup.
Untrusted catalogue entries cannot supply approval or inference destination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from max_core.gateway.contracts import Dialect, InferenceDestination, ModelSpec


class CapabilitySupport(StrEnum):
    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class EvidenceSource(StrEnum):
    UNKNOWN = "unknown"
    GATEWAY_REPORTED = "gateway_reported"
    OWNER_REVIEWED = "owner_reviewed"
    PROTOCOL_TESTED = "protocol_tested"


class AvailabilityState(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    NEEDS_KEY = "needs_key"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"


class ApprovalState(StrEnum):
    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    DISABLED = "disabled"


def _id(value: str, label: str, pattern: str) -> None:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"Invalid {label}")


def _version(version: int) -> None:
    if type(version) is not int or version != 1:
        raise ValueError("Unsupported catalogue schema version")


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    gateway_id: str
    provider_id: str
    model_id: str

    def __post_init__(self) -> None:
        _id(self.gateway_id, "gateway ID", r"[A-Za-z0-9_.:-]{1,100}")
        _id(self.provider_id, "provider ID", r"[A-Za-z0-9_.:-]{1,100}")
        _id(self.model_id, "model ID", r"[A-Za-z0-9_.:/-]{1,150}")


@dataclass(frozen=True, slots=True)
class RouteIdentity:
    model: ModelIdentity
    route_id: str
    dialect: Dialect

    def __post_init__(self) -> None:
        if not isinstance(self.model, ModelIdentity):
            raise ValueError("Invalid route model identity")
        _id(self.route_id, "route ID", r"[A-Za-z0-9_-]{1,100}")
        if not isinstance(self.dialect, Dialect):
            raise ValueError("Invalid route dialect")


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    name: str
    support: CapabilitySupport = CapabilitySupport.UNKNOWN
    source: EvidenceSource = EvidenceSource.UNKNOWN

    def __post_init__(self) -> None:
        _id(self.name, "capability name", r"[a-z][a-z0-9_.-]{0,63}")
        if not isinstance(self.support, CapabilitySupport):
            raise ValueError("Invalid capability support")
        if not isinstance(self.source, EvidenceSource):
            raise ValueError("Invalid capability provenance")


@dataclass(frozen=True, slots=True)
class CapabilityMetadata:
    schema_version: int = 1
    claims: tuple[CapabilityEvidence, ...] = ()

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.claims, tuple) or any(
            not isinstance(claim, CapabilityEvidence) for claim in self.claims
        ):
            raise ValueError("Invalid capability claims")
        names = [claim.name for claim in self.claims]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate capability name")

    def state(self, name: str) -> CapabilitySupport:
        _id(name, "capability name", r"[a-z][a-z0-9_.-]{0,63}")
        for claim in self.claims:
            if claim.name == name:
                return claim.support
        return CapabilitySupport.UNKNOWN

    def verified_supported(self, name: str) -> bool:
        return any(
            claim.name == name
            and claim.support == CapabilitySupport.SUPPORTED
            and claim.source in {EvidenceSource.OWNER_REVIEWED, EvidenceSource.PROTOCOL_TESTED}
            for claim in self.claims
        )


@dataclass(frozen=True, slots=True)
class PricingProvenance:
    input_usd_per_million: Decimal | None = None
    output_usd_per_million: Decimal | None = None
    source: EvidenceSource = EvidenceSource.UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise ValueError("Invalid pricing provenance")
        for rate in (self.input_usd_per_million, self.output_usd_per_million):
            if rate is not None and (
                not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0
            ):
                raise ValueError("Invalid pricing rate")


@dataclass(frozen=True, slots=True)
class AvailabilityObservation:
    state: AvailabilityState = AvailabilityState.UNKNOWN
    source: EvidenceSource = EvidenceSource.UNKNOWN
    observed_at_epoch: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, AvailabilityState) or not isinstance(
            self.source, EvidenceSource
        ):
            raise ValueError("Invalid availability observation")
        if self.observed_at_epoch is not None and (
            type(self.observed_at_epoch) is not int or self.observed_at_epoch < 0
        ):
            raise ValueError("Invalid availability timestamp")


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    gateway_id: str
    provider_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _id(self.gateway_id, "gateway ID", r"[A-Za-z0-9_.:-]{1,100}")
        _id(self.provider_id, "provider ID", r"[A-Za-z0-9_.:-]{1,100}")


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    identity: ModelIdentity
    context_tokens: int | None = None
    capabilities: CapabilityMetadata = CapabilityMetadata()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.identity, ModelIdentity) or not isinstance(
            self.capabilities, CapabilityMetadata
        ):
            raise ValueError("Invalid model metadata")
        if self.context_tokens is not None and (
            type(self.context_tokens) is not int or self.context_tokens < 1
        ):
            raise ValueError("Invalid model context window")


@dataclass(frozen=True, slots=True)
class DiscoveredRoute:
    """Untrusted discovery: no approval, destination, endpoint or secrets."""

    identity: RouteIdentity
    availability: AvailabilityObservation = AvailabilityObservation()
    pricing: PricingProvenance = PricingProvenance()
    region: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if (
            not isinstance(self.identity, RouteIdentity)
            or not isinstance(self.availability, AvailabilityObservation)
            or not isinstance(self.pricing, PricingProvenance)
        ):
            raise ValueError("Invalid discovered route metadata")
        if self.region is not None:
            _id(self.region, "region", r"[A-Za-z0-9_.:-]{1,100}")


@dataclass(frozen=True, slots=True)
class TrustedRoutePolicy:
    """Owner-reviewed configuration, NEVER populated from discovery payloads.

    This assertion is not runtime attestation of the proxy's upstream route.
    """

    identity: RouteIdentity
    approval: ApprovalState = ApprovalState.UNREVIEWED
    inference_destination: InferenceDestination = InferenceDestination.UNKNOWN
    evidence_source: EvidenceSource = EvidenceSource.UNKNOWN
    policy_revision: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.identity, RouteIdentity):
            raise ValueError("Invalid trusted policy route")
        if (
            not isinstance(self.approval, ApprovalState)
            or not isinstance(self.inference_destination, InferenceDestination)
            or not isinstance(self.evidence_source, EvidenceSource)
        ):
            raise ValueError("Invalid route policy classification")
        if self.policy_revision is not None:
            _id(self.policy_revision, "policy revision", r"[A-Za-z0-9_.:-]{1,100}")
        if self.approval == ApprovalState.APPROVED and (
            self.inference_destination == InferenceDestination.UNKNOWN
            or self.evidence_source != EvidenceSource.OWNER_REVIEWED
            or self.policy_revision is None
        ):
            raise ValueError("Approval requires an owner-reviewed destination and policy revision")
        if (
            self.inference_destination == InferenceDestination.VERIFIED_LOCAL
            and self.evidence_source != EvidenceSource.OWNER_REVIEWED
        ):
            raise ValueError("Verified local classification requires owner-reviewed evidence")


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    """Untrusted discovery snapshot, without trusted destination or approvals."""

    gateway_id: str
    revision: str
    providers: tuple[ProviderMetadata, ...] = ()
    models: tuple[ModelMetadata, ...] = ()
    routes: tuple[DiscoveredRoute, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _id(self.gateway_id, "gateway ID", r"[A-Za-z0-9_.:-]{1,100}")
        _id(self.revision, "catalogue revision", r"[A-Za-z0-9_.:-]{1,100}")
        if (
            not isinstance(self.providers, tuple)
            or not isinstance(self.models, tuple)
            or not isinstance(self.routes, tuple)
        ):
            raise ValueError("Catalogue collections must be immutable tuples")
        if (
            any(not isinstance(x, ProviderMetadata) for x in self.providers)
            or any(not isinstance(x, ModelMetadata) for x in self.models)
            or any(not isinstance(x, DiscoveredRoute) for x in self.routes)
        ):
            raise ValueError("Invalid catalogue member")
        provider_ids = [(p.gateway_id, p.provider_id) for p in self.providers]
        model_ids = [m.identity for m in self.models]
        route_ids = [(r.identity.model.gateway_id, r.identity.route_id) for r in self.routes]
        if (
            len(set(provider_ids)) != len(provider_ids)
            or len(set(model_ids)) != len(model_ids)
            or len(set(route_ids)) != len(route_ids)
        ):
            raise ValueError("Duplicate or ambiguous catalogue identity")
        for p in self.providers:
            if p.gateway_id != self.gateway_id:
                raise ValueError("Cross-gateway provider metadata")
        for m in self.models:
            if (m.identity.gateway_id, m.identity.provider_id) not in provider_ids:
                raise ValueError("Model references an unknown provider")
        for r in self.routes:
            if r.identity.model not in model_ids:
                raise ValueError("Route references an unknown model")


@dataclass(frozen=True, slots=True)
class SelectionMetadata:
    """Credential-free ranking input. Never an authorization to execute."""

    identity: RouteIdentity
    catalogue_revision: str
    capabilities: CapabilityMetadata
    context_tokens: int | None = None
    inference_destination: InferenceDestination = InferenceDestination.UNKNOWN
    approval: ApprovalState = ApprovalState.UNREVIEWED
    region: str | None = None
    priority: int = 100
    pricing: PricingProvenance = PricingProvenance()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity, RouteIdentity)
            or not isinstance(self.capabilities, CapabilityMetadata)
            or not isinstance(self.pricing, PricingProvenance)
        ):
            raise ValueError("Invalid selection metadata")
        _id(self.catalogue_revision, "catalogue revision", r"[A-Za-z0-9_.:-]{1,100}")
        if not isinstance(self.inference_destination, InferenceDestination) or not isinstance(
            self.approval, ApprovalState
        ):
            raise ValueError("Invalid selection policy")
        if self.context_tokens is not None and (
            type(self.context_tokens) is not int or self.context_tokens < 1
        ):
            raise ValueError("Invalid selection context window")
        if type(self.priority) is not int:
            raise ValueError("Invalid selection priority")
        if self.region is not None:
            _id(self.region, "region", r"[A-Za-z0-9_.:-]{1,100}")
        if (
            self.approval != ApprovalState.APPROVED
            or self.inference_destination == InferenceDestination.UNKNOWN
        ):
            raise ValueError("Selection candidates require an approved, classified route")


def project_model_spec(
    spec: ModelSpec,
    identity: RouteIdentity,
    policy: TrustedRoutePolicy,
    revision: str,
    capabilities: CapabilityMetadata | None = None,
) -> SelectionMetadata:
    """Trusted gateway projection; never transfers spec.endpoint or spec.api_key."""
    if (
        not isinstance(spec, ModelSpec)
        or not isinstance(identity, RouteIdentity)
        or not isinstance(policy, TrustedRoutePolicy)
    ):
        raise ValueError("Invalid model projection arguments")
    if (identity.model.provider_id, identity.model.model_id, identity.dialect) != (
        spec.provider_id,
        spec.model_id,
        spec.dialect,
    ) or policy.identity != identity:
        raise ValueError("Route identity does not match execution configuration")
    if policy.inference_destination != spec.inference_destination:
        raise ValueError("Trusted route destination does not match execution configuration")
    if (
        policy.approval != ApprovalState.APPROVED
        or policy.inference_destination == InferenceDestination.UNKNOWN
    ):
        raise ValueError("Unapproved or unknown route cannot produce selection metadata")
    # Legacy ModelSpec capability labels are not independently verified evidence.
    evidence = capabilities if capabilities is not None else CapabilityMetadata()
    if not isinstance(evidence, CapabilityMetadata):
        raise ValueError("Invalid capability evidence")
    return SelectionMetadata(
        identity=identity,
        catalogue_revision=revision,
        capabilities=evidence,
        context_tokens=spec.context_tokens,
        inference_destination=spec.inference_destination,
        approval=policy.approval,
        region=spec.region,
        priority=spec.priority,
        pricing=PricingProvenance(
            spec.input_usd_per_million,
            spec.output_usd_per_million,
            EvidenceSource.UNKNOWN,
        ),
    )
