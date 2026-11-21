"""In-process, loopback-free developer desktop client for existing MAX Core APIs.

No socket listener, browser token entry, automatic model call or OS tool execution.
The strong bootstrap token never leaves this Python process.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

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
from max_core.persona import with_persona
from max_core.store import TaskStore


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


@dataclass(frozen=True, slots=True)
class DesktopSnapshot:
    """Bounded, display-only values from authenticated local Core contracts."""

    core_status: str
    storage_status: str
    tasks: tuple[dict[str, Any], ...]
    approvals: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True, repr=False)
class LocalModelReply:
    """Owner-visible model text only; no tool execution or private repr."""

    state: str
    text: str = field(repr=False)
    proposed_tools: tuple[str, ...] = ()
    error_code: str | None = None


class NativeDesktopCore:
    """Private TestClient ASGI transport, not an HTTP server or public desktop API."""

    def __init__(self, *, database_url: str | None = None, store: TaskStore | None = None) -> None:
        safe_db = local_database_url(database_url)
        self._owner_token = token_hex(32)
        self._client = TestClient(
            create_app(Settings(bearer_token=self._owner_token, database_url=safe_db), store=store),
            base_url="http://localhost",
            raise_server_exceptions=False,
        )
        self._pending_task: tuple[str, str] | None = None

    @classmethod
    def from_env(cls) -> NativeDesktopCore:
        """Read only the optional local dev database URL, not public API tokens."""
        return cls(database_url=os.environ.get("MAX_DATABASE_URL"))

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, str] | None = None,
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

    def snapshot(self) -> DesktopSnapshot:
        ready = self._call("GET", "/health/ready")
        if not isinstance(ready, dict) or ready.get("status") != "ready":
            raise NativeDesktopError("local_core_unavailable")
        status = self._call("GET", "/v1/status")
        if not isinstance(status, dict) or status.get("assistant") != "MAX":
            raise NativeDesktopError("local_core_unavailable")
        storage_ready = ready.get("scope") == "durable-task-store"
        if not storage_ready:
            return DesktopSnapshot("Core online (in-process)", "Database not configured", (), ())
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
            "Local PostgreSQL ready",
            tuple(tasks),
            tuple(approvals),
        )

    def create_task(self, objective: str) -> dict[str, Any]:
        cleaned = objective.strip()
        if not 1 <= len(cleaned) <= 500:
            raise NativeDesktopError("invalid_task_description")
        if self._pending_task is None or self._pending_task[0] != cleaned:
            self._pending_task = (cleaned, str(uuid4()))
        result = self._call(
            "POST",
            "/v1/tasks",
            payload={"objective": cleaned},
            idempotency_key=self._pending_task[1],
        )
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            raise NativeDesktopError("invalid_core_response")
        self._pending_task = None
        return result

    def timeline(self, task_id: str) -> tuple[dict[str, Any], ...]:
        try:
            if not re.fullmatch(r"[0-9a-f-]{36}", task_id) or str(UUID(task_id)) != task_id:
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

    def close(self) -> None:
        self._pending_task = None
        self._client.close()
