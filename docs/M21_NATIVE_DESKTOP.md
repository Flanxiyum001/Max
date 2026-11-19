# M21 — MAX Native Desktop App

## Launch MAX as an actual desktop program (Windows)

**Fastest:** clone the MAX repo into VS Code and double-click start-max.cmd
in the repository root. It creates a .venv using Python 3.13 or 3.11,
installs the optional Qt package and opens MAX in its **own native window**.
No web browser tab is needed.

Or, from the VS Code PowerShell terminal:

    py -3.13 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[desktop,dev]"
    .\.venv\Scripts\python.exe -m max_core.native_desktop

To debug with F5, choose the repo's .venv Python interpreter in VS Code,
then choose **MAX Desktop (Native)** from .vscode/launch.json.
If you use Python 3.11, replace -3.13 with -3.11 in the venv command.
You can close and relaunch MAX directly without starting a web server.

A Windows executable installer, auto-update and startup integration are
*not* included yet: this is a source-code early-use desktop application.
PySide6 is an opt-in dependency; the regular MAX Core does not import Qt.
Official Qt for Python documentation: https://doc.qt.io/qtforpython-6/

On Arch Linux, install Python 3.11 or 3.13, create a venv, install
-e '.[desktop,dev]' and run python -m max_core.native_desktop.
Wayland/Hyprland focus and compositor tests are still pending.

## What this application actually does

- Provides a standalone Qt Widgets application with a MAX-themed orb,
  panels, shortcuts, navigation, task composer, Trust view and quiet mode.
  Quiet mode stops orb animation; it does not change OS notifications.
- Starts a private in-process FastAPI Core through an ASGI TestClient.
  It does NOT bind an HTTP port or use a browser WebView.
  A random 256-bit bootstrap credential remains only in Python memory,
  rather than being pasted into the desktop app or stored to disk.
- Uses existing Core task, timeline, status and Trust contracts.
  All task creation requires an explicit user click. Task creation is
  NOT AI execution; Trust requests are always read-only.
- Supports local commands /help, /status, /tasks, /trust and /task followed
  by text. /task only pre-fills the task editor. Ctrl+K and Alt+1..4 are
  app-focused shortcuts, not global system keyboard hooks.
- Starts and remains usable as a UI preview when no database is configured.
  It honestly shows that task persistence and Trust require PostgreSQL.

## Optional: enable real local task journaling

After PostgreSQL 17 is installed locally, create a development database,
set MAX_DATABASE_URL pointing explicitly to 127.0.0.1/localhost/::1 and a
specified port using the postgresql+psycopg driver, and apply the
repository's Alembic migrations as documented in CORE_BOOTSTRAP.md and
TASK_PERSISTENCE.md. For reference, a URL has this form:

    postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/max_dev

This is only an example—never commit actual credentials to source,
.vscode/launch.json, a screenshot, .env file or chat. Remote database
hosts are intentionally rejected by this development launcher.
No production DB or deployment is accessed by the preview.

With a correctly migrated local database the app can add, list and inspect
tracked tasks and read pending Trust requests. It cannot claim completion
from journal events or approve real side effects. For retrying an ambiguous
task creation, the in-process client reuses its request idempotency key
until the same text is acknowledged.

## Current limitations and release gates

**This is not a production-ready personal AI assistant.** In this build:

- No live model conversation, selected voice, microphone/STT/TTS or audio.
- No real computer control, screen/clipboard capture, file manipulation,
  global wake hotkey, always-on-top gaming overlay or MCP tool execution.
- No separate device authentication, remote desktop connectivity,
  installable signed Windows .exe, automated recovery, backup/restore,
  operating-system permission review or independent security certification.
- A healthy in-process Core is not evidence that personal workflows can run.
- A Windows/Wayland machine must be tested physically. Qt offscreen CI
  verifies construction and shutdown, not real GPU/compositor behaviour.

Privacy/permissions are intentionally narrower than the planned MAX
assistant. A later native runtime should replace in-process development
authentication with real device-bound sessions and independently reviewed
effect permissions before executing any OS or MCP action.

## Tests and rollback

The regular Python 3.11 and 3.13 CI keeps Ruff, strict mypy, migrated
ephemeral PostgreSQL integration, the full unit/security tests and audit.
New native backend tests verify local DB guard, no-db startup, owner-scoped
tasks/timeline and request-key replay.

A separate GitHub Actions Qt job installs the desktop dependency and runs
an offscreen window smoke test. No user device has been tested by CI.

Rollback: revert this PR. No DB migration, real provider access, secret,
local system installation, external calls or production deploy is performed.
Keep the PR separate and require manual owner merge.
