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


def test_speech_only_uses_user_checkpoint_and_reference(monkeypatch, tmp_path):
    from max_core.voice import local_runtime as local

    checkpoint = tmp_path / "weights"
    checkpoint.mkdir()
    (checkpoint / "config.json").write_text("{}", encoding="utf-8")
    reference = tmp_path / "reference-audio.wav"
    reference.write_bytes(b"RIFF")
    transcript = tmp_path / "reference-text.txt"
    transcript.write_text("This is my own voice.", encoding="utf-8")
    monkeypatch.setenv("MAX_LOCAL_TTS", "qwen")
    monkeypatch.setenv("MAX_QWEN_MODEL_DIR", str(checkpoint))
    monkeypatch.setenv("MAX_QWEN_REFERENCE_WAV", str(reference))
    monkeypatch.setenv("MAX_QWEN_REFERENCE_TEXT_FILE", str(transcript))
    assert local.selected_voice_assets() == (checkpoint, reference, "This is my own voice.")


def test_speech_only_reuses_existing_original(monkeypatch, tmp_path):
    from max_core.voice import local_runtime as local

    checkpoint = tmp_path / "Models" / "Qwen3-TTS-12Hz-1.7B-Base"
    checkpoint.mkdir(parents=True)
    (checkpoint / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(local.Path, "home", lambda: tmp_path)
    data = tmp_path / "app-data"
    data.mkdir()
    reference = data / "reference.wav"
    reference.write_bytes(b"RIFF")
    monkeypatch.setattr(local, "voice_dir", lambda: data)
    monkeypatch.setenv("MAX_LOCAL_TTS", "qwen")
    for name in ("MAX_QWEN_MODEL_DIR", "MAX_QWEN_REFERENCE_WAV", "MAX_QWEN_REFERENCE_TEXT_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert local.selected_voice_assets() == (checkpoint, reference, local.REFERENCE)
