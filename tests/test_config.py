from __future__ import annotations

import pytest

from max_core.config import ConfigurationError, Settings
from tests.conftest import TEST_TOKEN


def test_missing_token_fails_closed() -> None:
    with pytest.raises(ConfigurationError, match="MAX_API_BEARER_TOKEN"):
        Settings.from_env({})


@pytest.mark.parametrize(
    "token", ["short", "A" * 50, "valid token with spaces" + "0123456789abcdef"]
)
def test_weak_or_whitespace_token_rejected(token: str) -> None:
    with pytest.raises(ConfigurationError, match="MAX_API_BEARER_TOKEN"):
        Settings(bearer_token=token)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "192.168.1.2"])
def test_non_loopback_binding_not_allowed(host: str) -> None:
    with pytest.raises(ConfigurationError, match="MAX_BIND_HOST"):
        Settings(bearer_token=TEST_TOKEN, bind_host=host)


@pytest.mark.parametrize("port", [0, 22, 65536])
def test_unsafe_port_rejected(port: int) -> None:
    with pytest.raises(ConfigurationError, match="MAX_BIND_PORT"):
        Settings(bearer_token=TEST_TOKEN, bind_port=port)


def test_non_integer_port_rejected_without_echo() -> None:
    with pytest.raises(ConfigurationError, match="MAX_BIND_PORT") as error:
        Settings.from_env({"MAX_API_BEARER_TOKEN": TEST_TOKEN, "MAX_BIND_PORT": "secret!"})
    assert "secret!" not in str(error.value)


def test_valid_env_and_no_secret_in_repr() -> None:
    settings = Settings.from_env(
        {"MAX_API_BEARER_TOKEN": TEST_TOKEN, "MAX_BIND_PORT": "8766", "MAX_LOG_LEVEL": "warning"}
    )
    assert settings.bind_port == 8766
    assert settings.log_level == "WARNING"
    assert TEST_TOKEN not in repr(settings)


def test_invalid_log_level() -> None:
    with pytest.raises(ConfigurationError, match="MAX_LOG_LEVEL"):
        Settings(bearer_token=TEST_TOKEN, log_level="TRACE")


@pytest.mark.parametrize(
    "url", ["sqlite:///test.db", "postgresql://localhost/test", "file:///tmp/test.db"]
)
def test_task_database_must_use_postgresql_psycopg(url: str) -> None:
    with pytest.raises(ConfigurationError, match="MAX_DATABASE_URL"):
        Settings(bearer_token=TEST_TOKEN, database_url=url)


def test_database_url_is_not_printed_in_settings_repr() -> None:
    settings = Settings(
        bearer_token=TEST_TOKEN, database_url="postgresql+psycopg://example@localhost/test"
    )
    assert "postgresql+psycopg" not in repr(settings)


@pytest.mark.parametrize(
    "token",
    [
        "0123456789abcdef" * 4,
        "3fA9zQ2mKd0wXyVb8nCe4rTg1uJpL5hS7dGnXwRt" + "0123456789abcdefghij",
        "k8Wq2Nv5Rt7Zx3Mb9Cd1Ef4Gh6Jk0Lm2Pq5rSt8uVwx" + "Yz34",
    ],
)
def test_generated_random_tokens_accepted(token: str) -> None:
    settings = Settings(bearer_token=token)
    assert settings.bearer_token == token


@pytest.mark.parametrize(
    "token",
    [
        "correct horse battery staple" + "0123456789abcdef0123456789",
        "correct-horse-battery-staple-plus-more-padding",
    ],
)
def test_passphrase_like_tokens_rejected(token: str) -> None:
    with pytest.raises(ConfigurationError, match="MAX_API_BEARER_TOKEN"):
        Settings(bearer_token=token)
