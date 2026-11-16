"""Trial configuration and mock-only execution boundary."""

import asyncio
import json

import httpx
import pytest

from max_core.gateway_trial import TrialConfigurationError, TrialSettings, ask_once


def cloud_config() -> dict[str, str]:
    return {
        "MAX_TRIAL_DESTINATION": "cloud",
        "MAX_TRIAL_CLOUD_ALLOWED": "yes",
        "MAX_TRIAL_ENDPOINT": "https://example.com/v1/chat/completions",
        "MAX_TRIAL_MODEL": "reviewed-model",
        "MAX_TRIAL_CONTEXT_TOKENS": "8192",
        "MAX_TRIAL_API_KEY": "example-test-token",
        "MAX_TRIAL_MAX_COST_USD": "0.01",
        "MAX_TRIAL_INPUT_USD_PER_MILLION": "1",
        "MAX_TRIAL_OUTPUT_USD_PER_MILLION": "2",
    }


@pytest.mark.parametrize(
    ("missing", "code"),
    [
        ("MAX_TRIAL_CLOUD_ALLOWED", "cloud_not_enabled"),
        ("MAX_TRIAL_MAX_COST_USD", "cloud_budget_required"),
        ("MAX_TRIAL_INPUT_USD_PER_MILLION", "cloud_prices_required"),
        ("MAX_TRIAL_API_KEY", "cloud_key_required"),
    ],
)
def test_cloud_requires_explicit_admission(missing: str, code: str) -> None:
    config = cloud_config()
    del config[missing]
    with pytest.raises(TrialConfigurationError, match=f"^{code}$"):
        TrialSettings.from_env(config)


def test_unknown_and_unreviewed_local_egress_fail_closed() -> None:
    with pytest.raises(TrialConfigurationError, match="destination_unverified"):
        TrialSettings.from_env({})
    config = cloud_config()
    config.update(
        MAX_TRIAL_DESTINATION="verified_local",
        MAX_TRIAL_ENDPOINT="http://127.0.0.1:8000/v1/chat/completions",
        MAX_TRIAL_ENDPOINT_LOCAL="yes",
    )
    with pytest.raises(TrialConfigurationError, match="local_egress_not_reviewed"):
        TrialSettings.from_env(config)


def test_mock_text_trial_never_executes_tool_proposal() -> None:
    settings = TrialSettings.from_env(cloud_config())
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        assert body["messages"] == [{"role": "user", "content": "Hello"}]
        return httpx.Response(
            200,
            json={
                "model": "reviewed-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Hi",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "danger", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await ask_once(settings, "Hello", client=client)

    assert asyncio.run(run()) == "Hi"
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer example-test-token"


def test_bad_configuration_never_echoes_secret() -> None:
    config = cloud_config()
    config["MAX_TRIAL_ENDPOINT"] = "https://user:secret@example.com/v1/chat/completions"
    with pytest.raises(TrialConfigurationError) as error:
        TrialSettings.from_env(config)
    assert "secret" not in str(error.value)
