"""In-process, loopback-free developer desktop client for existing MAX Core APIs.

No socket listener, browser token entry, automatic model call or OS tool execution.
The strong bootstrap token never leaves this Python process.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from typing import Any, TypeAlias
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from max_core.activity_view import TaskActivity, task_activity
from max_core.agent_harness import ActivityEvent, AgentHarness
from max_core.api import create_app
from max_core.config import Settings
from max_core.gateway import (
    ChatMessage,
    Gateway,
    GatewayRequest,
    HTTPAdapter,
    InferenceDestination,
    Privacy,
)
from max_core.gateway_trial import TrialConfigurationError, TrialSettings
from max_core.local_config import DesktopConfig, load_desktop_config
from max_core.models import Base
from max_core.persona import with_persona
from max_core.store import TaskStore

TaskRecord: TypeAlias = dict[str, Any]
TimelineRecord: TypeAlias = dict[str, Any]
ApprovalRecord: TypeAlias = dict[str, Any]

_UUID_LOWER_RE = re.compile(r"[0-9a-f-]{36}")


class NativeDesktopError(RuntimeError):
    """Fixed safe error code; never include a database URL or secret."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def local_database_url(value: str | None) -> str | None:
    """This early-use desktop cannot silently connect to remote databases."""
    if not value:
        return None
    try:
        url = make_url(value)
        if (
            url.drivername != "postgresql+psycopg"
            or url.host not in {"localhost", "127.0.0.1", "::1"}
            or url.port is None
            or not 1 <= url.port <= 65535
            or not url.database
        ):
            raise NativeDesktopError("local_postgresql_required")
    except NativeDesktopError:
        raise
    except (ValueError, TypeError, AttributeError, ArgumentError):
        raise NativeDesktopError("invalid_local_database_configuration") from None
    return value


def desktop_database_url(source: DesktopConfig | str | None) -> str | None:
    """Validate a desktop database URL, allowing file-backed local SQLite or local PostgreSQL."""
    value = source.database_url if isinstance(source, DesktopConfig) else source
    if not value:
        return None
    try:
        url = make_url(value)
    except (ValueError, TypeError, AttributeError, ArgumentError):
        raise NativeDesktopError("invalid_local_database_configuration") from None
    if url.drivername == "sqlite+pysqlite":
        db_name = url.database or ""
        db_stripped = db_name.strip()
        if (
            url.host
            or not db_stripped
            or db_stripped.lower() == ":memory:"
            or db_stripped.lower().startswith("file:")
            or bool(url.query)
            or not value.startswith("sqlite+pysqlite:///")
        ):
            raise NativeDesktopError("invalid_local_database_configuration")
        return value
    return local_database_url(value)


def _create_local_sqlite_store(url_str: str) -> tuple[Engine, TaskStore]:
    """Initialize a thread-safe local SQLite engine and create missing tables."""
    engine: Engine | None = None
    try:
        url = make_url(url_str)
        db_name = url.database or ""
        db_stripped = db_name.strip()
        if (
            not db_stripped
            or db_stripped.lower() == ":memory:"
            or db_stripped.lower().startswith("file:")
            or bool(url.query)
        ):
            raise NativeDesktopError("invalid_local_database_configuration")
        Path(db_name).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            url_str,
            connect_args={"check_same_thread": False, "timeout": 30.0},
        )

        @event.listens_for(engine, "connect")
        def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

        @event.listens_for(engine, "begin")
        def _begin_immediate_transaction(conn: Connection) -> None:
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        Base.metadata.create_all(engine)
        return engine, TaskStore(engine)
    except NativeDesktopError:
        if engine is not None:
            engine.dispose()
        raise
    except (OSError, SQLAlchemyError):
        if engine is not None:
            engine.dispose()
        raise NativeDesktopError("task_store_not_ready") from None


@dataclass(frozen=True, slots=True)
class DesktopSnapshot:
    """Bounded, display-only values from authenticated local Core contracts."""

    core_status: str
    storage_status: str
    tasks: tuple[TaskRecord, ...]
    approvals: tuple[ApprovalRecord, ...]


@dataclass(frozen=True, slots=True, repr=False)
class LocalModelReply:
    """Owner-visible model text only; no tool execution or private repr."""

    state: str
    text: str = field(repr=False)
    proposed_tools: tuple[str, ...] = ()
    error_code: str | None = None


class NativeDesktopCore:
    """Private TestClient ASGI transport, not an HTTP server or public desktop API."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        store: TaskStore | None = None,
        config: DesktopConfig | None = None,
    ) -> None:
        self.config = config
        effective_url = (
            database_url
            if database_url is not None
            else (config.database_url if config is not None else None)
        )
        safe_db = desktop_database_url(effective_url)
        self._owner_token = token_hex(32)
        self._owned_engine: Engine | None = None
        self._is_sqlite_local = False
        self._pending_task: tuple[str, str] | None = None
        self._task_lock = threading.Lock()

        effective_store = store
        settings_db = safe_db
        if store is None and safe_db is not None and safe_db.startswith("sqlite+pysqlite:///"):
            self._owned_engine, effective_store = _create_local_sqlite_store(safe_db)
            self._is_sqlite_local = True
            settings_db = None

        try:
            self._client = TestClient(
                create_app(
                    Settings(bearer_token=self._owner_token, database_url=settings_db),
                    store=effective_store,
                ),
                base_url="http://localhost",
                raise_server_exceptions=False,
            )
        except Exception:
            if self._owned_engine is not None:
                self._owned_engine.dispose()
                self._owned_engine = None
            raise

    @property
    def engine(self) -> Engine | None:
        """Read-only access to the owned local SQLite engine when active."""
        return self._owned_engine

    @classmethod
    def from_env(cls, *, config: DesktopConfig | None = None) -> NativeDesktopCore:
        """Load local desktop configuration (SQLite by default, or PostgreSQL via MAX_DATABASE_URL)."""
        try:
            resolved = config if config is not None else load_desktop_config()
        except (OSError, ValueError):
            raise NativeDesktopError("invalid_local_database_configuration") from None
        return cls(database_url=resolved.database_url, config=resolved)

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        headers = {"Authorization": "Bearer " + self._owner_token}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(method, path, json=payload, headers=headers)
            if response.status_code == 503:
                raise NativeDesktopError("task_store_not_ready")
            if response.status_code not in {200, 201}:
                raise NativeDesktopError("local_core_request_rejected")
            data = response.json()
            if not isinstance(data, (dict, list)):
                raise NativeDesktopError("invalid_core_response")
            return data
        except NativeDesktopError:
            raise
        except Exception:
            raise NativeDesktopError("local_core_unavailable") from None

    def _verify_local_sqlite_ready(self) -> None:
        if self._owned_engine is None:
            raise NativeDesktopError("task_store_not_ready")
        try:
            with self._owned_engine.connect() as connection:
                connection.execute(text("SELECT id FROM max_tasks LIMIT 1"))
                connection.execute(text("SELECT id FROM max_approval_grants LIMIT 1"))
                connection.execute(text("SELECT id FROM max_trust_audit LIMIT 1"))
                connection.execute(text("SELECT id FROM max_verification_checks LIMIT 1"))
                connection.execute(text("SELECT id FROM max_connect_drafts LIMIT 1"))
                connection.execute(text("SELECT id FROM max_memories LIMIT 1"))
                connection.execute(text("SELECT id FROM max_memory_events LIMIT 1"))
                connection.execute(text("SELECT id FROM max_device_nodes LIMIT 1"))
                connection.execute(text("SELECT id FROM max_device_challenges LIMIT 1"))
        except SQLAlchemyError:
            raise NativeDesktopError("task_store_not_ready") from None

    def snapshot(self) -> DesktopSnapshot:
        if self._is_sqlite_local:
            live = self._call("GET", "/health/live")
            if not isinstance(live, dict) or live.get("status") != "alive":
                raise NativeDesktopError("local_core_unavailable")
            status = self._call("GET", "/v1/status")
            if not isinstance(status, dict) or status.get("assistant") != "MAX":
                raise NativeDesktopError("local_core_unavailable")
            self._verify_local_sqlite_ready()
            storage_label = "Local SQLite ready"
        else:
            ready = self._call("GET", "/health/ready")
            if not isinstance(ready, dict) or ready.get("status") != "ready":
                raise NativeDesktopError("local_core_unavailable")
            status = self._call("GET", "/v1/status")
            if not isinstance(status, dict) or status.get("assistant") != "MAX":
                raise NativeDesktopError("local_core_unavailable")
            if ready.get("scope") != "durable-task-store":
                return DesktopSnapshot(
                    "Core online (in-process)", "Database not configured", (), ()
                )
            storage_label = "Local PostgreSQL ready"

        tasks = self._call("GET", "/v1/tasks")
        approvals = self._call("GET", "/v1/approvals")
        if (
            not isinstance(tasks, list)
            or not isinstance(approvals, list)
            or len(tasks) > 100
            or len(approvals) > 100
            or not all(isinstance(item, dict) for item in tasks + approvals)
        ):
            raise NativeDesktopError("invalid_core_response")
        return DesktopSnapshot(
            "Core online (in-process)",
            storage_label,
            tuple(tasks),
            tuple(approvals),
        )

    def create_task(self, objective: str) -> TaskRecord:
        cleaned = objective.strip()
        if not 1 <= len(cleaned) <= 500:
            raise NativeDesktopError("invalid_task_description")
        with self._task_lock:
            if self._pending_task is None or self._pending_task[0] != cleaned:
                self._pending_task = (cleaned, str(uuid4()))
            idempotency_key = self._pending_task[1]
            result = self._call(
                "POST",
                "/v1/tasks",
                payload={"objective": cleaned},
                idempotency_key=idempotency_key,
            )
            if not isinstance(result, dict) or not isinstance(result.get("id"), str):
                raise NativeDesktopError("invalid_core_response")
            self._pending_task = None
            return result

    def timeline(self, task_id: str) -> tuple[TimelineRecord, ...]:
        try:
            if (
                not isinstance(task_id, str)
                or not _UUID_LOWER_RE.fullmatch(task_id)
                or str(UUID(task_id)) != task_id
            ):
                raise ValueError("bad id")
        except (TypeError, ValueError):
            raise NativeDesktopError("invalid_task_identifier") from None
        page = self._call("GET", f"/v1/tasks/{task_id}/events?limit=20")
        if not isinstance(page, dict) or not isinstance(page.get("events"), list):
            raise NativeDesktopError("invalid_core_response")
        events = page["events"]
        if len(events) > 20 or not all(isinstance(item, dict) for item in events):
            raise NativeDesktopError("invalid_core_response")
        return tuple(events)

    def activity(self, task_id: str) -> TaskActivity:
        """Read only authoritative task state and the bounded, owner-scoped journal.

        Do not deserialize model/tool payloads, accept pseudo-live claims or infer
        active coding from a generic task objective.
        """
        events = self.timeline(task_id)
        item = self._call("GET", f"/v1/tasks/{task_id}")
        if not isinstance(item, dict) or not isinstance(item.get("state"), str):
            raise NativeDesktopError("invalid_core_response")
        try:
            return task_activity(item["state"], events)
        except ValueError:
            raise NativeDesktopError("invalid_core_response") from None

    def ask_local(
        self,
        prompt: str,
        *,
        on_activity: Callable[[ActivityEvent], None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> LocalModelReply:
        """One manual, non-persistent, explicitly verified-local model turn.

        Supplying an HTTP client is solely a transport seam for mock-only tests;
        no call is made unless the owner explicitly initiates this method.
        """
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise NativeDesktopError("invalid_model_prompt")
        try:
            settings = TrialSettings.from_env()
        except TrialConfigurationError:
            raise NativeDesktopError("local_model_not_configured") from None
        if settings.spec.inference_destination != InferenceDestination.VERIFIED_LOCAL:
            raise NativeDesktopError("local_model_only")
        try:
            request = GatewayRequest(
                request_id=uuid4().hex,
                messages=(ChatMessage("user", prompt.strip()),),
                privacy=Privacy.CONFIDENTIAL,
                max_input_tokens=min(4096, settings.spec.context_tokens - 512),
                max_output_tokens=512,
                cloud_allowed=False,
                local_only=True,
                allow_fallback=False,
            )
            if settings.persona_enabled:
                request = with_persona(request)
        except ValueError:
            raise NativeDesktopError("invalid_model_prompt") from None

        async def one_turn() -> LocalModelReply:
            async def invoke(transport: httpx.AsyncClient) -> LocalModelReply:
                harness = AgentHarness(
                    Gateway((settings.spec,), HTTPAdapter(transport)),
                    deadline_seconds=45,
                )
                result = await harness.run(request, on_activity=on_activity)
                if result.response is None:
                    return LocalModelReply("failed", "", (), result.error_code)
                return LocalModelReply(
                    result.state,
                    result.response.content,
                    tuple(proposal.name for proposal in result.response.tool_proposals),
                )

            if client is not None:
                return await invoke(client)
            async with httpx.AsyncClient(trust_env=False) as transport:
                return await invoke(transport)

        try:
            return asyncio.run(one_turn())
        except Exception:
            raise NativeDesktopError("local_model_unavailable") from None

    def decide_approval(self, approval_id: str, approve: bool) -> ApprovalRecord:
        """Record an explicit owner approval or denial against a pending Trust grant."""
        try:
            if (
                not isinstance(approval_id, str)
                or not _UUID_LOWER_RE.fullmatch(approval_id)
                or str(UUID(approval_id)) != approval_id
            ):
                raise ValueError("bad id")
        except (TypeError, ValueError):
            raise NativeDesktopError("invalid_approval_identifier") from None
        if type(approve) is not bool:
            raise NativeDesktopError("invalid_approval_decision")
        result = self._call(
            "POST",
            f"/v1/approvals/{approval_id}/decision",
            payload={"approve": approve},
        )
        if (
            not isinstance(result, dict)
            or result.get("id") != approval_id
            or result.get("state") not in {"approved", "denied"}
        ):
            raise NativeDesktopError("invalid_core_response")
        return result

    def close(self) -> None:
        with self._task_lock:
            self._pending_task = None
        self._client.close()
        if self._owned_engine is not None:
            self._owned_engine.dispose()
            self._owned_engine = None
