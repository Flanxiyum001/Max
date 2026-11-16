"""Explicit one-turn cloud-to-verified-local fallback trial. No tools or task execution."""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import httpx

from max_core.gateway import (
    ChatMessage,
    Dialect,
    Gateway,
    GatewayError,
    GatewayRequest,
    HTTPAdapter,
    InferenceDestination,
    ModelSpec,
    Privacy,
)
from max_core.gateway_trial import TrialConfigurationError, TrialSettings


@dataclass(frozen=True, slots=True, repr=False)
class FallbackTrialSettings:
    """Both endpoints are trusted host configuration, never model-supplied."""

    cloud: TrialSettings
    local: ModelSpec

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> FallbackTrialSettings:
        env = os.environ if source is None else source
        if env.get("MAX_TRIAL_FALLBACK_ENABLED") != "yes":
            raise TrialConfigurationError("local_fallback_not_enabled")
        if env.get("MAX_TRIAL_FALLBACK_LOCAL_EGRESS_REVIEWED") != "yes":
            raise TrialConfigurationError("local_egress_not_reviewed")
        primary = TrialSettings.from_env(env)
        if primary.spec.inference_destination != InferenceDestination.CLOUD:
            raise TrialConfigurationError("fallback_primary_must_be_cloud")
        try:
            local = ModelSpec(
                provider_id="owner-reviewed-local-fallback",
                model_id=env["MAX_TRIAL_FALLBACK_MODEL"],
                dialect=Dialect.OPENAI_CHAT,
                endpoint=env["MAX_TRIAL_FALLBACK_ENDPOINT"],
                is_local=True,
                inference_destination=InferenceDestination.VERIFIED_LOCAL,
                context_tokens=int(env["MAX_TRIAL_FALLBACK_CONTEXT_TOKENS"]),
                priority=primary.spec.priority + 1,
                # API usage is locally metered as zero only on an owner-reviewed
                # self-hosted model. Hardware and energy costs are not included.
                input_usd_per_million=Decimal(0),
                output_usd_per_million=Decimal(0),
            )
            if local.context_tokens < 1024:
                raise TrialConfigurationError("fallback_context_too_small")
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, TrialConfigurationError):
                raise
            raise TrialConfigurationError("invalid_local_fallback_configuration") from None
        return cls(primary, local)


@dataclass(frozen=True, slots=True, repr=False)
class FallbackTrialReply:
    content: str
    provider_id: str


async def ask_with_local_fallback(
    settings: FallbackTrialSettings,
    prompt: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> FallbackTrialReply:
    """Only 429, 503 or definite pre-request connect failures may fail over.

    No retries for uncertain read/write timeouts, invalid JSON, authentication
    failures, partial streams, cost/privacy rejections or model tool proposals.
    """
    request = GatewayRequest(
        request_id=uuid4().hex,
        messages=(ChatMessage("user", prompt),),
        privacy=Privacy.PUBLIC,
        max_input_tokens=min(
            4096, settings.cloud.spec.context_tokens - 512, settings.local.context_tokens - 512
        ),
        max_output_tokens=512,
        max_cost_usd=settings.cloud.max_cost_usd,
        cloud_allowed=True,
        allow_fallback=True,
    )
    if client is not None:
        reply = await Gateway((settings.cloud.spec, settings.local), HTTPAdapter(client)).complete(
            request
        )
    else:
        async with httpx.AsyncClient(trust_env=False) as owned:
            reply = await Gateway(
                (settings.cloud.spec, settings.local), HTTPAdapter(owned)
            ).complete(request)
    # Deliberately discard all provider tool proposals.
    return FallbackTrialReply(reply.content, reply.provider_id)


def main() -> int:
    try:
        settings = FallbackTrialSettings.from_env()
        prompt = getpass.getpass("Public test prompt (hidden input): ")
        if not prompt.strip():
            raise TrialConfigurationError("empty_prompt")
        reply = asyncio.run(ask_with_local_fallback(settings, prompt))
    except (TrialConfigurationError, GatewayError, ValueError, httpx.HTTPError) as exc:
        code = (
            exc.code if isinstance(exc, (TrialConfigurationError, GatewayError)) else "trial_failed"
        )
        print(f"MAX fallback trial failed: {code}", file=sys.stderr)
        return 1
    print(f"Route used: {reply.provider_id}")
    print(reply.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
