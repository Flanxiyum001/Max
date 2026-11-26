"""Live MCP tool execution behind single-use Trust approvals.

Every executable remote tool maps to exactly one dotted canonical name
``mcp.<connection_id>.<tool_name>`` in a host-built ``ToolPolicyRegistry``.
The untrusted server ``readOnlyHint`` only labels a tool R1 instead of R2;
both tiers always require a fresh single-use owner approval, so the hint
never establishes a permission by itself. Dispatch revalidates arguments
against the reviewed schema snapshot and re-observes the live server for
schema drift before any remote call, over the same pinned, bounded egress
transport used by discovery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import anyio
import httpx
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

from max_core.assistant_chat import ToolExecutionSummary, ToolStatus
from max_core.assistant_chat import ToolExecutor as ChatToolExecutor
from max_core.gateway.contracts import ToolDefinition, ToolProposal
from max_core.mcp_discovery import MCPConnection, ObservedTool
from max_core.mcp_policy import MCPPolicyError, ReviewedToolPolicy, validated_arguments
from max_core.mcp_stdio import StdioMCPConnection, stdio_parameters
from max_core.mcp_transport import EndpointTransport
from max_core.persistence import _is_expired
from max_core.store import StoreError, TaskStore
from max_core.trust import (
    ActorContext,
    RiskTier,
    ToolPolicy,
    ToolPolicyRegistry,
    TrustError,
    TrustService,
)

_OWNER = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_LIVE_CONNECTION = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DOTTED_PREFIX = "mcp."
_MAX_TOOLS = 512
_MAX_PAGES = 16
_CALL_TIMEOUT_SECONDS = 30
_MAX_RESULT_TEXT_CHARS = 16_000
_RESULT_TRUNCATION_MARKER = "\n(mcp_tool_output_truncated)"
_ANCHOR_OBJECTIVE = "MAX Assistant MCP tool session"


class McpExecuteError(Exception):
    """Fixed safe codes only; never embeds upstream payloads or endpoints."""

    def __init__(self, code: str, *, status: Literal["denied", "failed"] = "failed") -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def auto_risk(observation: ObservedTool) -> RiskTier:
    """Derive the trust tier from the untrusted hint; both tiers need approval."""
    if not isinstance(observation, ObservedTool) or type(observation.read_only_hint) is not bool:
        raise McpExecuteError("invalid_mcp_observation")
    return RiskTier.R1 if observation.read_only_hint else RiskTier.R2


def dotted_tool_name(connection_id: str, tool_name: str) -> str:
    return f"{_DOTTED_PREFIX}{connection_id}.{tool_name}"


def _parse_dotted(name: object) -> tuple[str, str] | None:
    if not isinstance(name, str) or not name.startswith(_DOTTED_PREFIX):
        return None
    connection_id, separator, tool_name = name[len(_DOTTED_PREFIX) :].partition(".")
    if (
        not separator
        or _LIVE_CONNECTION.fullmatch(connection_id) is None
        or not isinstance(tool_name, str)
        or len(tool_name) > 128
        or not tool_name
    ):
        return None
    return connection_id, tool_name


@dataclass(frozen=True, slots=True)
class _LiveEntry:
    connection: MCPConnection | StdioMCPConnection
    observation: ObservedTool
    policy: ReviewedToolPolicy
    dotted_name: str


class _PersistentStdioSession:
    """Keep one stdio server alive on its owning event-loop thread.

    This is opt-in for stateful servers such as Playwright. The subprocess is
    created only after an approved call, never when tools are discovered.
    """

    def __init__(self, connection: StdioMCPConnection) -> None:
        self.connection = connection
        self.loop: asyncio.AbstractEventLoop | None = None
        self.session: ClientSession | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, name="max-mcp-stdio", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            self._error = McpExecuteError("mcp_call_failed")
        finally:
            self._ready.set()

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with stdio_client(stdio_parameters(self.connection)) as (reader, writer):
            async with ClientSession(
                reader, writer, read_timeout_seconds=timedelta(seconds=10)
            ) as session:
                await session.initialize()
                self.session = session
                self._ready.set()
                await self._stop.wait()

    def dispatch(self, action: Callable[[ClientSession], Any]) -> str:
        with self._lock:
            if not self._ready.wait(_CALL_TIMEOUT_SECONDS):
                self.close()
                raise McpExecuteError("mcp_call_timeout")
            if self._error is not None or self.loop is None or self.session is None:
                raise McpExecuteError("mcp_call_failed")
            try:
                future = asyncio.run_coroutine_threadsafe(action(self.session), self.loop)
                return str(future.result(timeout=_CALL_TIMEOUT_SECONDS))
            except FutureTimeout:
                future.cancel()
                self.close()
                raise McpExecuteError("mcp_call_timeout") from None
            except McpExecuteError:
                self.close()
                raise
            except Exception:
                self.close()
                raise McpExecuteError("mcp_call_failed") from None

    def close(self) -> None:
        if self.loop is not None and self._stop is not None and self._thread.is_alive():
            self.loop.call_soon_threadsafe(self._stop.set)
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=2)


def _build_live_entries(
    reviewed: Sequence[tuple[ObservedTool, ReviewedToolPolicy]],
    *,
    connections: Sequence[MCPConnection | StdioMCPConnection],
    owner_id: str,
) -> dict[str, _LiveEntry]:
    resolved: dict[str, MCPConnection | StdioMCPConnection] = {}
    for item in connections:
        if (
            not isinstance(item, MCPConnection) and not isinstance(item, StdioMCPConnection)
        ) or _LIVE_CONNECTION.fullmatch(item.connection_id) is None:
            raise McpExecuteError("invalid_mcp_connection")
        resolved[item.connection_id] = item
    entries: dict[str, _LiveEntry] = {}
    for observation, policy in reviewed:
        if not isinstance(observation, ObservedTool) or not isinstance(policy, ReviewedToolPolicy):
            raise McpExecuteError("invalid_mcp_live_policy")
        if not policy.enabled:
            continue
        if (
            policy.owner_id != owner_id
            or policy.risk.value not in (RiskTier.R1.value, RiskTier.R2.value)
            or policy.connection_id != observation.connection_id
            or policy.tool_name != observation.name
        ):
            raise McpExecuteError("invalid_mcp_live_policy")
        connection: MCPConnection | StdioMCPConnection | None = resolved.get(policy.connection_id)
        if connection is None:
            raise McpExecuteError("invalid_mcp_live_policy")
        dotted = dotted_tool_name(observation.connection_id, observation.name)
        if _parse_dotted(dotted) != (observation.connection_id, observation.name):
            raise McpExecuteError("ambiguous_mcp_tool_name")
        if dotted in entries:
            raise McpExecuteError("ambiguous_mcp_tool_name")
        entries[dotted] = _LiveEntry(connection, observation, policy, dotted)
    return entries


def _live_policy(entry: _LiveEntry) -> ToolPolicy:
    def validate(_context: ActorContext, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return validated_arguments(entry.observation, entry.policy, arguments)
        except MCPPolicyError as error:
            code = error.args[0] if error.args else "unreviewed_arguments"
            raise TrustError(str(code), 422) from None

    return ToolPolicy(
        name=entry.dotted_name,
        risk=auto_risk(entry.observation),
        validate_arguments=validate,
    )


def build_mcp_policy_registry(
    reviewed: Sequence[tuple[ObservedTool, ReviewedToolPolicy]],
    *,
    connections: Sequence[MCPConnection | StdioMCPConnection],
    owner_id: str,
) -> ToolPolicyRegistry:
    """Dotted allowlist of enabled, digest-pinned remote tools for TrustService."""
    entries = _build_live_entries(reviewed, connections=connections, owner_id=owner_id)
    return ToolPolicyRegistry(tuple(_live_policy(entry) for entry in entries.values()))


def _bounded_text(raw: str) -> str:
    cleaned = "".join(
        ch if ch in "\t\n" or (ord(ch) > 0x1F and ch != "\x7f") else "\ufffd" for ch in raw
    )
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    if len(cleaned) <= _MAX_RESULT_TEXT_CHARS:
        return cleaned
    return cleaned[:_MAX_RESULT_TEXT_CHARS] + _RESULT_TRUNCATION_MARKER


def _extract_tool_output(result: CallToolResult) -> str:
    if type(result.isError) is not bool or result.isError:
        raise McpExecuteError("mcp_tool_failed", status="failed")
    parts: list[str] = []
    total = 0
    for block in result.content:
        if not isinstance(block, TextContent) or not isinstance(block.text, str):
            continue
        parts.append(block.text)
        total += len(block.text)
        if total > _MAX_RESULT_TEXT_CHARS:
            break
    return _bounded_text("\n".join(parts))


def _flattened_exceptions(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        flattened: list[BaseException] = []
        for sub in error.exceptions:
            flattened.extend(_flattened_exceptions(sub))
        return flattened
    return [error]


def _run_in_fresh_thread(function: Callable[..., str], /, *args: object) -> str:
    """Run one MCP dispatch in a dedicated thread; safe inside any running loop."""
    outcome: list[tuple[Literal["ok", "err"], object]] = []

    def _target() -> None:
        try:
            outcome.append(("ok", function(*args)))
        except BaseException as error:  # noqa: BLE001 - re-raised on caller thread
            outcome.append(("err", error))

    thread = threading.Thread(target=_target, name="max-mcp-dispatch", daemon=True)
    thread.start()
    thread.join()
    if not outcome:
        raise McpExecuteError("mcp_thread_failed")
    kind, value = outcome[0]
    if kind == "ok":
        return str(value)
    assert isinstance(value, BaseException)
    raise value


class MCPToolExecutor:
    """Thread-safe ToolExecutor for owner-reviewed, single-use-approved MCP tools."""

    def __init__(
        self,
        *,
        trust: TrustService | None = None,
        task_store: TaskStore | None = None,
        connections: Sequence[MCPConnection | StdioMCPConnection],
        reviewed: Sequence[tuple[ObservedTool, ReviewedToolPolicy]],
        owner_id: str = "owner",
        actor_id: str = "desktop_owner",
        session_id: str = "mcp-session",
        device_id: str = "local-desktop",
        anchor_task_id: str | None = None,
        approval_ttl_seconds: int = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(owner_id, str) or _OWNER.fullmatch(owner_id) is None:
            raise ValueError("invalid_owner_id")
        for ident in (actor_id, session_id, device_id):
            if not isinstance(ident, str) or _OWNER.fullmatch(ident) is None:
                raise ValueError("invalid_actor_scope")
        if not isinstance(trust, TrustService) and not isinstance(task_store, TaskStore):
            raise ValueError("MCPToolExecutor requires trust or task_store")
        if anchor_task_id is not None and (
            not isinstance(anchor_task_id, str) or _UUID.fullmatch(anchor_task_id) is None
        ):
            raise ValueError("invalid_anchor_task_id")
        if type(approval_ttl_seconds) is not int or not 15 <= approval_ttl_seconds <= 900:
            raise ValueError("invalid_approval_ttl")
        self._entries = _build_live_entries(reviewed, connections=connections, owner_id=owner_id)
        self._registry = ToolPolicyRegistry(
            tuple(_live_policy(entry) for entry in self._entries.values())
        )
        self._transport = transport
        self._persistent_stdio: dict[str, _PersistentStdioSession] = {}
        self._persistent_lock = threading.Lock()
        self._owner_id = owner_id
        self._actor_id = actor_id
        self._session_id = session_id
        self._device_id = device_id
        self._anchor_task_id = anchor_task_id
        self._anchor_lock = threading.Lock()
        self._approval_ttl_seconds = approval_ttl_seconds
        if task_store is not None:
            self._task_store: TaskStore = task_store
        else:
            assert isinstance(trust, TrustService)
            self._task_store = TaskStore(trust.engine)
        self._trust = (
            trust
            if trust is not None
            else TrustService(self._task_store.engine, registry=self._registry)
        )

    @property
    def trust_service(self) -> TrustService:
        return self._trust

    @property
    def policy_registry(self) -> ToolPolicyRegistry:
        return self._registry

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        definitions: list[ToolDefinition] = []
        for dotted, entry in self._entries.items():
            try:
                schema = json.loads(entry.observation.schema_json)
            except (ValueError, TypeError, RecursionError) as error:
                raise McpExecuteError("invalid_mcp_observation") from error
            if not isinstance(schema, dict):
                raise McpExecuteError("invalid_mcp_observation")
            definitions.append(
                ToolDefinition(
                    name=dotted,
                    description=(
                        "Owner-reviewed MCP tool "
                        f"{entry.policy.connection_id}.{entry.policy.tool_name}"
                    ),
                    parameters=schema,
                )
            )
        return tuple(definitions)

    def _ensure_anchor_task_id(self) -> str:
        with self._anchor_lock:
            if self._anchor_task_id is not None:
                return self._anchor_task_id
            key = f"mcp-tool-session:{self._session_id}"
            task, _created = self._task_store.create(self._owner_id, _ANCHOR_OBJECTIVE, key)
            self._anchor_task_id = task.id
            return self._anchor_task_id

    def _summary(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        status: ToolStatus,
        output: dict[str, object] | str,
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> ToolExecutionSummary:
        return ToolExecutionSummary(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            status=status,
            output=output,
            approval_id=approval_id,
            task_id=task_id,
        )

    def execute(
        self,
        proposal: ToolProposal,
        *,
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> ToolExecutionSummary:
        raw_call_id = (
            proposal.call_id
            if isinstance(proposal, ToolProposal) and isinstance(proposal.call_id, str)
            else ""
        )
        raw_name = (
            proposal.name
            if isinstance(proposal, ToolProposal) and isinstance(proposal.name, str)
            else ""
        )
        raw_args: dict[str, object] = (
            dict(proposal.arguments)
            if isinstance(proposal, ToolProposal) and isinstance(proposal.arguments, dict)
            else {}
        )
        try:
            return self._execute(
                proposal,
                raw_call_id=raw_call_id,
                raw_name=raw_name,
                raw_args=raw_args,
                approval_id=approval_id,
                task_id=task_id,
            )
        except McpExecuteError as error:
            return self._summary(
                call_id=raw_call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status=error.status,
                output=error.code,
                approval_id=approval_id,
                task_id=task_id,
            )
        except StoreError:
            return self._summary(
                call_id=raw_call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status="failed",
                output="tool_storage_unavailable",
                approval_id=approval_id,
                task_id=task_id,
            )

    def _execute(
        self,
        proposal: ToolProposal,
        *,
        raw_call_id: str,
        raw_name: str,
        raw_args: dict[str, object],
        approval_id: str | None,
        task_id: str | None,
    ) -> ToolExecutionSummary:
        if not isinstance(proposal, ToolProposal):
            return self._summary(
                call_id=raw_call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status="denied",
                output="invalid_proposal",
                approval_id=approval_id,
                task_id=task_id,
            )
        entry = self._entries.get(proposal.name) if _parse_dotted(proposal.name) else None
        if entry is None:
            return self._summary(
                call_id=raw_call_id,
                tool_name=raw_name,
                arguments=raw_args,
                status="denied",
                output="capability_denied",
                approval_id=approval_id,
                task_id=task_id,
            )
        try:
            normalized = validated_arguments(entry.observation, entry.policy, proposal.arguments)
        except MCPPolicyError as error:
            return self._summary(
                call_id=raw_call_id,
                tool_name=proposal.name,
                arguments=raw_args,
                status="denied",
                output=error.args[0] if error.args else "unreviewed_arguments",
                approval_id=approval_id,
                task_id=task_id,
            )
        canonical_proposal = ToolProposal(
            call_id=proposal.call_id,
            name=entry.dotted_name,
            arguments=normalized,
        )

        if approval_id is not None:
            return self._execute_with_approval(
                canonical_proposal,
                entry=entry,
                approval_id=approval_id,
                raw_call_id=raw_call_id,
                raw_args=raw_args,
            )

        if task_id is not None and _UUID.fullmatch(task_id) is None:
            return self._summary(
                call_id=raw_call_id,
                tool_name=proposal.name,
                arguments=raw_args,
                status="failed",
                output="invalid_task_identifier",
                task_id=task_id,
            )
        try:
            anchor_task_id = task_id if task_id is not None else self._ensure_anchor_task_id()
            context = ActorContext(
                self._owner_id,
                anchor_task_id,
                self._actor_id,
                self._session_id,
                self._device_id,
            )
            grant = self._trust.request(context, canonical_proposal, self._approval_ttl_seconds)
        except TrustError as error:
            return self._summary(
                call_id=raw_call_id,
                tool_name=proposal.name,
                arguments=raw_args,
                status="denied",
                output=error.code,
                task_id=task_id,
            )
        return self._summary(
            call_id=raw_call_id,
            tool_name=proposal.name,
            arguments=raw_args,
            status="awaiting_approval",
            output="owner_approval_required",
            approval_id=grant.id,
            task_id=grant.task_id,
        )

    def _execute_with_approval(
        self,
        canonical_proposal: ToolProposal,
        *,
        entry: _LiveEntry,
        approval_id: str,
        raw_call_id: str,
        raw_args: dict[str, object],
    ) -> ToolExecutionSummary:
        tool_name = canonical_proposal.name
        if _UUID.fullmatch(approval_id) is None:
            return self._summary(
                call_id=raw_call_id,
                tool_name=tool_name,
                arguments=raw_args,
                status="denied",
                output="invalid_approval_identifier",
                approval_id=approval_id,
            )
        try:
            grant = self._trust.get_grant(self._owner_id, approval_id)
        except TrustError as error:
            return self._summary(
                call_id=raw_call_id,
                tool_name=tool_name,
                arguments=raw_args,
                status="denied",
                output=error.code,
                approval_id=approval_id,
            )
        if grant.state == "pending" and not _is_expired(grant.expires_at):
            return self._summary(
                call_id=raw_call_id,
                tool_name=tool_name,
                arguments=raw_args,
                status="awaiting_approval",
                output="owner_approval_required",
                approval_id=grant.id,
                task_id=grant.task_id,
            )
        context = ActorContext(
            self._owner_id,
            grant.task_id,
            self._actor_id,
            self._session_id,
            self._device_id,
        )

        def _dispatch(arguments: dict[str, Any]) -> str:
            try:
                validated_arguments(entry.observation, entry.policy, arguments)
            except MCPPolicyError as error:
                raise McpExecuteError(
                    error.args[0] if error.args else "unreviewed_arguments", status="denied"
                ) from None
            return self._dispatch_remote(entry, arguments)

        try:
            output_text = self._trust.consume_and_execute(
                context, approval_id, canonical_proposal, _dispatch
            )
        except TrustError as error:
            mapped = error.code
            if mapped == "approval_not_active":
                mapped = {
                    "denied": "owner_denied",
                    "consumed": "approval_already_consumed",
                }.get(grant.state, "approval_expired")
            return self._summary(
                call_id=raw_call_id,
                tool_name=tool_name,
                arguments=raw_args,
                status="denied",
                output=mapped,
                approval_id=approval_id,
                task_id=grant.task_id,
            )
        except McpExecuteError as error:
            return self._summary(
                call_id=raw_call_id,
                tool_name=tool_name,
                arguments=raw_args,
                status=error.status,
                output=error.code,
                approval_id=approval_id,
                task_id=grant.task_id,
            )
        return self._summary(
            call_id=raw_call_id,
            tool_name=tool_name,
            arguments=raw_args,
            status="completed",
            output={"result": output_text, "tool": tool_name},
            approval_id=approval_id,
            task_id=grant.task_id,
        )

    def _dispatch_remote(self, entry: _LiveEntry, arguments: dict[str, object]) -> str:
        try:
            if isinstance(entry.connection, StdioMCPConnection) and entry.connection.persistent:
                return self._dispatch_persistent(entry, arguments)
            return _run_in_fresh_thread(
                anyio.run,
                self._dispatch_remote_async,
                entry,
                arguments,
            )
        except Exception as error:
            # SDK task groups can wrap errors, or nest them in exception groups,
            # carrying raw URLs or payloads; unwrap to fixed safe codes only.
            flattened = _flattened_exceptions(error)
            for sub in flattened:
                if isinstance(sub, McpExecuteError):
                    raise sub from None
            if any(isinstance(sub, TimeoutError) for sub in flattened):
                raise McpExecuteError("mcp_call_timeout") from None
            raise McpExecuteError("mcp_call_failed") from None

    async def _dispatch_remote_async(self, entry: _LiveEntry, arguments: dict[str, object]) -> str:
        # A whole-operation deadline also bounds SSE streams and pagination.
        with anyio.fail_after(_CALL_TIMEOUT_SECONDS):
            return await self._dispatch_with_deadline(entry, arguments)

    async def _dispatch_with_deadline(self, entry: _LiveEntry, arguments: dict[str, object]) -> str:
        if isinstance(entry.connection, StdioMCPConnection):
            async with stdio_client(stdio_parameters(entry.connection)) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=10)
                ) as session:
                    return await self._dispatch_over_session(session, entry, arguments)
        if not isinstance(entry.connection, MCPConnection):
            raise McpExecuteError("invalid_mcp_connection", status="denied")
        return await self._dispatch_https(entry.connection, entry, arguments)

    def _dispatch_persistent(self, entry: _LiveEntry, arguments: dict[str, object]) -> str:
        connection = entry.connection
        assert isinstance(connection, StdioMCPConnection)
        with self._persistent_lock:
            bridge = self._persistent_stdio.get(connection.connection_id)
            if bridge is None:
                bridge = _PersistentStdioSession(connection)
                self._persistent_stdio[connection.connection_id] = bridge

        async def action(session: ClientSession) -> str:
            return await self._dispatch_over_session_initialized(session, entry, arguments)

        return bridge.dispatch(action)

    async def _dispatch_over_session_initialized(
        self, session: ClientSession, entry: _LiveEntry, arguments: dict[str, object]
    ) -> str:
        await self._assert_no_schema_drift(session, entry)
        result = await session.call_tool(entry.observation.name, arguments=arguments)
        return _extract_tool_output(result)

    def close(self) -> None:
        """Stop stateful child servers on revoke or desktop shutdown."""
        with self._persistent_lock:
            bridges = list(self._persistent_stdio.values())
            self._persistent_stdio.clear()
        for bridge in bridges:
            bridge.close()

    async def _dispatch_https(
        self, connection: MCPConnection, entry: _LiveEntry, arguments: dict[str, object]
    ) -> str:
        headers = {"Accept-Encoding": "identity"}
        if connection.bearer_token is not None:
            headers["Authorization"] = "Bearer " + connection.bearer_token
        async with httpx.AsyncClient(
            transport=EndpointTransport(
                connection.endpoint,
                self._transport or httpx.AsyncHTTPTransport(retries=0),
            ),
            headers=headers,
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with streamable_http_client(connection.endpoint, http_client=client) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=10)
                ) as session:
                    return await self._dispatch_over_session(session, entry, arguments)

    async def _dispatch_over_session(
        self,
        session: ClientSession,
        entry: _LiveEntry,
        arguments: dict[str, object],
    ) -> str:
        initialized = await session.initialize()
        if initialized.capabilities.tools is None:
            raise McpExecuteError("schema_drifted", status="denied")
        await self._assert_no_schema_drift(session, entry)
        result = await session.call_tool(entry.observation.name, arguments=arguments)
        return _extract_tool_output(result)

    async def _assert_no_schema_drift(self, session: ClientSession, entry: _LiveEntry) -> None:
        """Re-observe the live server; any digest or hint change fails dispatch closed."""
        observed = await self._observe_live(session, entry)
        if (
            observed is None
            or observed.connection_id != entry.observation.connection_id
            or observed.name != entry.observation.name
            or observed.schema_json != entry.observation.schema_json
            or observed.read_only_hint != entry.observation.read_only_hint
            or hashlib.sha256(observed.schema_json.encode()).hexdigest()
            != entry.observation.schema_sha256
        ):
            raise McpExecuteError("schema_drifted", status="denied")

    async def _observe_live(self, session: ClientSession, entry: _LiveEntry) -> ObservedTool | None:
        """Bounded fresh tools/list over the pinned session; exact tool or None."""
        names: set[str] = set()
        cursor: str | None = None
        for _page in range(_MAX_PAGES):
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                if not isinstance(tool.inputSchema, dict) or tool.name in names:
                    raise McpExecuteError("schema_drifted", status="denied")
                names.add(tool.name)
                if tool.name == entry.observation.name:
                    schema = json.dumps(
                        tool.inputSchema, sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                    return ObservedTool(
                        entry.observation.connection_id,
                        tool.name,
                        schema,
                        hashlib.sha256(schema.encode()).hexdigest(),
                        bool(getattr(tool.annotations, "readOnlyHint", False)),
                    )
            cursor = page.nextCursor
            if cursor is None:
                return None
        if len(names) > _MAX_TOOLS:
            raise McpExecuteError("schema_drifted", status="denied")
        return None


class CompositeToolExecutor:
    """Routes local names to the primary executor and mcp.<conn>.<tool> names to MCP."""

    def __init__(self, primary: ChatToolExecutor, mcp: ChatToolExecutor) -> None:
        self._primary = primary
        self._mcp = mcp

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._primary.tool_definitions()) + tuple(self._mcp.tool_definitions())

    def execute(
        self,
        proposal: ToolProposal,
        *,
        approval_id: str | None = None,
    ) -> ToolExecutionSummary:
        if isinstance(proposal, ToolProposal) and _parse_dotted(proposal.name) is not None:
            return self._mcp.execute(proposal, approval_id=approval_id)
        return self._primary.execute(proposal, approval_id=approval_id)
