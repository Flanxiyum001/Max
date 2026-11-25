"""Opt-in local Magnitude configuration and read-only connection diagnostics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal

import httpx

from max_core.gateway import Dialect, InferenceDestination, ModelSpec
from max_core.gateway_trial import TrialConfigurationError, TrialSettings

ORIGIN = "http://127.0.0.1:10100"
API_BASE = ORIGIN + "/inference/v1"
PROVIDER_ID = "magnitude"
_MODEL_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,150}")
_MAX_LIST_BYTES = 256_000


def magnitude_settings(env: Mapping[str, str]) -> TrialSettings:
    model = env.get("MAX_MAGNITUDE_MODEL", "").strip()
    if not _MODEL_ID.fullmatch(model):
        raise TrialConfigurationError("magnitude_model_required")
    try:
        context = int(env.get("MAX_MAGNITUDE_CONTEXT_TOKENS", "8192"))
    except ValueError:
        raise TrialConfigurationError("magnitude_context_invalid") from None
    if not 2048 <= context <= 65536:
        raise TrialConfigurationError("magnitude_context_invalid")
    tools = env.get("MAX_MAGNITUDE_TOOLS", "no")
    if tools not in {"yes", "no"}:
        raise TrialConfigurationError("magnitude_tools_invalid")
    return TrialSettings(
        spec=ModelSpec(
            provider_id=PROVIDER_ID,
            model_id=model,
            dialect=Dialect.OPENAI_CHAT,
            endpoint=API_BASE + "/chat/completions",
            is_local=True,
            context_tokens=context,
            inference_destination=InferenceDestination.VERIFIED_LOCAL,
            capabilities=frozenset({"text", "tools"} if tools == "yes" else {"text"}),
            input_usd_per_million=Decimal(0),
            output_usd_per_million=Decimal(0),
        ),
        max_cost_usd=Decimal(0),
        persona_enabled=True,
    )


def _bounded_get(client: httpx.Client, url: str) -> bytes:
    try:
        with client.stream("GET", url, follow_redirects=False, timeout=3.0) as response:
            if response.status_code != 200:
                raise TrialConfigurationError("magnitude_service_not_ready")
            raw = bytearray()
            for part in response.iter_bytes(chunk_size=4096):
                if len(raw) + len(part) > _MAX_LIST_BYTES:
                    raise TrialConfigurationError("magnitude_invalid_model_list")
                raw.extend(part)
            return bytes(raw)
    except httpx.RequestError:
        raise TrialConfigurationError("magnitude_unreachable") from None


def discover_models(client: httpx.Client) -> list[str]:
    _bounded_get(client, ORIGIN + "/health")
    raw = _bounded_get(client, API_BASE + "/models")
    try:
        payload = json.loads(raw)
        rows = payload["data"]
        if not isinstance(rows, list):
            raise TypeError
        models = sorted(
            {
                model
                for row in rows
                if isinstance(row, dict)
                and isinstance((model := row.get("id")), str)
                and _MODEL_ID.fullmatch(model)
            }
        )
    except (KeyError, TypeError, ValueError, RecursionError):
        raise TrialConfigurationError("magnitude_invalid_model_list") from None
    if not models:
        raise TrialConfigurationError("magnitude_no_models")
    return models


def choose_model(configured: str, models: list[str]) -> str:
    if configured:
        if configured not in models:
            raise TrialConfigurationError("magnitude_model_not_found")
        return configured
    if len(models) == 1:
        return models[0]
    raise TrialConfigurationError("magnitude_select_model")


_HELP = {
    "magnitude_unreachable": "Start Magnitude Desktop or magnitude serve, then retry.",
    "magnitude_service_not_ready": "Magnitude is not ready. Check its local server and retry.",
    "magnitude_no_models": "Install a model in Magnitude first; MAX does not download models.",
    "magnitude_model_not_found": "Set MAX_MAGNITUDE_MODEL to an exact model ID shown above.",
    "magnitude_select_model": "More than one model is available. Set MAX_MAGNITUDE_MODEL.",
    "magnitude_model_required": "Set MAX_MAGNITUDE_MODEL or launch with --magnitude.",
    "magnitude_context_invalid": "Set MAX_MAGNITUDE_CONTEXT_TOKENS between 2048 and 65536.",
    "magnitude_tools_invalid": "Set MAX_MAGNITUDE_TOOLS to yes or no.",
}


def describe_error(code: str) -> str:
    return _HELP.get(code, "Magnitude returned an invalid model list.")


def check(*, configured: str = "") -> str:
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        models = discover_models(client)
    print("Magnitude models: " + ", ".join(models))
    return choose_model(configured.strip(), models)
