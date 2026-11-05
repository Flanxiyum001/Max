"""Dependency-injected MAX Core API factory; no remote actions or provider calls."""

from __future__ import annotations

from secrets import token_hex

from fastapi import Depends, FastAPI, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, Response

from max_core.config import Settings
from max_core.observability import configure_access_logger
from max_core.security import BootstrapBearerAuth


def create_app(settings: Settings | None = None) -> FastAPI:
    """Fail configuration validation before constructing a service."""
    config = settings if settings is not None else Settings.from_env()
    logger = configure_access_logger(config.log_level)
    app = FastAPI(
        title="MAX Core",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        debug=False,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
    )
    auth = BootstrapBearerAuth(config.bearer_token)

    @app.middleware("http")
    async def safe_access_log(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = token_hex(16)
        try:
            response = await call_next(request)
        except Exception:
            logger.error(
                "internal failure",
                extra={
                    "event": "http_failure",
                    "request_id": request_id,
                    "method": request.method,
                    "route": "<error>",
                    "status_code": 500,
                },
            )
            response = JSONResponse({"detail": "Internal server error"}, status_code=500)
        response.headers["X-Request-ID"] = request_id
        route = request.scope.get("route")
        route_path = getattr(route, "path", "<unmatched>")
        logger.info(
            "request finished",
            extra={
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "route": route_path,
                "status_code": response.status_code,
            },
        )
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        # Readiness covers only this process/config, not future DB/provider dependencies.
        return {"status": "ready", "scope": "bootstrap-only"}

    @app.get("/v1/session", dependencies=[Depends(auth)])
    async def session() -> dict[str, str]:
        return {"principal": "owner", "auth_mode": "local-bootstrap-token"}

    @app.get("/v1/status", dependencies=[Depends(auth)])
    async def status() -> dict[str, str]:
        return {"principal": "owner", "assistant": "MAX", "phase": "secure-core-bootstrap"}

    return app
