"""Dependency-injected MAX Core API factory; no remote actions or provider calls."""

from __future__ import annotations

from secrets import token_hex
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, Response

from max_core.config import Settings
from max_core.observability import configure_access_logger
from max_core.security import BootstrapBearerAuth
from max_core.store import StoreError, TaskStore, TaskView, create_store
from max_core.timeline import TaskTimeline, TimelinePage
from max_core.trust import GrantView, TrustError, TrustService


class OwnerApprovalDecision(BaseModel):
    """Owner-only; the temporary bearer token is NOT a production step-up factor."""

    model_config = ConfigDict(extra="forbid")
    approve: StrictBool


class NewTask(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    objective: str = Field(min_length=1, max_length=500)


def _error(error: StoreError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.reason)


def require_store(request: Request) -> TaskStore:
    """Module-level dependency avoids local closure annotation resolution issues."""
    storage: TaskStore | None = request.app.state.task_store
    if storage is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task persistence is not configured",
        )
    return storage


def require_trust(request: Request) -> TrustService:
    service: TrustService | None = request.app.state.trust_service
    if service is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trust storage is not configured",
        )
    return service


def require_timeline(request: Request) -> TaskTimeline:
    timeline: TaskTimeline | None = request.app.state.task_timeline
    if timeline is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task persistence is not configured",
        )
    return timeline


def create_app(settings: Settings | None = None, store: TaskStore | None = None) -> FastAPI:
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
    task_store = (
        store
        if store is not None
        else (create_store(config.database_url) if config.database_url is not None else None)
    )
    app.state.task_store = task_store
    app.state.task_timeline = (
        TaskTimeline(task_store, config.bearer_token) if task_store is not None else None
    )
    app.state.trust_service = TrustService(task_store.engine) if task_store is not None else None

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
    def ready() -> dict[str, str]:
        if task_store is None:
            return {"status": "ready", "scope": "bootstrap-only"}
        if not task_store.ready():
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Task storage is not ready or migrations are incomplete",
            )
        return {"status": "ready", "scope": "durable-task-store"}

    @app.get("/v1/session", dependencies=[Depends(auth)])
    async def session() -> dict[str, str]:
        return {"principal": "owner", "auth_mode": "local-bootstrap-token"}

    @app.get("/v1/status", dependencies=[Depends(auth)])
    async def status() -> dict[str, str]:
        return {"principal": "owner", "assistant": "MAX", "phase": "secure-core-bootstrap"}

    @app.post("/v1/tasks", status_code=201, dependencies=[Depends(auth)])
    def create_task(
        payload: NewTask,
        idempotency_key: Annotated[str, Header(min_length=8, max_length=100)],
        response: Response,
        storage: Annotated[TaskStore, Depends(require_store)],
    ) -> TaskView:
        try:
            task, created = storage.create("owner", payload.objective, idempotency_key)
        except StoreError as exc:
            raise _error(exc) from None
        if not created:
            response.status_code = 200
        return task

    @app.get("/v1/tasks", dependencies=[Depends(auth)])
    def list_tasks(storage: Annotated[TaskStore, Depends(require_store)]) -> list[TaskView]:
        return storage.list_recent("owner")

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(auth)])
    def get_task(task_id: UUID, storage: Annotated[TaskStore, Depends(require_store)]) -> TaskView:
        try:
            return storage.get("owner", str(task_id))
        except StoreError as exc:
            raise _error(exc) from None

    @app.get("/v1/tasks/{task_id}/events", dependencies=[Depends(auth)])
    def task_events(
        task_id: UUID,
        timeline: Annotated[TaskTimeline, Depends(require_timeline)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(min_length=3, max_length=512)] = None,
    ) -> TimelinePage:
        """Read only owner-scoped historical metadata; does not mark work complete."""
        try:
            return timeline.page("owner", str(task_id), limit=limit, cursor=cursor)
        except StoreError as exc:
            raise _error(exc) from None

    @app.post("/v1/tasks/{task_id}/cancel", dependencies=[Depends(auth)])
    def cancel_task(
        task_id: UUID, storage: Annotated[TaskStore, Depends(require_store)]
    ) -> TaskView:
        try:
            return storage.cancel("owner", str(task_id))
        except StoreError as exc:
            raise _error(exc) from None

    @app.get("/v1/approvals", dependencies=[Depends(auth)])
    def pending_approvals(
        trust: Annotated[TrustService, Depends(require_trust)],
    ) -> list[GrantView]:
        return trust.list_pending("owner")

    @app.post("/v1/approvals/{approval_id}/decision", dependencies=[Depends(auth)])
    def decide_approval(
        approval_id: UUID,
        decision: OwnerApprovalDecision,
        trust: Annotated[TrustService, Depends(require_trust)],
    ) -> GrantView:
        try:
            return trust.decide("owner", str(approval_id), decision.approve)
        except TrustError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from None

    return app
