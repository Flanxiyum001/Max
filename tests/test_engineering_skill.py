"""First-party MAX engineering guidance is scoped and non-authoritative."""

from __future__ import annotations

from max_core.chat import session_system_messages
from max_core.engineering_skill import ENGINEERING_DISCIPLINE
from max_core.gateway import ChatMessage, GatewayRequest, Privacy
from max_core.gateway_trial import TrialSettings
from max_core.persona import ReplyMode, persona_message, with_persona


def _settings(*, enabled: bool) -> TrialSettings:
    return TrialSettings.from_env(
        {
            "MAX_TRIAL_DESTINATION": "verified_local",
            "MAX_TRIAL_LOCAL_EGRESS_REVIEWED": "yes",
            "MAX_TRIAL_ENDPOINT_LOCAL": "yes",
            "MAX_TRIAL_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions",
            "MAX_TRIAL_MODEL": "synthetic-local",
            "MAX_TRIAL_CONTEXT_TOKENS": "8192",
            "MAX_TRIAL_PERSONA": "yes" if enabled else "no",
        }
    )


def test_engineering_mode_is_explicit_and_isolated_from_normal_persona() -> None:
    for mode in ReplyMode:
        content = persona_message(mode).content
        assert ("single-owner development partner" in content) == (
            mode == ReplyMode.ENGINEERING
        )
        assert "You are MAX" in content
        assert len(content) < 2500
    assert ENGINEERING_DISCIPLINE in persona_message(ReplyMode.ENGINEERING).content


def test_engineering_guidance_never_changes_gateway_admission_or_user_text() -> None:
    original = GatewayRequest(
        request_id="engineering-guidance-1",
        messages=(ChatMessage("user", "Review only the tests"),),
        privacy=Privacy.CONFIDENTIAL,
        max_input_tokens=4096,
        max_output_tokens=256,
        cloud_allowed=False,
        local_only=True,
        allow_fallback=False,
    )
    modified = with_persona(original, mode=ReplyMode.ENGINEERING)
    assert original.messages == (ChatMessage("user", "Review only the tests"),)
    assert modified.messages[0].role == "system"
    assert modified.messages[1:] == original.messages
    assert modified.privacy == original.privacy
    assert modified.cloud_allowed == original.cloud_allowed
    assert modified.local_only == original.local_only
    assert modified.allow_fallback == original.allow_fallback
    assert modified.max_input_tokens == original.max_input_tokens
    assert modified.max_output_tokens == original.max_output_tokens
    assert modified.request_id == original.request_id


def test_terminal_skill_requires_persona_opt_in_and_selected_mode() -> None:
    opted_out = _settings(enabled=False)
    opted_in = _settings(enabled=True)
    assert session_system_messages(opted_out, ReplyMode.ENGINEERING) == []
    conversation = session_system_messages(opted_in, ReplyMode.CONVERSATION)
    engineering = session_system_messages(opted_in, ReplyMode.ENGINEERING)
    assert len(conversation) == len(engineering) == 1
    assert "single-owner development partner" not in conversation[0].content
    assert "single-owner development partner" in engineering[0].content
    assert conversation[0].role == engineering[0].role == "system"


def test_guidance_does_not_claim_tool_execution_or_imply_owner_approval() -> None:
    text = ENGINEERING_DISCIPLINE
    assert "Never call a PR mergeable" in text
    assert "Real permissions remain host-enforced" in text
    assert "owner-controlled merge and deployment" in text
