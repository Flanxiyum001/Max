from max_core.voice.local_runtime import CHAT, REASON, local_settings


def test_local_profile_routes_to_installed_models():
    settings = local_settings({CHAT, REASON})
    assert settings["MAX_ROUTE_CHAT"] == CHAT
    assert settings["MAX_ROUTE_REASONING"] == REASON
    assert settings["MAX_TRIAL_ENDPOINT"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert settings["MAX_TRIAL_CLOUD_ALLOWED"] == "no"
    assert settings["MAX_TRIAL_API_KEY"] == ""
    assert settings["MAX_TRIAL_MAX_COST_USD"] == "0"
    assert settings["HF_HUB_OFFLINE"] == "1"


def test_local_profile_does_not_require_second_model():
    assert local_settings({CHAT})["MAX_ROUTE_CODE"] == CHAT
