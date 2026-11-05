from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from max_core.observability import SafeJsonFormatter
from tests.conftest import TEST_TOKEN


def test_liveness_and_bootstrap_readiness(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "alive"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "scope": "bootstrap-only"}


def test_owner_status_requires_valid_token(client: TestClient) -> None:
    result = client.get("/v1/status", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert result.status_code == 200
    assert result.json() == {
        "principal": "owner",
        "assistant": "MAX",
        "phase": "secure-core-bootstrap",
    }
    assert len(result.headers["X-Request-ID"]) == 32


@pytest.mark.parametrize(
    "header",
    [None, "Bearer wrong", "Basic invalid", "Bearer ", "Bearer x y", "Bearer\tinvalid"],
)
def test_authentication_fails_closed(client: TestClient, header: str | None) -> None:
    headers = {} if header is None else {"Authorization": header}
    for path in ("/v1/session", "/v1/status"):
        response = client.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_duplicate_authorization_header_rejected(client: TestClient) -> None:
    response = client.get(
        "/v1/status",
        headers=[
            ("Authorization", f"Bearer {TEST_TOKEN}"),
            ("Authorization", f"Bearer {TEST_TOKEN}"),
        ],
    )
    assert response.status_code == 401


def test_spoofed_host_rejected(client: TestClient) -> None:
    response = client.get("/health/live", headers={"Host": "external.example"})
    assert response.status_code == 400


def test_no_public_docs_or_openapi(client: TestClient) -> None:
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404


def test_unknown_route_does_not_reflect_token(client: TestClient) -> None:
    response = client.get("/sensitive-in-path/" + TEST_TOKEN + "?key=" + TEST_TOKEN)
    assert response.status_code == 404
    assert TEST_TOKEN not in response.text


def test_safe_json_formatter_discards_untrusted_message_and_extra_data() -> None:
    record = logging.LogRecord("max_core.access", logging.INFO, __file__, 1, TEST_TOKEN, (), None)
    record.secret = TEST_TOKEN
    record.event = "http_request"
    record.method = "GET"
    record.route = "/v1/status"
    record.status_code = 200
    payload = SafeJsonFormatter().format(record)
    assert TEST_TOKEN not in payload
    assert json.loads(payload)["route"] == "/v1/status"
    assert "secret" not in json.loads(payload)


def test_unexpected_failure_returns_generic_500_without_exposing_exception(
    client: TestClient,
) -> None:
    @client.app.get("/v1/explode")
    async def explode() -> None:
        raise RuntimeError("internal-value-should-stay-private")

    response = client.get("/v1/explode")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "internal-value" not in response.text
    assert len(response.headers["X-Request-ID"]) == 32


def test_request_logging_omits_token_and_query_values(client: TestClient) -> None:
    logger = logging.getLogger("max_core.access")
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    capture = io.StringIO()
    original_stream = handler.stream
    handler.setStream(capture)
    try:
        client.get(
            "/v1/status?private=" + TEST_TOKEN,
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
    finally:
        handler.setStream(original_stream)
    output = capture.getvalue()
    assert TEST_TOKEN not in output
    assert "private" not in output
    assert '"route":"/v1/status"' in output
