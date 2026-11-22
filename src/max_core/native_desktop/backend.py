"""In-process, loopback-free developer desktop client for existing MAX Core APIs.

No socket listener, browser token entry, automatic model call or OS tool execution.
The strong bootstrap token never leaves this Python process.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from typing import TYPE_CHECKING, Any, TypeAlias
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

if TYPE_CHECKING:
    import httpx

    from max_core.assistant_chat import (
        AssistantChatService,
        ChatTurnResult,
        ProviderStatus,
        ToolExecutionSummary,
    )
    from max_core.gateway.contracts import ToolDefinition, ToolProposal
    from max_core.local_tools import LocalToolExecutor, LocalToolRegistry
    from max_core.assistant_chat import ToolExecutor
    from max_core.mcp_execute import MCPToolExecutor
    from max_core.persona import ReplyMode
    from max_core.voice.controller import VoiceCapabilities, VoiceController

TaskRecord: TypeAlias = dict[str, Any]
TimelineRecord: TypeAlias = dict[str, Any]
ApprovalRecord: TypeAlias = dict[str, Any]

_UUID_LOWER_RE = re.compile(r"[0-9a-f-]{36}")


class NativeDesktopError(RuntimeError):
    """Fixed safe error code; never include a database URL or secret."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def local_database_url(value: str | None) -> str | None:
    """This early-use desktop cannot silently connect to remote databases."""
    if not value:
        return None
    if not isinstance(value, str) or value != value.strip() or _has_control_chars(value):
        raise NativeDesktopError("invalid_local_database_configuration")
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
        if url.database != url.database.strip() or _has_control_chars(url.database):
            raise NativeDesktopError("invalid_local_database_configuration")
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
    if not isinstance(value, str) or value != value.strip() or _has_control_chars(value):
        raise NativeDesktopError("invalid_local_database_configuration")
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
            or db_name != db_stripped
            or _has_control_chars(db_name)
            or db_stripped.lower() == ":memory:"
            or db_stripped.lower().startswith("file:")
            or bool(url.query)
            or "?" in value
            or not value.startswith("sqlite+pysqlite:///")
        ):
            raise NativeDesktopError("invalid_local_database_configuration")
        return value
    return local_database_url(value)


def _create_local_sqlite_store(url_str: str) -> tuple[Engine, TaskStore]:
    """Initialize a thread-safe local SQLite engine and create missing tables."""
    engine: Engine | None = None
    try:
        if (
            not isinstance(url_str, str)
            or url_str != url_str.strip()
            or _has_control_chars(url_str)
        ):
            raise NativeDesktopError("invalid_local_database_configuration")
        url = make_url(url_str)
        db_name = url.database or ""
        db_stripped = db_name.strip()
        if (
            url.drivername != "sqlite+pysqlite"
            or url.host
            or not db_stripped
            or db_name != db_stripped
            or _has_control_chars(db_name)
            or db_stripped.lower() == ":memory:"
            or db_stripped.lower().startswith("file:")
            or bool(url.query)
            or "?" in url_str
        ):
            raise NativeDesktopError("invalid_local_database_configuration")
        db_path = Path(db_name)
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The durable journal contains task details, Trust decisions and memory.
        # Never follow an existing SQLite-file symlink or open a shared Unix DB.
        if db_path.is_symlink():
            raise NativeDesktopError("unsafe_sqlite_path")
        try:
            descriptor = os.open(
                db_path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            info = db_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise NativeDesktopError("unsafe_sqlite_path") from None
            if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise NativeDesktopError("unsafe_sqlite_permissions") from None
        else:
            os.close(descriptor)
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
    except (OSError, ValueError, SQLAlchemyError):
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


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """Display-safe metadata for a registered local tool."""

    name: str
    description: str
    risk_tier: str
    requires_approval: bool


@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    """Result of an owner Trust decision plus any automatic in-session continuation."""

    approval: ApprovalRecord
    chat_result: ChatTurnResult | None = None
    tool_summary: ToolExecutionSummary | None = None


_MAX_CONTINUATION_CACHE = 64


class NativeDesktopCore:
    """Private TestClient ASGI transport, not an HTTP server or public desktop API."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        store: TaskStore | None = None,
        config: DesktopConfig | None = None,
        chat_transport: httpx.AsyncBaseTransport | None = None,
        chat_service: AssistantChatService | None = None,
        tool_executor: LocalToolExecutor | None = None,
        voice_controller: VoiceController | None = None,
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
        self._lifecycle_cond = threading.Condition()
        self._close_lock = threading.Lock()
        self._bridge_lock = threading.RLock()
        self._continuation_lock = threading.Lock()
        self._thread_local = threading.local()
        self._closed = False
        self._resources_closed = False
        self._active_calls = 0

        self._chat_transport = chat_transport
        self._chat_service: AssistantChatService | None = chat_service
        self._chat_service_injected = chat_service is not None
        self._tool_registry: LocalToolRegistry | None = None
        self._tool_executor: ToolExecutor | None = tool_executor
        self._tool_executor_injected = tool_executor is not None
        from max_core.mcp_desktop import MCPDesktopManager

        self._mcp_manager = MCPDesktopManager()
        self._mcp_executor: MCPToolExecutor | None = None
        self._combined_executor: ToolExecutor | None = None
        self._voice_controller: VoiceController | None = voice_controller
        self._pending_direct_tools: dict[str, tuple[ToolProposal, str | None]] = {}
        self._approval_continuations: dict[str, ChatTurnResult] = {}
        self._direct_continuations: dict[str, ToolExecutionSummary] = {}

        effective_store = store
        settings_db = safe_db
        if store is None and safe_db is not None and safe_db.startswith("sqlite+pysqlite:///"):
            self._owned_engine, effective_store = _create_local_sqlite_store(safe_db)
            self._is_sqlite_local = True
            settings_db = None
        elif (
            store is not None
            and settings_db is not None
            and settings_db.startswith("sqlite+pysqlite:///")
        ):
            settings_db = None

        try:
            self._app = create_app(
                Settings(bearer_token=self._owner_token, database_url=settings_db),
                store=effective_store,
            )
            self._task_store: TaskStore | None = self._app.state.task_store
            self._client = TestClient(
                self._app,
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

    @property
    def has_injected_chat_service(self) -> bool:
        """True if a custom chat_service was explicitly supplied."""
        with self._bridge_lock:
            return self._chat_service_injected

    @classmethod
    def from_env(
        cls,
        *,
        config: DesktopConfig | None = None,
        chat_transport: httpx.AsyncBaseTransport | None = None,
        chat_service: AssistantChatService | None = None,
        tool_executor: LocalToolExecutor | None = None,
        voice_controller: VoiceController | None = None,
    ) -> NativeDesktopCore:
        """Load local desktop configuration (SQLite by default, or PostgreSQL via MAX_DATABASE_URL)."""
        try:
            resolved = config if config is not None else load_desktop_config()
            return cls(
                database_url=resolved.database_url,
                config=resolved,
                chat_transport=chat_transport,
                chat_service=chat_service,
                tool_executor=tool_executor,
                voice_controller=voice_controller,
            )
        except NativeDesktopError:
            raise
        except (OSError, ValueError):
            raise NativeDesktopError("invalid_local_database_configuration") from None

    def _effective_config(self) -> DesktopConfig:
        if self.config is not None:
            return self.config
        cwd = Path.cwd().resolve()
        return DesktopConfig(
            database_url="",
            is_sqlite_local=False,
            workspace_root=cwd,
            notes_dir=(cwd / "notes").resolve(),
            active_provider=None,
            openai=None,
            anthropic=None,
        )

    def _get_tool_registry(self) -> LocalToolRegistry:
        with self._bridge_lock:
            if self._tool_registry is None:
                from max_core.local_tools import LocalToolRegistry

                self._tool_registry = LocalToolRegistry(
                    config=self._effective_config(),
                    core=self,
                    task_store=self._task_store,
                )
            return self._tool_registry

    def _get_tool_executor(self) -> ToolExecutor | None:
        with self._bridge_lock:
            if self._tool_executor is None and not self._tool_executor_injected:
                if self._task_store is None:
                    return None
                from max_core.local_tools import LocalToolExecutor

                self._tool_executor = LocalToolExecutor(
                    registry=self._get_tool_registry(),
                    core=self,
                    task_store=self._task_store,
                    config=self._effective_config(),
                    lifecycle_guard=self.call_guard,
                )
            primary = self._tool_executor
            if primary is None or self._tool_executor_injected:
                return primary
            if self._combined_executor is not None:
                return self._combined_executor
            from max_core.mcp_execute import CompositeToolExecutor, MCPToolExecutor

            # No network on startup or tool registration. Only approved snapshots
            # can be advertised, and every execution still requires Trust approval.
            from max_core.mcp_desktop import MCPSettingsError
            from max_core.mcp_execute import McpExecuteError

            try:
                connections, reviewed = self._mcp_manager.runtime()
                if not reviewed or self._task_store is None:
                    return primary
                self._mcp_executor = MCPToolExecutor(
                    task_store=self._task_store,
                    connections=connections,
                    reviewed=reviewed,
                )
                self._combined_executor = CompositeToolExecutor(primary, self._mcp_executor)
                return self._combined_executor
            except (MCPSettingsError, McpExecuteError, ValueError):
                # Malformed or unauthenticated external configuration never
                # takes the owner's existing local assistant offline.
                return primary

    def _get_chat_service(self) -> AssistantChatService:
        with self._bridge_lock:
            if self._chat_service is None:
                from max_core.assistant_chat import AssistantChatService

                self._chat_service = AssistantChatService(
                    self._effective_config(),
                    transport=self._chat_transport,
                    tools=self._get_tool_registry().tool_definitions(),
                )
            return self._chat_service

    def _get_voice_controller(self) -> VoiceController:
        with self._bridge_lock:
            if self._voice_controller is None:
                from max_core.voice.controller import VoiceController

                self._voice_controller = VoiceController(config=self._effective_config())
            return self._voice_controller

    @property
    def tool_registry(self) -> LocalToolRegistry:
        return self._get_tool_registry()

    @property
    def tool_executor(self) -> ToolExecutor | None:
        return self._get_tool_executor()

    @property
    def chat_service(self) -> AssistantChatService:
        return self._get_chat_service()

    @property
    def voice_controller(self) -> VoiceController:
        return self._get_voice_controller()

    def configure_chat(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        chat_service: AssistantChatService | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        """Configure or replace chat transport, service, or tool executor after core creation."""
        with self._bridge_lock:
            if tool_executor is not None:
                self._tool_executor = tool_executor
                self._tool_executor_injected = True
            if chat_service is not None:
                self._chat_service = chat_service
                self._chat_service_injected = True
            elif transport is not None:
                self._chat_transport = transport
                if (
                    self._chat_service is not None
                    and not self._chat_service_injected
                    and not self._chat_service.pending_approval_ids()
                    and self._chat_service.history_len() == 0
                ):
                    self._chat_service = None

    def configure_voice(self, voice_controller: VoiceController) -> None:
        """Attach a configured VoiceController instance for lifecycle management."""
        with self._bridge_lock:
            self._voice_controller = voice_controller

    def _enter_call(self) -> None:
        depth = int(getattr(self._thread_local, "depth", 0))
        with self._lifecycle_cond:
            if depth == 0 and self._closed:
                raise NativeDesktopError("local_core_unavailable")
            self._active_calls += 1
        self._thread_local.depth = depth + 1

    def _exit_call(self) -> None:
        depth = int(getattr(self._thread_local, "depth", 0))
        self._thread_local.depth = max(0, depth - 1)
        with self._lifecycle_cond:
            self._active_calls -= 1
            if self._active_calls == 0:
                self._lifecycle_cond.notify_all()

    @contextmanager
    def call_guard(self) -> Iterator[None]:
        """Protect Core resources and owned SQLite engine for the duration of a call."""
        self._enter_call()
        try:
            yield
        finally:
            self._exit_call()

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
        self._enter_call()
        try:
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
        finally:
            self._exit_call()

    def create_task(self, objective: str) -> TaskRecord:
        self._enter_call()
        try:
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
        finally:
            self._exit_call()

    def timeline(self, task_id: str) -> tuple[TimelineRecord, ...]:
        self._enter_call()
        try:
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
        finally:
            self._exit_call()

    def activity(self, task_id: str) -> TaskActivity:
        """Read only authoritative task state and the bounded, owner-scoped journal.

        Do not deserialize model/tool payloads, accept pseudo-live claims or infer
        active coding from a generic task objective.
        """
        self._enter_call()
        try:
            events = self.timeline(task_id)
            item = self._call("GET", f"/v1/tasks/{task_id}")
            if not isinstance(item, dict) or not isinstance(item.get("state"), str):
                raise NativeDesktopError("invalid_core_response")
            try:
                return task_activity(item["state"], events)
            except ValueError:
                raise NativeDesktopError("invalid_core_response") from None
        finally:
            self._exit_call()

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

    def provider_status(self) -> ProviderStatus:
        """Return secret-free provider status for the desktop session."""
        with self._bridge_lock:
            chat = self._chat_service
        if chat is not None:
            return chat.provider_status()
        from max_core.assistant_chat import ProviderStatus

        cfg = self._effective_config().active_provider
        if cfg is None:
            return ProviderStatus(configured=False, provider=None, model_id=None)
        configured = isinstance(cfg.api_key, str) and bool(cfg.api_key.strip())
        return ProviderStatus(configured=configured, provider=cfg.provider, model_id=cfg.model_id)

    def mcp_connections(self) -> tuple[dict[str, Any], ...]:
        """Read-only connection and tool review metadata for the desktop."""
        return self._mcp_manager.connections()

    def mcp_add_connection(self, ident: str, endpoint: str, token_env: str = "") -> None:
        """Owner-initiated configuration; never called from a model tool."""
        with self.call_guard():
            self._mcp_manager.add(ident, endpoint, token_env)
            self._invalidate_mcp()

    def mcp_discover(self, ident: str) -> int:
        """Explicit network discovery invalidates all prior pinned approvals."""
        with self.call_guard():
            count = self._mcp_manager.discover(ident)
            self._invalidate_mcp()
            return count

    def mcp_review(self, ident: str, tool_name: str, *, enable: bool) -> None:
        with self.call_guard():
            self._mcp_manager.review(ident, tool_name, enable=enable)
            self._invalidate_mcp()

    def _invalidate_mcp(self) -> None:
        with self._bridge_lock:
            self._combined_executor = None
            self._mcp_executor = None

    def send_chat_message(self, user_text: str) -> ChatTurnResult:
        """Send a user chat message through AssistantChatService with local tools."""
        with self.call_guard():
            executor = self._get_tool_executor()
            return self._get_chat_service().send_message(user_text, tool_executor=executor)

    def resume_chat_approval(self, approval_id: str) -> ChatTurnResult:
        """Resume a paused chat turn for an approval owned by this session."""
        with self.call_guard():
            executor = self._get_tool_executor()
            if executor is None:
                from max_core.assistant_chat import ChatTurnResult

                status = self.provider_status()
                return ChatTurnResult(
                    ok=False,
                    reply="Local task storage is not ready to resume tool approvals.",
                    provider=status.provider,
                    model_id=status.model_id,
                    error_code="task_store_not_ready",
                )
            return self._get_chat_service().resume_approval(approval_id, tool_executor=executor)

    def clear_chat_history(self) -> None:
        """Clear in-memory conversation history and pending chat approvals."""
        with self._bridge_lock:
            if self._chat_service is not None:
                self._chat_service.clear_history()

    def set_reply_mode(self, mode: ReplyMode) -> None:
        """Update the persona reply mode on the chat coordinator."""
        self._get_chat_service().set_reply_mode(mode)

    def list_tools(self) -> tuple[ToolDescriptor, ...]:
        """Return display-safe descriptors for all registered local tools."""
        registry = self._get_tool_registry()
        descriptors: list[ToolDescriptor] = []
        for definition in registry.tool_definitions():
            spec = registry.get_spec(definition.name)
            if spec is None:
                continue
            descriptors.append(
                ToolDescriptor(
                    name=spec.name,
                    description=spec.description,
                    risk_tier=spec.risk_tier.value,
                    requires_approval=spec.requires_approval,
                )
            )
        executor = self._get_tool_executor()
        if executor is not None:
            for definition in executor.tool_definitions():
                if definition.name.startswith("mcp."):
                    descriptors.append(
                        ToolDescriptor(
                            name=definition.name,
                            description=definition.description,
                            risk_tier="r2_write",
                            requires_approval=True,
                        )
                    )
        return tuple(descriptors)

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """Return Gateway ToolDefinition contracts for enabled tools."""
        executor = self._get_tool_executor()
        return tuple(executor.tool_definitions()) if executor is not None else self._get_tool_registry().tool_definitions()

    def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        call_id: str | None = None,
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> ToolExecutionSummary:
        """Invoke an allowlisted local tool directly through LocalToolExecutor."""
        from max_core.assistant_chat import ToolExecutionSummary
        from max_core.gateway.contracts import ToolProposal

        resolved_call_id = (
            call_id if isinstance(call_id, str) and call_id else f"tool-{uuid4().hex[:12]}"
        )
        resolved_args: dict[str, object] = dict(arguments) if isinstance(arguments, dict) else {}
        with self.call_guard():
            executor = self._get_tool_executor()
            if executor is None:
                return ToolExecutionSummary(
                    call_id=resolved_call_id,
                    tool_name=name if isinstance(name, str) else "",
                    arguments=resolved_args,
                    status="failed",
                    output="task_store_not_ready",
                    approval_id=approval_id,
                    task_id=task_id,
                )
            proposal = ToolProposal(
                call_id=resolved_call_id,
                name=name,
                arguments=resolved_args,
            )
            from max_core.local_tools import LocalToolExecutor

            if isinstance(executor, LocalToolExecutor):
                summary = executor.execute(proposal, approval_id=approval_id, task_id=task_id)
            else:
                summary = executor.execute(proposal, approval_id=approval_id)
            if summary.status == "awaiting_approval" and summary.approval_id:
                with self._continuation_lock:
                    if len(self._pending_direct_tools) >= _MAX_CONTINUATION_CACHE:
                        oldest = next(iter(self._pending_direct_tools))
                        self._pending_direct_tools.pop(oldest, None)
                    self._pending_direct_tools[summary.approval_id] = (proposal, summary.task_id)
            return summary

    def voice_capabilities(self) -> VoiceCapabilities:
        """Inspect local voice hardware and STT/TTS readiness."""
        return self._get_voice_controller().capabilities()

    def _store_approval_continuation(self, approval_id: str, result: ChatTurnResult) -> None:
        with self._continuation_lock:
            if len(self._approval_continuations) >= _MAX_CONTINUATION_CACHE:
                oldest = next(iter(self._approval_continuations))
                self._approval_continuations.pop(oldest, None)
            self._approval_continuations[approval_id] = result

    def _store_direct_continuation(self, approval_id: str, summary: ToolExecutionSummary) -> None:
        with self._continuation_lock:
            if len(self._direct_continuations) >= _MAX_CONTINUATION_CACHE:
                oldest = next(iter(self._direct_continuations))
                self._direct_continuations.pop(oldest, None)
            self._direct_continuations[approval_id] = summary

    def pop_approval_continuation(self, approval_id: str) -> ChatTurnResult | None:
        """Retrieve and clear any stored chat continuation result for an approval ID."""
        with self._continuation_lock:
            return self._approval_continuations.pop(approval_id, None)

    def decide_approval(self, approval_id: str, approve: bool) -> ApprovalRecord:
        """Record an explicit owner approval or denial against a pending Trust grant."""
        with self.call_guard():
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

            with self._bridge_lock:
                chat_service = self._chat_service
            if chat_service is not None and approval_id in chat_service.pending_approval_ids():
                executor = self._get_tool_executor()
                if executor is not None:
                    try:
                        chat_result = chat_service.resume_approval(
                            approval_id, tool_executor=executor
                        )
                    except Exception:
                        from max_core.assistant_chat import ChatTurnResult

                        status = self.provider_status()
                        chat_result = ChatTurnResult(
                            ok=False,
                            reply=(
                                "Approval decision was saved, but resuming the assistant "
                                "turn failed."
                            ),
                            provider=status.provider,
                            model_id=status.model_id,
                            error_code="provider_error",
                        )
                    self._store_approval_continuation(approval_id, chat_result)
            else:
                with self._continuation_lock:
                    pending_direct = self._pending_direct_tools.pop(approval_id, None)
                if pending_direct is not None:
                    proposal, pending_task_id = pending_direct
                    executor = self._get_tool_executor()
                    if executor is not None:
                        from max_core.local_tools import LocalToolExecutor

                        if isinstance(executor, LocalToolExecutor):
                            summary = executor.execute(
                                proposal, approval_id=approval_id, task_id=pending_task_id
                            )
                        else:
                            summary = executor.execute(proposal, approval_id=approval_id)
                        self._store_direct_continuation(approval_id, summary)

            return result

    def decide_approval_with_continuation(
        self, approval_id: str, approve: bool
    ) -> ApprovalResolution:
        """Record a Trust decision and return any in-session chat/tool continuation outcome."""
        with self.call_guard():
            approval = self.decide_approval(approval_id, approve)
            with self._continuation_lock:
                chat_result = self._approval_continuations.pop(approval_id, None)
                tool_summary = self._direct_continuations.pop(approval_id, None)
            return ApprovalResolution(
                approval=approval,
                chat_result=chat_result,
                tool_summary=tool_summary,
            )

    def close(self) -> None:
        with self._lifecycle_cond:
            self._closed = True
            while self._active_calls > 0:
                self._lifecycle_cond.wait()
        with self._close_lock:
            with self._task_lock:
                self._pending_task = None
            with self._continuation_lock:
                self._pending_direct_tools.clear()
                self._approval_continuations.clear()
                self._direct_continuations.clear()
            with self._bridge_lock:
                voice = self._voice_controller
            if voice is not None:
                try:
                    voice.revoke_microphone()
                except Exception:
                    pass
            if not self._resources_closed:
                self._resources_closed = True
                try:
                    self._client.close()
                finally:
                    if self._owned_engine is not None:
                        self._owned_engine.dispose()
                        self._owned_engine = None
