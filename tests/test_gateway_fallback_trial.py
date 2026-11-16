"""Mock-only cloud-first local fallback tests; never reaches real endpoints."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from max_core.gateway import GatewayError
from max_core.gateway_fallback_trial import (
    FallbackTrialSettings,
    ask_with_local_fallback,
)
from max_core.gateway_trial import TrialConfigurationError
from tests.test_gateway_trial import cloud_config


def settings_env() -> dict[str, str]:
    result = cloud_config()
    result.update(
        MAX_TRIAL_FALLBACK_ENABLED="yes",
        MAX_TRIAL_FALLBACK_LOCAL_EGRESS_REVIEWED="yes",
        MAX_TRIAL_FALLBACK_ENDPOINT="http://127.0.0.1:11434/v1/chat/completions",
        MAX_TRIAL_FALLBACK_MODEL="local-model",
        MAX_TRIAL_FALLBACK_CONTEXT_TOKENS="4096",
    )
    return result


def reply(model: str, text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": text,
                        "tool_calls": [
                            {
                                "id": "not-executable",
                                "type": "function",
                                "function": {"name": "dangerous", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 5},
        },
    )


def perform(settings: FallbackTrialSettings, handler: object) -> tuple[str, str]:
    async def run() -> tuple[str, str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:  # type: ignore[arg-type]
            response = await ask_with_local_fallback(settings, "hello", client=client)
            return response.content, response.provider_id

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("missing", "error"),
    [
        ("MAX_TRIAL_FALLBACK_ENABLED", "local_fallback_not_enabled"),
        ("MAX_TRIAL_FALLBACK_LOCAL_EGRESS_REVIEWED", "local_egress_not_reviewed"),
        ("MAX_TRIAL_FALLBACK_ENDPOINT", "invalid_local_fallback_configuration"),
        ("MAX_TRIAL_FALLBACK_MODEL", "invalid_local_fallback_configuration"),
    ],
)
def test_fallback_requires_all_approval_flags_and_endpoint(missing: str, error: str) -> None:
    config = settings_env()
    del config[missing]
    with pytest.raises(TrialConfigurationError, match=f"^{error}$"):
        FallbackTrialSettings.from_env(config)


def test_cloud_success_does_not_contact_local_model() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        assert request.url.host == "example.com"
        assert request.headers["authorization"] == "Bearer example-test-token"
        return reply("reviewed-model", "online")

    assert perform(FallbackTrialSettings.from_env(settings_env()), handler) == (
        "online",
        "owner-trial",
    )
    assert calls == ["example.com"]


@pytest.mark.parametrize("status", [429, 503])
def test_only_approved_cloud_failure_falls_back_to_local(status: int) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "example.com":
            assert request.headers["authorization"] == "Bearer example-test-token"
            return httpx.Response(status)
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers
        assert json.loads(request.content)["messages"] == [
            {"role": "user", "content": "hello"}
        ]
        return reply("local-model", "offline")

    assert perform(FallbackTrialSettings.from_env(settings_env()), handler) == (
        "offline",
        "owner-reviewed-local-fallback",
    )
    assert calls == ["example.com", "127.0.0.1"]


@pytest.mark.parametrize("kind", ["connect_error", "connect_timeout"])
def test_pre_request_connect_failure_can_fall_back(kind: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "example.com":
            if kind == "connect_timeout":
                raise httpx.ConnectTimeout("secret upstream transport", request=request)
            raise httpx.ConnectError("secret upstream transport", request=request)
        return reply("local-model", "offline")

    assert perform(FallbackTrialSettings.from_env(settings_env()), handler)[0] == "offline"
    assert calls == ["example.com", "127.0.0.1"]


@pytest.mark.parametrize("kind", ["401", "read_timeout", "invalid_json"])
def test_uncertain_or_unauthorized_response_never_falls_back(kind: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if kind == "read_timeout":
            raise httpx.ReadTimeout("secret upstream failure", request=request)
        if kind == "invalid_json":
            return httpx.Response(200, text="{not_json")
        return httpx.Response(401)

    with pytest.raises(GatewayError):
        perform(FallbackTrialSettings.from_env(settings_env()), handler)
    assert calls == ["example.com"]


def test_unauthorized_local_egress_rejected_before_transport() -> None:
    config = settings_env()
    config["MAX_TRIAL_FALLBACK_ENDPOINT"] = "https://host.example/v1/chat/completions"
    with pytest.raises(TrialConfigurationError, match="invalid_local_fallback_configuration"):
        FallbackTrialSettings.from_env(config)


def test_bad_cloud_configuration_does_not_enable_local_only() -> None:
    config = settings_env()
    config["MAX_TRIAL_DESTINATION"] = "unknown"
    with pytest.raises(TrialConfigurationError, match="destination_unverified"):
        FallbackTrialSettings.from_env(config)


def test_no_secrets_in_trial_settings_repr() -> None:
    config = settings_env()
    settings = FallbackTrialSettings.from_env(config)
    assert config["MAX_TRIAL_API_KEY"] not in repr(settings)
