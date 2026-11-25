"""Original MAX personality and deferred feminine voice contract tests."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from max_core.gateway import ChatMessage, GatewayRequest, Privacy
from max_core.gateway_trial import TrialConfigurationError, TrialSettings, ask_once
from max_core.persona import PersonaError, ReplyMode, VoicePreference, persona_message, with_persona


def request(*messages: ChatMessage, input_limit: int = 4096) -> GatewayRequest:
    return GatewayRequest(
        request_id="synthetic-persona-1",
        messages=tuple(messages),
        privacy=Privacy.PUBLIC,
        max_input_tokens=input_limit,
        max_output_tokens=512,
        max_cost_usd=Decimal("0.01"),
        cloud_allowed=False,
        local_only=True,
    )


def test_persona_is_original_and_evidence_cautious_in_all_modes() -> None:
    for mode in ReplyMode:
        message = persona_message(mode)
        assert message.role == "system"
        assert "You are MAX" in message.content
        assert "Never claim a tool was used" in message.content
        assert "do not grant computer access" in message.content
        assert "consequential actions" in message.content
        assert "trusted connected" in message.content
        assert "Do not invent background work" in message.content
        assert "owner-approved, relevant memory" in message.content
        assert "configured licensed voice" in message.content
        assert "Tailor priorities to the owner" in message.content
        assert len(message.content) < 2500
        for forbidden in ("JARVIS", "FRIDAY", "ULTRON", "Tony Stark", "Hello, sir"):
            assert forbidden not in message.content
    assert "independently verified" in persona_message(ReplyMode.WORK).content
    assert "nonessential" in persona_message(ReplyMode.FOCUS).content
    assert "calendar events" in persona_message(ReplyMode.BRIEFING).content
    assert "trusted sources actually supplied" in persona_message(ReplyMode.BRIEFING).content


def test_persona_preserves_gateway_policy_and_does_not_modify_input() -> None:
    original = request(ChatMessage("user", "Synthetic hello"))
    modified = with_persona(original, mode=ReplyMode.WORK)
    assert original.messages == (ChatMessage("user", "Synthetic hello"),)
    assert modified.messages[0].role == "system"
    assert modified.messages[1] == original.messages[0]
    assert modified.request_id == original.request_id
    assert modified.cloud_allowed == original.cloud_allowed
    assert modified.local_only == original.local_only
    assert modified.privacy == original.privacy
    assert modified.max_cost_usd == original.max_cost_usd
    assert modified.required_capabilities == original.required_capabilities


def test_persona_rejects_existing_system_instruction_or_over_budget() -> None:
    with pytest.raises(PersonaError, match="existing_system_instructions"):
        with_persona(request(ChatMessage("system", "already set"), ChatMessage("user", "hello")))
    with pytest.raises(PersonaError, match="invalid_reply_mode"):
        persona_message("work")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        with_persona(request(ChatMessage("user", "hi"), input_limit=64))


def test_voice_is_feminine_preference_with_no_unapproved_provider() -> None:
    default = VoicePreference()
    assert default.presentation == "feminine" and default.locale == "en-GB"
    assert not default.selected and default.provider_id is None and default.voice_id is None
    assert VoicePreference(provider_id="licensed", voice_id="owner-selected").selected
    assert VoicePreference(locale="en-US").locale == "en-US"
    with pytest.raises(PersonaError, match="invalid_voice_locale"):
        VoicePreference(locale="en-GB\\nunsafe")
    with pytest.raises(PersonaError, match="incomplete_voice_selection"):
        VoicePreference(provider_id="licensed")
    with pytest.raises(PersonaError, match="invalid_voice_selection"):
        VoicePreference(provider_id="licensed", voice_id="unsafe newline\n")
    with pytest.raises(PersonaError, match="unsupported_voice_preference"):
        VoicePreference(presentation="masculine")


def _config() -> dict[str, str]:
    return {
        "MAX_TRIAL_DESTINATION": "cloud",
        "MAX_TRIAL_CLOUD_ALLOWED": "yes",
        "MAX_TRIAL_ENDPOINT": "https://example.com/v1/chat/completions",
        "MAX_TRIAL_MODEL": "reviewed-model",
        "MAX_TRIAL_CONTEXT_TOKENS": "8192",
        "MAX_TRIAL_API_KEY": "synthetic-test-key",
        "MAX_TRIAL_MAX_COST_USD": "0.01",
        "MAX_TRIAL_INPUT_USD_PER_MILLION": "1",
        "MAX_TRIAL_OUTPUT_USD_PER_MILLION": "2",
    }


def test_trial_persona_explicit_opt_in_and_bad_flag_rejected() -> None:
    assert not TrialSettings.from_env(_config()).persona_enabled
    enabled = _config()
    enabled["MAX_TRIAL_PERSONA"] = "yes"
    assert TrialSettings.from_env(enabled).persona_enabled
    enabled["MAX_TRIAL_PERSONA"] = "maybe"
    with pytest.raises(TrialConfigurationError, match="invalid_persona_toggle"):
        TrialSettings.from_env(enabled)


def test_opted_in_mock_trial_sends_persona_but_never_executes_tools() -> None:
    config = _config()
    config["MAX_TRIAL_PERSONA"] = "yes"
    settings = TrialSettings.from_env(config)
    seen: list[list[dict[str, str]]] = []

    def reply(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        seen.append(payload["messages"])
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1] == {"role": "user", "content": "Synthetic hello"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Understood.",
                            "tool_calls": [
                                {
                                    "id": "synthetic-call",
                                    "type": "function",
                                    "function": {
                                        "name": "no_permitted_execution",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 180, "completion_tokens": 8},
            },
        )

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            return await ask_once(settings, "Synthetic hello", client=client)

    assert asyncio.run(run()) == "Understood."
    assert len(seen) == 1 and len(seen[0]) == 2
