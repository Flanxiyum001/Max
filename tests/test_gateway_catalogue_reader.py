"""Read-only catalogue transport tests: HTTPX mocks only, zero live network calls."""

from __future__ import annotations

import asyncio
import traceback

import httpx
import pytest

from max_core.gateway.catalogue import AvailabilityState, CapabilitySupport
from max_core.gateway.catalogue_reader import (
    CatalogueFetchError,
    ReadOnlyCatalogueReader,
    ReadOnlyCatalogueScope,
)


def scope(**changes: object) -> ReadOnlyCatalogueScope:
    values = dict(
        gateway_id="gw-owner-reviewed",
        url="https://models.example.test/v1/models",
        owner_approval_revision="approved-config-1",
        enabled=True,
    )
    values.update(changes)
    return ReadOnlyCatalogueScope(**values)


def listing(**extra: object) -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "object": "model",
                "id": "model-one",
                "owned_by": "vendor-one",
                "context_window": 8192,
                "execution_status": "ready",
                "available": True,
                **extra,
            }
        ],
    }


def invoke(
    handler: httpx.MockTransport,
    approval: ReadOnlyCatalogueScope,
    *,
    authorization_factory: object = None,
) -> object:
    async def run() -> object:
        async with httpx.AsyncClient(transport=handler) as client:
            provider = authorization_factory if callable(authorization_factory) else None
            reader = ReadOnlyCatalogueReader(client, authorization_factory=provider)
            return await reader.fetch(approval, observed_at_epoch=200)

    return asyncio.run(run())


def test_reader_requires_explicit_owner_approval_before_auth_or_network() -> None:
    network_calls = 0
    credential_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json=listing())

    def credentials() -> str:
        nonlocal credential_calls
        credential_calls += 1
        return "Bearer TEST_ONLY"

    with pytest.raises(CatalogueFetchError, match="catalogue_access_not_approved"):
        invoke(httpx.MockTransport(handler), scope(enabled=False), authorization_factory=credentials)
    with pytest.raises(CatalogueFetchError, match="catalogue_access_not_approved"):
        invoke(
            httpx.MockTransport(handler),
            scope(owner_approval_revision=None),
            authorization_factory=credentials,
        )
    assert network_calls == 0
    assert credential_calls == 0


def test_approved_read_is_get_only_and_discards_secret_payload_fields() -> None:
    secret = "SENSITIVE_FAKE_ONLY_DO_NOT_EXPOSE"
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer " + secret
        return httpx.Response(200, json=listing(api_key=secret, approval="approved"))

    imported = invoke(
        httpx.MockTransport(handler),
        scope(),
        authorization_factory=lambda: "Bearer " + secret,
    )
    assert len(captured) == 1
    assert imported.snapshot.models[0].identity.model_id == "model-one"
    assert imported.snapshot.routes[0].availability.state == AvailabilityState.READY
    assert imported.snapshot.models[0].capabilities.state("text") == CapabilitySupport.UNKNOWN
    assert secret not in repr(imported)
    assert secret not in repr(scope())
    assert not hasattr(imported.snapshot.routes[0], "approval")


@pytest.mark.parametrize(
    "url",
    [
        "http://models.example.test/v1/models",
        "https://someone:secret@models.example.test/v1/models",
        "https://models.example.test/v1/models?token=credential",
        "https://models.example.test/v1/models#fragment",
        "https://localhost/v1/models",
        "https://127.0.0.1/v1/models",
        "https://192.168.1.1/v1/models",
        "https://models.example.test/other",
    ],
)
def test_scope_rejects_untrusted_or_secret_bearing_urls(url: str) -> None:
    with pytest.raises(CatalogueFetchError, match="invalid_catalogue_endpoint"):
        scope(url=url)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (302, "catalogue_redirect_denied"),
        (401, "catalogue_auth_denied"),
        (403, "catalogue_auth_denied"),
        (404, "catalogue_unavailable"),
        (429, "catalogue_unavailable"),
        (500, "catalogue_unavailable"),
    ],
)
def test_errors_are_sanitized(status_code: int, expected: str) -> None:
    redirect = {"location": "https://credential-harvesting.invalid/v1/models"}
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status_code, headers=redirect, text="SECRET_RESPONSE_BODY")
    )
    with pytest.raises(CatalogueFetchError, match=expected) as error:
        invoke(transport, scope())
    assert "SECRET_RESPONSE_BODY" not in str(error.value)


def test_header_injection_and_callback_failure_are_sanitized() -> None:
    def fail() -> str:
        raise RuntimeError("SENSITIVE_CREDENTIAL_STORE_ERROR")

    handler = httpx.MockTransport(lambda _: httpx.Response(200, json=listing()))
    with pytest.raises(CatalogueFetchError, match="catalogue_authorization_failed") as error:
        invoke(handler, scope(), authorization_factory=fail)
    assert error.value.__suppress_context__
    assert "SENSITIVE_CREDENTIAL_STORE_ERROR" not in "".join(
        traceback.format_exception(error.value)
    )
    with pytest.raises(CatalogueFetchError, match="catalogue_authorization_failed"):
        invoke(handler, scope(), authorization_factory=lambda: "Bearer\nsecret")


def test_response_content_type_length_and_body_size_bound() -> None:
    cases = (
        (httpx.Response(200, text="hello"), "catalogue_invalid_content_type"),
        (
            httpx.Response(200, headers={"content-type": "application/json", "content-length": "1000"}, content=b"{}"),
            "catalogue_too_large",
        ),
        (
            httpx.Response(200, headers={"content-type": "application/json", "content-length": "NaN"}, content=b"{}"),
            "catalogue_invalid_length",
        ),
        (httpx.Response(200, json=listing()), "catalogue_too_large"),
    )
    for response, error_code in cases:
        with pytest.raises(CatalogueFetchError, match=error_code):
            invoke(httpx.MockTransport(lambda _, r=response: r), scope(max_response_bytes=20))


def test_invalid_json_and_unknown_schema_fail_closed() -> None:
    for response, code in (
        (
            httpx.Response(200, content=b"not json", headers={"content-type": "application/json"}),
            "catalogue_invalid_json",
        ),
        (httpx.Response(200, json={"object": "list", "data": [123]}), "catalogue_invalid_payload"),
    ):
        with pytest.raises(CatalogueFetchError, match=code):
            invoke(httpx.MockTransport(lambda _, r=response: r), scope())


def test_transport_timeout_never_exposes_exception_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("SENSITIVE_TIMEOUT_AND_HOST", request=request)

    with pytest.raises(CatalogueFetchError, match="catalogue_timeout") as error:
        invoke(httpx.MockTransport(handler), scope())
    assert error.value.__suppress_context__
    assert "SENSITIVE_TIMEOUT_AND_HOST" not in "".join(
        traceback.format_exception(error.value)
    )
