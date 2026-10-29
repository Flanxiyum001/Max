"""MAX desktop control — a stdio MCP server with a built-in approval gate.

Run it as ``python -m max.desktop.server --root <workspace> --state <dir>``;
MAX starts it as a child process through its existing MCP manager, so it
inherits exactly the environment that manager builds (``PATH`` and ``LANG``
only — no Discord token, no API key, no SSH agent).

The three tools:

``execute_terminal_command``  argv-only (never a shell). Read-only commands in
                              the workspace run at once; anything that changes
                              state needs a one-shot human approval; a fixed
                              deny list is refused always.
``control_mouse_keyboard``    coordinate-based input. One approval covers a
                              short, counted session of events.
``read_active_screen``        save a screenshot inside the workspace.

Protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout (``initialize``,
``tools/list``, ``tools/call``, ``ping``). Logs go to stderr only — stdout is
reserved for the protocol.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from .approvals import ApprovalBook, ApprovalError, fingerprint
from ..core.tools import ToolError
from .io import GUI_ACTIONS, DesktopIO
from .policy import MAX_ARGS, classify

_SECRET_ARG = re.compile(
    r"^((?:--?)?[a-z0-9_-]*(?:key|token|secret|pass|passwd|password|credential)"
    r"[a-z0-9_-]*)=(.+)$",
    re.IGNORECASE,
)

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "execute_terminal_command",
        "description": (
            "Run a read-only or state-changing local command inside the MAX workspace. "
            "Pass an argv array (never a shell string). Read-only commands run immediately; "
            "commands that change state return status='approval_required' with an approval_id "
            "that a human must grant at the console (`max desktop approve <id>`) before you "
            "retry the exact same command with that approval_id. Absolute paths, '..', shell "
            "metacharacters and privilege-escalation/network programs are always denied."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "argv, e.g. [\"python\",\"-m\",\"pytest\",\"-q\"]",
                },
                "cwd": {"type": "string", "description": "workspace-relative working directory"},
                "timeout_s": {"type": "number", "description": "seconds, clamped to the configured maximum"},
                "approval_id": {"type": "string", "description": "id returned by a granted approval"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "control_mouse_keyboard",
        "description": (
            "Move the mouse, click, type or send hotkeys on the local desktop. Requires a "
            "granted input-session approval: first call returns approval_id, a human grants it "
            "with `max desktop approve <id>`, then you retry with that approval_id (one grant "
            "covers a bounded number of events before it expires)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(GUI_ACTIONS),
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
    },
    {
        "name": "read_active_screen",
        "description": (
            "Capture the local screen into the workspace and return its path and dimensions. "
            "Read-only, no approval required; images are kept only inside the workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"monitor": {"type": "integer", "description": "0 = primary"}},
        },
    },
]

DEFAULT_MAX_OUTPUT = 4000


def sandbox_env(root: Path) -> dict:
    """Minimal environment for a child process: no secrets can leak in."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LANG", "C.UTF-8"),
        "HOME": str(root),
        "TMPDIR": str(Path(root) / ".tmp"),
        "MAX_DESKTOP": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def redact_argv(argv) -> list[str]:
    """Mask secret-shaped ``--flag=value`` arguments before anything is logged."""
    out = []
    for arg in argv:
        match = _SECRET_ARG.match(arg)
        out.append(f"{match.group(1)}=***" if match else arg)
    return out


class DesktopMCPServer:
    def __init__(
        self,
        root: Path,
        state_dir: Path,
        *,
        ttl_s: float = 600.0,
        gui_ttl_s: float = 300.0,
        gui_max_actions: int = 50,
        max_timeout_s: float = 30.0,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".tmp").mkdir(parents=True, exist_ok=True)
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.book = ApprovalBook(
            self.state_dir, ttl_s=ttl_s, gui_ttl_s=gui_ttl_s, gui_max_actions=gui_max_actions
        )
        self.io = DesktopIO(self.root)
        self.max_timeout_s = max(1.0, float(max_timeout_s))
        self.max_output = max(200, int(max_output))
        self.audit_path = self.state_dir / "desktop_audit.jsonl"

    # -------------------------------------------------------------- protocol
    def handle(self, msg: dict):
        mid = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str):
            return None
        if mid is None:  # notification (e.g. notifications/initialized)
            return None
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "max-desktop", "version": "1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = self._call(params)
        else:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            if name == "execute_terminal_command":
                payload = self.exec_command(args)
            elif name == "control_mouse_keyboard":
                payload = self.input_action(args)
            elif name == "read_active_screen":
                payload = self.read_screen(args)
            else:
                payload = {"status": "error", "error": f"unknown tool '{name}'"}
        except ApprovalError as exc:
            payload = {"status": "error", "error": f"approval error: {exc}"}
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the server
            self._record("desktop_error", tool=str(name)[:60], error=type(exc).__name__)
            payload = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "isError": False,
        }

    # ------------------------------------------------------------ terminal
    def exec_command(self, args: dict) -> dict:
        verdict = classify(args.get("command"))
        if verdict.tier == "denied":
            self._record("desktop_exec_denied", reason=verdict.reason)
            return {"status": "denied", "reason": verdict.reason}

        argv = list(verdict.argv)
        cwd = self._cwd(args.get("cwd"))
        fp = fingerprint("term", argv, str(cwd))
        approval_id = str(args.get("approval_id") or "").strip()

        if verdict.tier == "approval":
            if not approval_id:
                record = self.book.request("term", argv, str(cwd), verdict.reason)
                self._record("desktop_approval_requested", id=record["id"],
                             argv=redact_argv(argv), cwd=str(cwd))
                return self._approval_required(record, argv)
            try:
                self.book.consume(approval_id, fp, "term")
            except ApprovalError as exc:
                record = self.book.request("term", argv, str(cwd), verdict.reason)
                self._record("desktop_approval_rejected", id=approval_id, detail=str(exc)[:200])
                return {
                    **self._approval_required(record, argv),
                    "reason": f"approval could not be used: {exc}",
                    "previous_approval_id": approval_id,
                }

        return self._run(argv, cwd, args.get("timeout_s"), approved=verdict.tier == "approval")

    def _approval_required(self, record: dict, argv: list[str]) -> dict:
        return {
            "status": "approval_required",
            "approval_id": record["id"],
            "expires_in_s": max(0, int(record["expires_ts"] - time.time())),
            "command": redact_argv(argv),
            "reason": record.get("reason") or "",
            "instructions": (
                f"Human approval required. Ask the operator to run "
                f"`max desktop approve {record['id']}` at the console, then retry this "
                f"exact command with approval_id='{record['id']}'. Approval never "
                f"transfers to a different command."
            ),
        }

    def _run(self, argv: list[str], cwd: Path, timeout_s, approved: bool) -> dict:
        timeout = self.max_timeout_s
        if isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool):
            timeout = min(max(1.0, float(timeout_s)), self.max_timeout_s)

        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv only, never a shell
                argv,
                cwd=str(cwd),
                env=sandbox_env(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
            )
        except FileNotFoundError:
            self._record("desktop_exec_error", argv=redact_argv(argv), error="not_found")
            return {"status": "error", "error": f"program not found: {argv[0]}"}
        except OSError as exc:
            self._record("desktop_exec_error", argv=redact_argv(argv), error=type(exc).__name__)
            return {"status": "error", "error": f"could not start command: {exc}"}

        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                proc.kill()
            out, err = proc.communicate()

        elapsed = round(time.monotonic() - started, 3)
        stdout, out_cut = _clip(out or "", self.max_output)
        stderr, err_cut = _clip(err or "", self.max_output)
        self._record(
            "desktop_exec",
            argv=redact_argv(argv),
            cwd=str(cwd),
            exit=proc.returncode,
            timed_out=timed_out,
            seconds=elapsed,
            approved=approved,
        )
        return {
            "status": "ok" if proc.returncode == 0 and not timed_out else "failed",
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "seconds": elapsed,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": out_cut or err_cut,
        }

    # ---------------------------------------------------------------- input
    def input_action(self, args: dict) -> dict:
        action = str(args.get("action") or "").strip().lower()
        if action not in GUI_ACTIONS:
            raise ToolError(
                f"unknown action '{action}' — permitted: {', '.join(sorted(GUI_ACTIONS))}"
            )
        fp = fingerprint("gui", ["gui", "control_mouse_keyboard"], "")
        approval_id = str(args.get("approval_id") or "").strip()

        if not approval_id:
            record = self.book.request(
                "gui", ["gui", "control_mouse_keyboard"], "", "desktop input session"
            )
            self._record("desktop_approval_requested", id=record["id"], kind="gui")
            return {
                "status": "approval_required",
                "approval_id": record["id"],
                "expires_in_s": max(0, int(record["expires_ts"] - time.time())),
                "max_actions": record["max_actions"],
                "reason": "controlling the mouse and keyboard needs human approval",
                "instructions": (
                    f"Ask the operator to run `max desktop approve {record['id']}`, then "
                    f"retry with approval_id='{record['id']}'. One grant covers up to "
                    f"{record['max_actions']} input events before it expires."
                ),
            }

        record = self.book.consume(approval_id, fp, "gui")
        result = self.io.action(
            action,
            x=args.get("x"),
            y=args.get("y"),
            to_x=args.get("to_x"),
            to_y=args.get("to_y"),
            text=args.get("text"),
            keys=args.get("keys"),
            amount=args.get("amount"),
        )
        self._record("desktop_input", action=action, remaining=max(0, record["max_actions"] - record["uses"]))
        return result

    # -------------------------------------------------------------- screen
    def read_screen(self, args: dict) -> dict:
        result = self.io.screenshot(int(args.get("monitor") or 0))
        self._record("desktop_screenshot", path=result["path"], bytes=result["bytes"])
        return result

    # ------------------------------------------------------------- helpers
    def _cwd(self, raw) -> Path:
        value = str(raw or "").strip()
        if not value or value in {".", "./"}:
            return self.root
        if value.startswith("/") or value.startswith("~") or ".." in value.split("/"):
            raise PermissionError("cwd must stay inside the workspace")
        candidate = (self.root / value).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PermissionError("cwd escapes the workspace")
        if not candidate.is_dir():
            raise NotADirectoryError(f"not a directory: {value}")
        return candidate

    def _record(self, event: str, **detail) -> None:
        entry = {"ts": round(time.time(), 3), "event": event, **detail}
        try:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ main loop
    def serve(self, stdin=None, stdout=None) -> int:
        src = stdin if stdin is not None else sys.stdin
        sink = stdout if stdout is not None else sys.stdout
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            response = self.handle(msg)
            if response is None:
                continue
            sink.write(json.dumps(response, default=str) + "\n")
            sink.flush()
        return 0


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]", True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="max-desktop", description=__doc__)
    parser.add_argument("--root", required=True, help="workspace root (the only readable/writable path)")
    parser.add_argument("--state", required=True, help="directory for approval records and audit")
    parser.add_argument("--ttl", type=float, default=600.0, help="terminal approval lifetime (s)")
    parser.add_argument("--gui-ttl", type=float, default=300.0, help="input-session lifetime (s)")
    parser.add_argument("--gui-actions", type=int, default=50, help="input events per grant")
    parser.add_argument("--max-timeout", type=float, default=30.0, help="per-command time cap (s)")
    parser.add_argument("--max-output", type=int, default=DEFAULT_MAX_OUTPUT, help="stdout/stderr cap (chars)")
    args = parser.parse_args(argv)

    server = DesktopMCPServer(
        Path(args.root),
        Path(args.state),
        ttl_s=args.ttl,
        gui_ttl_s=args.gui_ttl,
        gui_max_actions=max(1, args.gui_actions),
        max_timeout_s=args.max_timeout,
        max_output=args.max_output,
    )
    server._record("desktop_server_started", root=str(server.root), max_args=MAX_ARGS)
    return server.serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
