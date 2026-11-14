"""Trusted read-only catalogue transport; deliberately not wired into any runtime.

An explicit owner-reviewed scope and a caller-supplied HTTP client are required.
No credentials, raw payloads, or provider errors escape into selection metadata.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from max_core.gateway.catalogue_discovery import (
    MAX_CATALOGUE_BYTES,
    CatalogueImport,
    CatalogueImportError,
    parse_freellmapi_models,
)


class CatalogueFetchError(ValueError):
    """Safe, fixed error code; no URLs, headers or response bodies."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ReadOnlyCatalogueScope:
    gateway_id: str
    url: str
    owner_approval_revision: str | None = None
    enabled: bool = False
    timeout_seconds: float = 5.0
    max_response_bytes: int = MAX_CATALOGUE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.gateway_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,100}", self.gateway_id
        ):
            raise CatalogueFetchError("invalid_gateway_scope")
        if type(self.enabled) is not bool:
            raise CatalogueFetchError("invalid_enabled_flag")
        if self.owner_approval_revision is not None and (
            not isinstance(self.owner_approval_revision, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.owner_approval_revision)
        ):
            raise CatalogueFetchError("invalid_approval_revision")
        if not isinstance(self.url, str):
            raise CatalogueFetchError("invalid_catalogue_endpoint")
        try:
            parsed = urlsplit(self.url)
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            raise CatalogueFetchError("invalid_catalogue_endpoint") from None
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/v1/models"
            or "\\" in self.url
            or "@" in parsed.netloc
        ):
            raise CatalogueFetchError("invalid_catalogue_endpoint")
        if host == "localhost" or host.endswith(".local") or host.endswith(".localhost"):
            raise CatalogueFetchError("invalid_catalogue_endpoint")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass  # DNS hosts must be pinned/restricted by trusted network policy at deployment.
        else:
            if not ip.is_global:
                raise CatalogueFetchError("invalid_catalogue_endpoint")
        if (
            type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= MAX_CATALOGUE_BYTES
        ):
            raise CatalogueFetchError("invalid_catalogue_size_limit")
        if (
            not isinstance(self.timeout_seconds, (float, int))
            or isinstance(self.timeout_seconds, bool)
            or not 0 < self.timeout_seconds <= 15
        ):
            raise CatalogueFetchError("invalid_catalogue_timeout")


class ReadOnlyCatalogueReader:
    """No hidden network work: fetch is the sole I/O method.

    authorization_factory stays strictly in this trusted transport boundary;
    it is never called unless an approved, enabled fetch is requested.
    """

    __slots__ = ("_client", "_authorization_factory")

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        authorization_factory: Callable[[], str | None] | None = None,
    ) -> None:
        self._client = client
        self._authorization_factory = authorization_factory

    async def fetch(
        self, scope: ReadOnlyCatalogueScope, *, observed_at_epoch: int | None = None
    ) -> CatalogueImport:
        if not isinstance(scope, ReadOnlyCatalogueScope):
            raise CatalogueFetchError("invalid_catalogue_scope")
        if not scope.enabled or scope.owner_approval_revision is None:
            raise CatalogueFetchError("catalogue_access_not_approved")
        headers = {"Accept": "application/json"}
        if self._authorization_factory is not None:
            try:
                authorization = self._authorization_factory()
                if authorization is not None:
                    if (
                        not isinstance(authorization, str)
                        or not authorization
                        or "\r" in authorization
                        or "\n" in authorization
                    ):
                        raise CatalogueFetchError("invalid_catalogue_authorization")
                    headers["Authorization"] = authorization
            except Exception:
                raise CatalogueFetchError("catalogue_authorization_failed") from None
        try:
            async with self._client.stream(
                "GET",
                scope.url,
                headers=headers,
                follow_redirects=False,
                timeout=scope.timeout_seconds,
            ) as response:
                if response.is_redirect:
                    raise CatalogueFetchError("catalogue_redirect_denied")
                if response.status_code == 401 or response.status_code == 403:
                    raise CatalogueFetchError("catalogue_auth_denied")
                if response.status_code != 200:
                    raise CatalogueFetchError("catalogue_unavailable")
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
                    "application/json"
                ):
                    raise CatalogueFetchError("catalogue_invalid_content_type")
                size = response.headers.get("content-length")
                if size is not None:
                    try:
                        length = int(size)
                    except ValueError:
                        raise CatalogueFetchError("catalogue_invalid_length") from None
                    if length < 0 or length > scope.max_response_bytes:
                        raise CatalogueFetchError("catalogue_too_large")
                body = bytearray()
                async for piece in response.aiter_bytes():
                    if len(body) + len(piece) > scope.max_response_bytes:
                        raise CatalogueFetchError("catalogue_too_large")
                    body.extend(piece)
        except CatalogueFetchError:
            raise
        except httpx.TimeoutException:
            raise CatalogueFetchError("catalogue_timeout") from None
        except httpx.HTTPError:
            raise CatalogueFetchError("catalogue_transport_failure") from None

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise CatalogueFetchError("catalogue_invalid_json") from None
        try:
            return parse_freellmapi_models(
                decoded, gateway_id=scope.gateway_id, observed_at_epoch=observed_at_epoch
            )
        except CatalogueImportError:
            raise CatalogueFetchError("catalogue_invalid_payload") from None
