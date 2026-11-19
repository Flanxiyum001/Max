"""Packaged static desktop assets; no template interpolation or credentials."""

from __future__ import annotations

from importlib.resources import files
from typing import Literal

from starlette.responses import Response

AssetName = Literal["index.html", "app.css", "app.js"]

_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'"
)


def desktop_asset(name: AssetName) -> Response:
    """Serve only allowlisted packaged content under strict browser containment."""
    if name not in _CONTENT_TYPES:
        raise ValueError("unsupported_desktop_asset")
    raw = files("max_core.desktop").joinpath(name).read_bytes()
    return Response(
        content=raw,
        media_type=_CONTENT_TYPES[name],
        headers={
            "Content-Security-Policy": _CSP,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )
