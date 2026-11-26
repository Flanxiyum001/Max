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
You can close and relaunch MAX directly without starting a web server. Once
installed, an unchanged checkout launches without reinstalling its dependencies.
If the desktop dependency configuration changes, the next start refreshes the
environment. A failed install can be retried by relaunching. If installed files
are damaged, run the following in PowerShell from the repository root:

    $env:MAX_DESKTOP_REPAIR = '1'
    .\start-max.cmd
    Remove-Item Env:\MAX_DESKTOP_REPAIR

Repair reinstalls packages, so it requires access to the package index. It does
not remove your local journal or configuration. If the venv's Python itself
no longer starts, exit MAX, rename `.venv` to `.venv-old`, and relaunch.

In the orb, Esc pauses an answer being read aloud. Type `continue` to resume
from the interrupted sentence, without starting the microphone or asking a model
again. Typing a new question discards the paused audio. The cursor lives only
for the current app session.

CI exercises the Windows launcher on a clean environment and on repeat launch,
then checks a clean Python wheel installation. These checks are offscreen and
do not replace a hands-on microphone, speaker, and first-run test on your PC.

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
- Creates a local SQLite task journal by default in the owner's AppData/XDG
  data directory. Tasks, timelines, and Trust records persist across restarts.
  The database is restricted to the current Unix user; Windows depends on
  the owner's AppData ACL. Backups and encryption are not implemented.
- Requires explicit owner action to queue tasks. The activity journal alone
  is not evidence that a model or tool executed.

## Local task journal and optional PostgreSQL

SQLite is enabled automatically for the native launcher. It creates the
needed tables from the current model schema, not from Alembic revision
migrations. This is still an early-use, single-owner local store. Do not
store data you cannot afford to lose and do not use it as production Trust
authorization for remote devices. See `.env.example` for optional
`MAX_LOCAL_DB_PATH` and `MAX_DESKTOP_DB_URL` overrides. Never commit
database files, secrets, or backups.

Alternatively, after PostgreSQL 17 is installed locally, create a development database,
set MAX_DATABASE_URL pointing explicitly to 127.0.0.1/localhost/::1 and a
specified port using the postgresql+psycopg driver, and apply the
repository's Alembic migrations as documented in CORE_BOOTSTRAP.md and
TASK_PERSISTENCE.md. For reference, a URL has this form:

    postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/max_dev

This is only an example—never commit actual credentials to source,
.vscode/launch.json, a screenshot, .env file or chat. Remote database
hosts are intentionally rejected by this development launcher.
No production DB or deployment is accessed by the preview.

With SQLite or a correctly migrated local PostgreSQL database the app can
add, list and inspect tracked tasks and read pending Trust requests.
The backend also exposes an explicit approval-decision method; the current
native Trust screen itself remains read-only. Journal entries cannot prove
external execution. For retrying an ambiguous task creation, the in-process
client reuses its request idempotency key until the same text is acknowledged.

## Current limitations and release gates

**This is not a production-ready personal AI assistant.** In this build:

- An opt-in, manually invoked verified-local model text trial exists;
  no fully autonomous agent chat, selected voice, microphone/STT/TTS or audio.
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
New native backend tests verify local DB guards, SQLite persistence and
file privacy, owner-scoped tasks/timeline, request-key replay and Qt storage
readiness.

A separate GitHub Actions Qt job installs the desktop dependency and runs
an offscreen window smoke test. No user device has been tested by CI.

Rollback: revert the feature and restore the prior native launcher behavior.
The local SQLite database may remain under the owner's data directory; do not
delete it without an independent backup and explicit owner intent. No hosted
provider or production deployment is activated by this storage change.
