"""Configuration: env parsing, .env seeding, secret redaction."""

import os

from max.config import Config, load_dotenv


def test_defaults_are_offline(tmp_path):
    config = Config.from_env(repo_root=tmp_path, apply_dotenv=False)
    assert config.provider == "mock"
    assert config.base_url == ""
    assert config.model == ""


def test_env_file_fills_gaps_but_env_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        'FROM_FILE=abc\nOVERRIDE=from-file\nMAX_PROVIDER=openai\n# comment\nEMPTY=\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OVERRIDE", "from-env")

    loaded = load_dotenv(tmp_path / ".env")
    assert loaded["FROM_FILE"] == "abc"
    assert loaded["OVERRIDE"] == "from-file"  # raw parse
    assert "EMPTY" in loaded

    monkeypatch.setenv("OVERRIDE", "from-env")
    monkeypatch.delenv("MAX_PROVIDER", raising=False)
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    assert os.environ["OVERRIDE"] == "from-env"
    assert os.environ["MAX_PROVIDER"] == "openai"
    monkeypatch.delenv("MAX_PROVIDER")
    monkeypatch.delenv("OVERRIDE", raising=False)


def test_api_key_read_from_named_env_var_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_API_KEY_ENV", "MY_KEY")
    monkeypatch.setenv("MY_KEY", "super-secret-value")
    config = Config.from_env(repo_root=tmp_path, apply_dotenv=False)
    assert config.api_key == "super-secret-value"


def test_redacted_never_contains_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_API_KEY_ENV", "MY_KEY")
    monkeypatch.setenv("MY_KEY", "super-secret-value")
    monkeypatch.setenv("MAX_OPENCLAW_TOKEN", "gateway-token-value")
    monkeypatch.setenv("MAX_BASE_URL", "http://127.0.0.1:9999/v1")
    config = Config.from_env(repo_root=tmp_path, apply_dotenv=False)
    snapshot = repr(config.redacted())
    assert "super-secret-value" not in snapshot
    assert "gateway-token-value" not in snapshot
    assert config.redacted()["api_key_set"] is True
    assert config.redacted()["openclaw_token_set"] is True

def test_legacy_railway_ai_settings_select_real_provider(tmp_path, monkeypatch):
    # Existing Railway deployments use AI_* instead of MAX_*.
    for key in ("MAX_PROVIDER", "MAX_BASE_URL", "MAX_MODEL", "MAX_API_KEY_ENV"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")
    monkeypatch.setenv("AI_API_KEY", "testing-not-a-real-key")
    cfg = Config.from_env(repo_root=tmp_path, apply_dotenv=False)
    assert cfg.provider == "openai"
    assert cfg.base_url == "https://example.invalid/v1"
    assert cfg.model == "test-model"
    assert cfg.api_key == "testing-not-a-real-key"
    assert "testing-not-a-real-key" not in repr(cfg.redacted())

    # An explicit MAX configuration always wins.
    monkeypatch.setenv("MAX_PROVIDER", "mock")
    monkeypatch.setenv("MAX_MODEL", "custom-model")
    cfg = Config.from_env(repo_root=tmp_path, apply_dotenv=False)
    assert cfg.provider == "mock"
    assert cfg.model == "custom-model"
