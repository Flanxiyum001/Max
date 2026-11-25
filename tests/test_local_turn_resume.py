from max_core.voice.local_runtime import CHAT, REASON, local_settings
from max_core.voice.turn_resume import SpeechCursor, is_resume_request


def test_gaming_routes_to_small_model():
    settings = local_settings({CHAT, REASON}, gaming=True)
    assert settings["MAX_LOCAL_GAMING"] == "yes"
    for route in ("QUICK", "CHAT", "CODE", "REASONING"):
        assert settings[f"MAX_ROUTE_{route}"] == CHAT
    assert settings["MAX_TRIAL_CLOUD_ALLOWED"] == "no"


def test_normal_profile_retains_reasoning_route():
    settings = local_settings({CHAT, REASON})
    assert settings["MAX_LOCAL_GAMING"] == "no"
    assert settings["MAX_ROUTE_REASONING"] == REASON


def test_resume_replays_only_interrupted_sentence():
    cursor = SpeechCursor()
    cursor.begin("First sentence. Second sentence! Third sentence?")
    assert cursor.current == "First sentence."
    cursor.advance()
    assert cursor.current == "Second sentence!"
    assert cursor.current == "Second sentence!"
    cursor.advance()
    assert cursor.current == "Third sentence?"
    cursor.advance()
    assert cursor.current == ""
    cursor.clear()
    assert cursor.current == ""


def test_resume_only_explicit_short_commands():
    assert is_resume_request("Continue, please.")
    assert is_resume_request("Go on!")
    assert not is_resume_request("Continue with a different task")
    assert not is_resume_request("No, don't continue")
    assert not is_resume_request("")
