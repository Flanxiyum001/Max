"""MAX-side adapter for the desktop control MCP server.

Mirrors the browser layer: MAX registers ordinary native tools
(``desktop_exec`` / ``desktop_screen`` / ``desktop_input``), and this provider
forwards them through :class:`~max.mcp.backend.MCPServerManager`, which owns the
child process, the environment allowlist and the audit trail.

The permission story is deliberately two-layer:

* MAX's policy gate decides whether the *tool* may be called at all. The three
  desktop tools are category ``exec`` (denied by default); enabling
  ``MAX_DESKTOP_ENABLE=1`` puts them on the policy allowlist, and an explicit
  ``MAX_TOOLS_DENY`` entry still wins because deny is checked first.
* the server decides whether *this command* may run — read-only immediately,
  state-changing only with a one-shot console approval, never for a fixed
  deny list.

Neither layer can be rewritten by the model: the first is configuration read
at startup, the second lives in a separate process whose code the model cannot
reach.
"""

from __future__ import annotations

import sys

from ..core.tools import ToolError
from ..mcp.backend import MCPServerConfig

DESKTOP_SERVER_ID = "desktop"
DESKTOP_TOOLS = ("desktop_exec", "desktop_screen", "desktop_input")
MCP_TOOL_NAMES = (
    "execute_terminal_command",
    "control_mouse_keyboard",
    "read_active_screen",
)


def desktop_server_config(config) -> MCPServerConfig:
    """Build the built-in desktop server entry from MAX configuration.

    Everything is passed on the command line so the server needs no
    environment beyond the ``PATH``/``LANG`` the MCP manager already allows —
    credentials physically cannot reach it.
    """
    state = (config.data_dir / "desktop").resolve()
    return MCPServerConfig(
        id=DESKTOP_SERVER_ID,
        command=[
            sys.executable,
            "-m",
            "max.desktop.server",
            "--root",
            str(config.workspace_dir.resolve()),
            "--state",
            str(state),
            "--ttl",
            str(int(config.desktop_approval_ttl_s)),
            "--gui-ttl",
            str(int(config.desktop_gui_ttl_s)),
            "--gui-actions",
            str(int(config.desktop_gui_actions)),
            "--max-timeout",
            str(int(config.desktop_timeout_s)),
        ],
        enabled=True,
        env_allow=(),
        timeout_s=max(15.0, float(config.desktop_timeout_s) + 20.0),
        tool_allow=MCP_TOOL_NAMES,
        tool_deny=(),
        approval_required=(),
        cwd=str(config.repo_root),
    )


class DesktopProvider:
    def __init__(self, manager, enabled: bool = False, audit=None):
        self.manager = manager
        self.enabled = enabled
        self.audit = audit
        self.server_id = DESKTOP_SERVER_ID

    def _require(self) -> None:
        if not self.enabled:
            raise ToolError(
                "desktop control is disabled — set MAX_DESKTOP_ENABLE=1 (and keep "
                "MAX_TOOLS_DENY clear of desktop_*) to enable local machine control"
            )
        if self.server_id not in self.manager.configs:
            raise ToolError(
                "no desktop MCP server is configured — it is registered automatically "
                "when MAX_DESKTOP_ENABLE=1"
            )

    # -------------------------------------------------------------- terminal
    def exec_command(
        self,
        command,
        cwd=None,
        timeout_s=None,
        approval_id=None,
    ) -> str:
        self._require()
        if isinstance(command, str) or not isinstance(command, (list, tuple)):
            raise ToolError("'command' must be an array of arguments (MAX never runs a shell string)")
        if not all(isinstance(item, str) for item in command):
            raise ToolError("every element of 'command' must be a string")

        args: dict = {"command": list(command)[:32]}
        if cwd:
            args["cwd"] = str(cwd)[:200]
        if timeout_s is not None:
            args["timeout_s"] = float(timeout_s)
        if approval_id:
            args["approval_id"] = str(approval_id)[:64]
        return self.manager.call(self.server_id, "execute_terminal_command", args)

    # ---------------------------------------------------------------- input
    def input_action(self, **kwargs) -> str:
        self._require()
        args = {k: v for k, v in kwargs.items() if v is not None}
        return self.manager.call(self.server_id, "control_mouse_keyboard", args)

    # -------------------------------------------------------------- screen
    def screen(self, monitor: int = 0) -> str:
        self._require()
        return self.manager.call(
            self.server_id, "read_active_screen", {"monitor": int(monitor or 0)}
        )


def register_desktop_tools(registry, provider: DesktopProvider) -> None:
    from ..core.tools import ToolContext, ToolSpec

    def _exec(args: dict, ctx: ToolContext) -> str:
        return provider.exec_command(
            args.get("command"),
            cwd=args.get("cwd"),
            timeout_s=args.get("timeout_s"),
            approval_id=args.get("approval_id"),
        )

    def _input(args: dict, ctx: ToolContext) -> str:
        allowed = {
            "action", "x", "y", "to_x", "to_y", "text", "keys", "amount", "approval_id",
        }
        return provider.input_action(**{k: v for k, v in args.items() if k in allowed})

    def _screen(args: dict, ctx: ToolContext) -> str:
        return provider.screen(int(args.get("monitor") or 0))

    registry.register(
        ToolSpec(
            name="desktop_exec",
            description=(
                "Run a local command inside the MAX workspace (argv array, never a shell). "
                "Read-only commands run at once; state-changing commands return an "
                "approval_id that the operator must grant at the console before MAX retries. "
                "Absolute paths outside the workspace are always denied."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "argv, e.g. [\"python\",\"-m\",\"pytest\",\"-q\"]",
                    },
                    "cwd": {"type": "string", "description": "workspace-relative directory"},
                    "timeout_s": {"type": "number"},
                    "approval_id": {"type": "string"},
                },
                "required": ["command"],
            },
            category="exec",
            handler=_exec,
        )
    )
    registry.register(
        ToolSpec(
            name="desktop_input",
            description=(
                "Control the local mouse and keyboard (click, move, type, hotkey, scroll, "
                "drag). Needs a granted input-session approval: the first call returns an "
                "approval_id, the operator grants it, then MAX retries with it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "move", "click", "double_click", "right_click", "middle_click",
                            "type", "hotkey", "press", "scroll", "drag",
                        ],
                    },
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "to_x": {"type": "integer"},
                    "to_y": {"type": "integer"},
                    "text": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "amount": {"type": "integer"},
                    "approval_id": {"type": "string"},
                },
                "required": ["action"],
            },
            category="exec",
            handler=_input,
        )
    )
    registry.register(
        ToolSpec(
            name="desktop_screen",
            description=(
                "Capture the local screen into the workspace and return its path and "
                "dimensions (read-only, no approval). MAX is text-only, so it reports the "
                "path rather than viewing the image."
            ),
            parameters={
                "type": "object",
                "properties": {"monitor": {"type": "integer", "description": "0 = primary"}},
            },
            category="exec",
            handler=_screen,
        )
    )
