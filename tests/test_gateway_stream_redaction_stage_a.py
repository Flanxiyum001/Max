"""No raw transport exceptions may leak through streaming timeouts."""

from __future__ import annotations

import asyncio
import traceback

import httpx
import pytest

from max_core.gateway import Gateway, GatewayError, HTTPAdapter
from tests.test_gateway import local, request


def test_stream_timeout_traceback_suppresses_transport_exception() -> None:
    sensitive = "SENSITIVE_TRANSPORT_TIMEOUT_AND_REQUEST_DETAILS"

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(sensitive, request=req)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = Gateway([local()], HTTPAdapter(client))
            async for _ in gateway.stream(request()):
                pass

    with pytest.raises(GatewayError) as error:
        asyncio.run(run())
    assert error.value.code == "timeout"
    assert error.value.__suppress_context__ is True
    assert sensitive not in str(error.value)
    assert sensitive not in "".join(traceback.format_exception(error.value))
