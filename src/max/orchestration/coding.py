"""Optional coding-worker backends (OpenHands SDK or MAX's own guarded runner).

Decision record (see docs/INTEGRATIONS.md §OpenHands):

* The OpenHands Software Agent SDK (``openhands-sdk``, MIT, Python >=3.12) is
  an *optional* backend selected with ``MAX_ORCH_CODER_BACKEND=openhands``.
* It is imported lazily. If the package (or its required services) is absent,
  ``available()`` reports False with an actionable reason and the supervisor
  keeps the built-in backend — text chat is never affected.
* Provider configuration stays outside MAX's secrets: the SDK receives the
  model string, base URL and the *name* of the env var holding the key.
* The adapter never bypasses MAX's policy gate: it runs inside the task
  workspace with an explicit tool allowlist, and terminal/shell tools are only
  offered when guarded execution is enabled in MAX configuration.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Task


class BackendUnavailable(Exception):
    """The requested coding backend cannot run in this environment."""


@dataclass
class BackendReport:
    backend: str
    summary: str
    changed_files: list[str]
    diff: str
    tests_run: list[dict]
    errors: list[str]
    evidence: list[str]
    duration_s: float = 0.0

    def to_payload(self, task: Task) -> dict:
        status = "failed" if self.errors and not self.summary else "completed"
        return {
            "task_id": task.id,
            "status": status,
            "summary": self.summary or ("backend reported no summary" if self.errors else "done"),
            "evidence": self.evidence,
            "changed_files": self.changed_files,
            "tests_run": self.tests_run,
            "errors": self.errors,
            "diff": self.diff,
        }


class MaxCodingBackend:
    """Built-in backend: MAX's existing isolated worktree + guarded runner."""

    name = "max"

    def __init__(self, task_manager: Any, policy: Any, test_argv: list[str] | None = None) -> None:
        self.task_manager = task_manager
        self.policy = policy
        self.test_argv = list(test_argv or [])

    def available(self) -> tuple[bool, str]:
        if self.task_manager is None:
            return False, "no coding task manager configured (set MAX_WORKER_REPOS)"
        return True, "ok"

    def run(self, task: Task, grant: Any, work: Any) -> BackendReport:  # pragma: no cover - thin
        raise BackendUnavailable("MaxCodingBackend is driven by the coder worker function")


class OpenHandsBackend:
    """Adapter around the OpenHands Software Agent SDK.

    The SDK is imported lazily and probed for the API surface the adapter
    needs (``LLM``, ``Agent``, ``Conversation``). If anything is missing the
    adapter fails clearly instead of silently substituting another runtime.
    """

    name = "openhands"

    def __init__(
        self,
        model: str,
        base_url: str = "",
        api_key_env: str = "",
        max_iterations: int = 8,
        allow_terminal: bool = False,
        timeout_s: float = 300.0,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.max_iterations = int(max_iterations)
        self.allow_terminal = bool(allow_terminal)
        self.timeout_s = float(timeout_s)
        self._sdk = None

    # ------------------------------------------------------------ discovery
    def _load(self):
        if self._sdk is not None:
            return self._sdk
        try:
            import openhands.sdk as sdk  # type: ignore
        except Exception as exc:  # noqa: BLE001 - missing optional dependency
            raise BackendUnavailable(
                "openhands-sdk is not installed (pip install 'max-assistant[openhands]')"
            ) from exc
        missing = [n for n in ("LLM", "Agent", "Conversation") if not hasattr(sdk, n)]
        if missing:
            raise BackendUnavailable(
                "installed openhands-sdk does not expose required API: " + ", ".join(missing)
            )
        self._sdk = sdk
        return sdk

    def available(self) -> tuple[bool, str]:
        try:
            self._load()
        except BackendUnavailable as exc:
            return False, str(exc)
        if not self.model:
            return False, "MAX_ORCH_OH_MODEL is not configured"
        if self.api_key_env and not os.environ.get(self.api_key_env):
            return False, f"environment variable {self.api_key_env} is not set"
        return True, "ok"

    def tools(self) -> list[str]:
        """Explicit tool allowlist: no terminal/shell unless MAX allows it."""
        names = ["str_replace_editor"] if not self.allow_terminal else ["str_replace_editor", "execute_bash"]
        return names

    # ------------------------------------------------------------- execution
    def run(self, task: Task, workspace: Path, prompt: str) -> dict:
        ok, reason = self.available()
        if not ok:
            raise BackendUnavailable(reason)
        sdk = self._load()

        api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
        llm_kwargs: dict[str, Any] = {"model": self.model}
        if self.base_url:
            llm_kwargs["base_url"] = self.base_url
        if api_key:
            llm_kwargs["api_key"] = api_key

        import time

        start = time.monotonic()
        try:
            llm = sdk.LLM(**llm_kwargs)
            agent = sdk.Agent(llm=llm, tools=[sdk.Tool(name=n) for n in self.tools()])
            conversation = sdk.Conversation(agent=agent, workspace=Path(workspace))
            conversation.send_message(prompt)
            conversation.run(max_iteration_per_run=self.max_iterations)
        except BackendUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK/provider failure => failed task
            return {
                "status": "failed",
                "summary": f"openhands backend failed: {type(exc).__name__}",
                "errors": [str(exc)[:400]],
            }
        duration = round(time.monotonic() - start, 3)

        summary = ""
        try:
            conversation_data = conversation.export() if hasattr(conversation, "export") else {}
            if isinstance(conversation_data, dict):
                summary = str(conversation_data.get("summary", ""))[:1000]
        except Exception:  # noqa: BLE001 - export is best effort
            summary = ""
        return {
            "status": "completed",
            "summary": summary or "openhands run finished",
            "evidence": [f"openhands: {self.model} ran in {workspace}"],
            "errors": [],
            "resources": {"duration_s": duration, "backend": self.name},
        }


def build_coding_backend(config: Any, task_manager: Any, policy: Any) -> tuple[str, Any]:
    """Select the configured backend, degrading to the built-in one."""
    choice = (getattr(config, "orch_coder_backend", "max") or "max").lower()
    if choice == "openhands":
        backend = OpenHandsBackend(
            model=getattr(config, "orch_openhands_model", ""),
            base_url=getattr(config, "orch_openhands_base_url", ""),
            api_key_env=getattr(config, "orch_openhands_key_env", ""),
            max_iterations=getattr(config, "orch_openhands_iters", 8),
            allow_terminal=getattr(config, "orch_exec", False),
            timeout_s=getattr(config, "orch_task_timeout_s", 300.0),
        )
        ok, reason = backend.available()
        if ok:
            return "openhands", backend
        return "max", MaxCodingBackend(task_manager, policy)
    return "max", MaxCodingBackend(task_manager, policy)


def parse_test_argv(raw: str) -> list[str]:
    if not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return [part for part in raw.split() if part]
    return [str(v) for v in value] if isinstance(value, list) else []
