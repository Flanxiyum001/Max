"""Plain-English "what MAX is doing" lines for the orb, from tool names only.

Only the tool name is used, never its arguments: arguments can hold private
or untrusted text, so the activity line stays display-safe.
"""

from __future__ import annotations

_LABELS = {
    "apps.open": "Opening an app or website",
    "clock.now": "Checking the time",
    "math.calculate": "Calculating",
    "memory.remember": "Saving to memory",
    "memory.recall": "Searching memory",
    "memory.forget": "Removing from memory",
    "notes.write": "Writing a note",
    "reminders.add": "Adding a reminder",
    "reminders.list": "Checking reminders",
    "reminders.complete": "Completing a reminder",
    "shell.run": "Running a command",
    "system.status": "Checking this PC",
    "system.gpu": "Checking the GPU",
    "weather.current": "Checking the weather",
    "web.read": "Reading a web page",
    "web.search": "Searching the web",
    "workspace.list": "Looking through files",
    "workspace.read": "Reading a file",
    "desktop.active_window": "Checking the active window",
    "desktop.list_windows": "Looking at open windows",
    "desktop.focus_window": "Switching windows",
    "pc.screen_size": "Checking the screen",
    "pc.type_text": "Typing",
    "pc.press_keys": "Pressing keys",
    "pc.click": "Clicking",
    "pc.move_mouse": "Moving the mouse",
    "pc.scroll": "Scrolling",
    "sandbox.run_pr": "Starting a PR sandbox run",
}

_SERVICES = {
    "github": "GitHub",
    "cloudflare": "Cloudflare",
    "railway": "Railway",
    "notion": "Notion",
    "linear": "Linear",
    "sentry": "Sentry",
    "figma": "Figma",
    "render": "Render",
}

_FAILED = {"denied": "not allowed", "failed": "failed"}


def describe(tool: str) -> str:
    """'web.search' -> 'Searching the web'; 'mcp.github.list_issues' -> 'GitHub: list issues'."""
    if tool in _LABELS:
        return _LABELS[tool]
    parts = tool.split(".")
    if len(parts) >= 3 and parts[0] == "mcp":
        service = _SERVICES.get(parts[1], parts[1].capitalize())
        action = " ".join(".".join(parts[2:]).replace("_", " ").replace("-", " ").split())
        return f"{service}: {action[:60]}" if action else f"Using {service}"
    words = " ".join(tool.replace(".", " ").replace("_", " ").split())[:60]
    return f"Using {words}" if words else "Working"


def activity_line(tool: str, status: str) -> str | None:
    """Line to show for one tool step, or None when nothing should change."""
    if status == "proposed":
        return describe(tool)
    if status in _FAILED:
        return f"{describe(tool)} — {_FAILED[status]}"
    return None


__all__ = ["activity_line", "describe"]
