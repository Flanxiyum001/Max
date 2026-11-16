"""Dependency-injected MAX Core API factory; no remote actions or provider calls."""

from __future__ import annotations

import asyncio
from secrets import token_hex
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, Response

from max_core.config import Settings
from max_core.connect import ConnectService, DraftView
from max_core.event_stream import EventStreamError, LocalEventBus, OneUseTickets, loopback_origins
from max_core.observability import configure_access_logger
from max_core.security import BootstrapBearerAuth
from max_core.store import StoreError, TaskStore, TaskView, create_store
from max_core.timeline import TaskTimeline, TimelinePage
from max_core.trust import GrantView, TrustError, TrustService


class OwnerApprovalDecision(BaseModel):
    """Owner-only; the temporary bearer token is NOT a production step-up factor."""

    model_config = ConfigDict(extra="forbid")
    approve: StrictBool


class NewConnectEvent(BaseModel):
    """Only fixed metadata; no arbitrary instructions, URLs, tokens or webhook bodies."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: Literal["github_ci", "calendar", "local_build"]
    kind: Literal["ci.failed", "calendar.upcoming", "build.finished"]
    event_key: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    resource_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:/#-]+$")


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


def require_connect(request: Request) -> ConnectService:
    service: ConnectService | None = request.app.state.connect_service
    if service is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Connect persistence is not configured",
        )
    return service


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
    event_bus = LocalEventBus()
    event_tickets = OneUseTickets()
    allowed_event_origins = loopback_origins(config.bind_port)
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
    app.state.connect_service = (
        ConnectService(task_store.engine) if task_store is not None else None
    )

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
        else:
            event_bus.publish("task.created", task.id, task.state, task.revision)
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
            task = storage.cancel("owner", str(task_id))
        except StoreError as exc:
            raise _error(exc) from None
        event_bus.publish("task.cancel_requested", task.id, task.state, task.revision)
        return task

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

    @app.post("/v1/connect/events", status_code=201, dependencies=[Depends(auth)])
    def ingest_connect_event(
        payload: NewConnectEvent,
        response: Response,
        connect_service: Annotated[ConnectService, Depends(require_connect)],
    ) -> DraftView:
        try:
            draft, created = connect_service.ingest(
                "owner",
                payload.source,
                payload.kind,
                payload.event_key,
                payload.resource_key,
            )
        except StoreError as exc:
            raise _error(exc) from None
        if not created:
            response.status_code = 200
        return draft

    @app.get("/v1/connect/drafts", dependencies=[Depends(auth)])
    def pending_connect_drafts(
        connect_service: Annotated[ConnectService, Depends(require_connect)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> list[DraftView]:
        return connect_service.list_pending("owner", limit)

    @app.post("/v1/connect/drafts/{draft_id}/dismiss", dependencies=[Depends(auth)])
    def dismiss_connect_draft(
        draft_id: UUID,
        connect_service: Annotated[ConnectService, Depends(require_connect)],
    ) -> DraftView:
        try:
            return connect_service.dismiss("owner", str(draft_id))
        except StoreError as exc:
            raise _error(exc) from None


    @app.post("/v1/events/ticket", dependencies=[Depends(auth)])
    def issue_event_ticket(request: Request) -> dict[str, str | int]:
        """Local bearer-authenticated one-use ticket; never expose in a URL."""
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_event_origins:
            raise HTTPException(status_code=403, detail="Origin denied")
        # Non-browser local clients must explicitly use the localhost origin.
        chosen = origin or f"http://localhost:{config.bind_port}"
        try:
            token = event_tickets.issue(chosen)
        except EventStreamError:
            raise HTTPException(status_code=503, detail="Event tickets unavailable") from None
        return {"ticket": token, "expires_in_seconds": event_tickets.ttl}

    @app.websocket("/v1/events/ws")
    async def event_socket(socket: WebSocket) -> None:
        """Local metadata stream only; no commands, grants, or remote device auth."""
        headers = socket.scope["headers"]
        origins = [v for k, v in headers if k.lower() == b"origin"]
        protocols = [v for k, v in headers if k.lower() == b"sec-websocket-protocol"]
        if len(origins) != 1 or len(protocols) != 1:
            await socket.close(code=1008)
            return
        try:
            origin_value = origins[0].decode("ascii")
            offered = [part.strip() for part in protocols[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            await socket.close(code=1008)
            return
        if (
            origin_value not in allowed_event_origins
            or len(offered) != 2
            or offered[0] != "max-events-v1"
            or not offered[1].startswith("max-ticket.")
            or not event_tickets.consume(offered[1][len("max-ticket.") :], origin_value)
        ):
            await socket.close(code=1008)
            return
        try:
            identifier, queue, sequence = event_bus.subscribe()
        except EventStreamError:
            await socket.close(code=1013)
            return
        try:
            await socket.accept(subprotocol="max-events-v1")
            await socket.send_json(
                {"type": "hello", "last_sequence": sequence, "recovery": "refetch"}
            )
            while True:
                outbound = asyncio.create_task(queue.get())
                inbound = asyncio.create_task(socket.receive())
                done, pending = await asyncio.wait(
                    {outbound, inbound}, timeout=15.0, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if inbound in done:
                    message = inbound.result()
                    if message["type"] != "websocket.disconnect":
                        await socket.close(code=1008)
                    return
                if outbound in done:
                    event = outbound.result()
                    if event is None:
                        await socket.close(code=1013)
                        return
                    await asyncio.wait_for(socket.send_json(event.payload()), timeout=5.0)
                else:
                    await asyncio.wait_for(
                        socket.send_json({"type": "keepalive"}), timeout=5.0
                    )
        except (RuntimeError, TimeoutError):
            return
        finally:
            event_bus.unsubscribe(identifier)

    return app
