"""OpenAI-compatible adapter: parsing, classification, timeouts, no leaks."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from max.providers import (
    MockProvider,
    OpenAICompatProvider,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimit,
    ProviderResult,
    ProviderServerError,
    ProviderTimeout,
    build_provider,
)

KEY = "sk-test-abcdef1234567890-SECRETVALUE"


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, n: int = -1) -> bytes:
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_opener(items):
    """opener(request, timeout=...) serving ``items`` in order (bytes | Exception)."""
    calls = []

    def opener(request, timeout=None):
        calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "auth": request.get_header("Authorization"),
                "body": json.loads(request.data.decode("utf-8")) if request.data else {},
            }
        )
        item = items.pop(0) if items else b"{}"
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    opener.calls = calls
    return opener


def http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


def provider(opener, retries: int = 0) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url="http://127.0.0.1:9999/v1",
        model="test-model",
        api_key=KEY,
        timeout=5,
        retries=retries,
        opener=opener,
        sleep=lambda _s: None,
    )


# ------------------------------------------------------------------ config
def test_missing_base_url_is_config_error():
    with pytest.raises(ProviderConfigError):
        OpenAICompatProvider(base_url="", model="m")
    with pytest.raises(ProviderConfigError):
        build_provider(type("C", (), {"provider": "openai", "base_url": "", "model": "m", "api_key": None})())


def test_build_provider_requires_model_for_openai():
    class C:
        provider = "openai"
        base_url = "http://example.invalid/v1"
        model = ""
        api_key = None

    with pytest.raises(ProviderConfigError):
        build_provider(C())


# ----------------------------------------------------------------- success
def test_successful_text_completion():
    body = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": "hi there"}}], "usage": {"total_tokens": 9}}
    ).encode()
    opener = make_opener([body])
    result = provider(opener).complete([{"role": "user", "content": "hello"}])
    assert isinstance(result, ProviderResult)
    assert result.text == "hi there"
    assert result.usage["total_tokens"] == 9
    assert opener.calls[0]["url"] == "http://127.0.0.1:9999/v1/chat/completions"
    assert opener.calls[0]["timeout"] == 5
    assert opener.calls[0]["auth"] == f"Bearer {KEY}"


def test_tool_calls_parsed_and_arguments_json_decoded():
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                            }
                        ],
                    }
                }
            ]
        }
    ).encode()
    result = provider(make_opener([body])).complete([{"role": "user", "content": "x"}])
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == {"path": "a.txt"}


def test_tools_are_sent_in_openai_shape():
    body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
    opener = make_opener([body])
    provider(opener).complete(
        [{"role": "user", "content": "x"}],
        tools=[{"name": "read_file", "description": "d", "parameters": {"type": "object"}}],
    )
    sent = opener.calls[0]["body"]
    assert sent["model"] == "test-model"
    assert sent["tools"][0]["function"]["name"] == "read_file"
    assert sent["messages"] == [{"role": "user", "content": "x"}]


# ------------------------------------------------------------------ errors
def test_http_401_is_auth_error_and_key_never_leaks():
    opener = make_opener([http_error(401, b'{"error": "invalid key"}')])
    with pytest.raises(ProviderAuthError) as exc:
        provider(opener).complete([{"role": "user", "content": "x"}])
    assert "401" in str(exc.value)
    assert KEY not in str(exc.value)
    assert exc.value.retryable is False


def test_http_429_is_rate_limit_with_retry():
    body = json.dumps({"choices": [{"message": {"content": "finally"}}]}).encode()
    opener = make_opener([http_error(429, b"slow down"), body])
    result = provider(opener, retries=1).complete([{"role": "user", "content": "x"}])
    assert result.text == "finally"
    assert len(opener.calls) == 2

    with pytest.raises(ProviderRateLimit):
        provider(make_opener([http_error(429, b"slow")]), retries=0).complete([])


def test_http_500_is_server_error():
    with pytest.raises(ProviderServerError) as exc:
        provider(make_opener([http_error(503, b"unavailable")])).complete([])
    assert "503" in str(exc.value)
    assert exc.value.retryable is True


def test_connection_refused_is_classified():
    import urllib.error

    err = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    with pytest.raises(ProviderServerError) as exc:
        provider(make_opener([err])).complete([])
    assert exc.value.code == "connection_error"


def test_timeout_is_classified():
    import socket
    import urllib.error

    err = urllib.error.URLError(socket.timeout("timed out"))
    with pytest.raises(ProviderTimeout):
        provider(make_opener([err])).complete([])


def test_invalid_json_is_protocol_error():
    with pytest.raises(ProviderServerError) as exc:
        provider(make_opener([b"<html>not json</html>"])).complete([])
    assert exc.value.code == "bad_json"


def test_provider_error_never_crashes_caller():
    """Every failure path raises ProviderError, which the agent catches."""
    opener = make_opener([http_error(400, b"bad request")])
    with pytest.raises(ProviderError):
        provider(opener).complete([])


# -------------------------------------------------------------------- mock
def test_mock_provider_is_deterministic_and_records_requests():
    mock = MockProvider(script=[{"text": "one"}, {"text": "two"}])
    assert mock.complete([{"role": "user", "content": "a"}]).text == "one"
    assert mock.complete([{"role": "user", "content": "b"}]).text == "two"
    assert mock.complete([{"role": "user", "content": "c"}]).text == "MOCK_REPLY: c"
    assert len(mock.requests) == 3


def test_mock_provider_can_fail_then_recover():
    mock = MockProvider(script=[{"error": "rate_limited"}, {"text": "back"}])
    with pytest.raises(ProviderRateLimit):
        mock.complete([])
    assert mock.complete([]).text == "back"
