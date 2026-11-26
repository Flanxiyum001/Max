"""Explicit, single-turn local text trial; no task or tool execution."""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
from max_core.persona import with_persona


class TrialConfigurationError(ValueError):
    """Fixed error code without configuration values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class TrialSettings:
    spec: ModelSpec
    max_cost_usd: Decimal | None
    persona_enabled: bool = False

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> TrialSettings:
        env = os.environ if source is None else source
        try:
            destination = InferenceDestination(env.get("MAX_TRIAL_DESTINATION", "unknown"))
            dialect = Dialect(env.get("MAX_TRIAL_DIALECT", "openai_chat"))
            if destination == InferenceDestination.UNKNOWN:
                raise TrialConfigurationError("destination_unverified")
            if (
                destination == InferenceDestination.VERIFIED_LOCAL
                and env.get("MAX_TRIAL_LOCAL_EGRESS_REVIEWED") != "yes"
            ):
                raise TrialConfigurationError("local_egress_not_reviewed")
            if (
                destination == InferenceDestination.CLOUD
                and env.get("MAX_TRIAL_CLOUD_ALLOWED") != "yes"
            ):
                raise TrialConfigurationError("cloud_not_enabled")
            ceiling = env.get("MAX_TRIAL_MAX_COST_USD")
            budget = Decimal(ceiling) if ceiling is not None else None
            if destination == InferenceDestination.CLOUD and budget is None:
                raise TrialConfigurationError("cloud_budget_required")
            input_rate = env.get("MAX_TRIAL_INPUT_USD_PER_MILLION")
            output_rate = env.get("MAX_TRIAL_OUTPUT_USD_PER_MILLION")
            if destination == InferenceDestination.CLOUD and (
                input_rate is None or output_rate is None
            ):
                raise TrialConfigurationError("cloud_prices_required")
            spec = ModelSpec(
                provider_id="owner-trial",
                model_id=env["MAX_TRIAL_MODEL"],
                dialect=dialect,
                endpoint=env["MAX_TRIAL_ENDPOINT"],
                is_local=env.get("MAX_TRIAL_ENDPOINT_LOCAL") == "yes",
                context_tokens=int(env["MAX_TRIAL_CONTEXT_TOKENS"]),
                inference_destination=destination,
                input_usd_per_million=Decimal(input_rate) if input_rate is not None else None,
                output_usd_per_million=Decimal(output_rate) if output_rate is not None else None,
                api_key=env.get("MAX_TRIAL_API_KEY") or None,
                capabilities=_capabilities(env, destination),
            )
            if spec.context_tokens < 1024:
                raise TrialConfigurationError("context_too_small_for_trial")
            if destination == InferenceDestination.CLOUD and spec.api_key is None:
                raise TrialConfigurationError("cloud_key_required")
            persona = env.get("MAX_TRIAL_PERSONA", "no")
            if persona not in {"yes", "no"}:
                raise TrialConfigurationError("invalid_persona_toggle")
            if budget is not None and (not budget.is_finite() or budget < 0):
                raise TrialConfigurationError("invalid_budget")
            return cls(spec, budget, persona == "yes")
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            if isinstance(exc, TrialConfigurationError):
                raise
            raise TrialConfigurationError("invalid_trial_configuration") from None


def _capabilities(env: Mapping[str, str], destination: InferenceDestination) -> frozenset[str]:
    """MAX_TRIAL_VISION=yes marks a local model as able to read screenshots."""
    vision = env.get("MAX_TRIAL_VISION", "no")
    if vision not in {"yes", "no"}:
        raise TrialConfigurationError("invalid_vision_toggle")
    if vision == "yes" and destination != InferenceDestination.VERIFIED_LOCAL:
        raise TrialConfigurationError("vision_requires_local_model")
    return frozenset({"text", "vision"} if vision == "yes" else {"text"})


async def ask_once(
    settings: TrialSettings, prompt: str, *, client: httpx.AsyncClient | None = None
) -> str:
    """Return text only. Never interpret or execute provider tool proposals."""
    request = GatewayRequest(
        request_id=uuid4().hex,
        messages=(ChatMessage("user", prompt),),
        privacy=Privacy.PUBLIC,
        max_input_tokens=min(4096, settings.spec.context_tokens - 512),
        max_output_tokens=512,
        max_cost_usd=settings.max_cost_usd,
        cloud_allowed=settings.spec.inference_destination == InferenceDestination.CLOUD,
        local_only=settings.spec.inference_destination == InferenceDestination.VERIFIED_LOCAL,
        allow_fallback=False,
    )
    if settings.persona_enabled:
        request = with_persona(request)
    if client is not None:
        return (await Gateway((settings.spec,), HTTPAdapter(client)).complete(request)).content
    async with httpx.AsyncClient(trust_env=False) as owned_client:
        return (
            await Gateway((settings.spec,), HTTPAdapter(owned_client)).complete(request)
        ).content


def main() -> int:
    try:
        settings = TrialSettings.from_env()
        prompt = getpass.getpass("Public test prompt (hidden input): ")
        if not prompt.strip():
            raise TrialConfigurationError("empty_prompt")
        answer = asyncio.run(ask_once(settings, prompt))
    except (TrialConfigurationError, GatewayError, ValueError, httpx.HTTPError) as exc:
        code = (
            exc.code if isinstance(exc, (TrialConfigurationError, GatewayError)) else "trial_failed"
        )
        print(f"MAX trial failed: {code}", file=sys.stderr)
        return 1
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
