# MAX

**My personal AI assistant. Built as a single-owner personal project.**

MAX is my project to build a personal, voice-capable AI assistant that stays under my control, tracks work durably, gates consequential actions behind explicit approval, and respects my privacy boundaries.

I'm designing MAX so that I own its orchestration, trust rules, memory model, tool allowlist, and interface contracts. External libraries and cloud model providers are replaceable adapters behind boundaries that I enforce in code.

> **Current build status:** MAX now has a working **local desktop build** on Windows (`start-max.cmd` / `python -m max_core.native_desktop`) with an in-process FastAPI Core, automatic local SQLite persistence, live multi-turn OpenAI or Anthropic text chat, a bounded local-tool allowlist gated by MAX Trust, plus opt-in reviewed HTTPS MCP tools, interactive Trust Inbox approvals, and opt-in push-to-talk / text-to-speech voice controls. It remains a single-owner local build—not a hosted service, multi-user product, or unattended OS automation agent.

---

## Quick Start (Windows Local Desktop)

### 1. Launch the desktop app

Double-click **`start-max.cmd`**, or run it from PowerShell:

```powershell
.\start-max.cmd
```

On first run, `start-max.cmd` creates `.venv` (using Python 3.11+) and installs the package with optional Qt desktop dependencies (`pip install -e ".[desktop]"`), then launches `python -m max_core.native_desktop`.

For an offscreen verification check without keeping a window open:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m max_core.native_desktop --smoke-test
```

### 2. Configure an OpenAI or Anthropic API key (no code edits required)

Configuration is loaded in this order of precedence: **`environment variables > config.json > .env > defaults`**.

- **Option A — Local `.env` file (recommended for quick local testing):**
  Copy `.env.example` to `.env` in the repository root and set your key:

  ```dotenv
  MAX_LLM_PROVIDER=openai
  OPENAI_API_KEY=your-openai-key
  # Or use Anthropic:
  # MAX_LLM_PROVIDER=anthropic
  # ANTHROPIC_API_KEY=your-anthropic-key
  MAX_COST_USD=0.05
  ```

  `.env` is gitignored—never commit real keys or tokens.

- **Option B — User config JSON outside the repository:**
  Create `%LOCALAPPDATA%\MAX\config.json` on Windows (or point `MAX_CONFIG_PATH` to any JSON file):

  ```json
  {
    "active_provider": "openai",
    "max_cost_usd": 0.05,
    "providers": {
      "openai": {
        "api_key": "your-openai-key",
        "model_id": "gpt-4o-mini"
      }
    }
  }
  ```

- **Option C — Process environment variables:**
  Set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `MAX_LLM_PROVIDER` (`openai`, `anthropic`, or `auto`), `MAX_OPENAI_MODEL`, `MAX_ANTHROPIC_MODEL`, or `MAX_COST_USD` in your shell before launching.

If no API key is configured, MAX still launches cleanly and shows an actionable configuration prompt when you send a chat message.

### 3. Local storage (SQLite by default, optional local PostgreSQL)

- **Default zero-setup SQLite:** When `MAX_DATABASE_URL` is unset, MAX initializes a local WAL-mode SQLite database outside the Git working tree at:
  - Windows: `%LOCALAPPDATA%\MAX\max_desktop.sqlite3`
  - POSIX: `$XDG_DATA_HOME/max/max_desktop.sqlite3` (or `~/.local/share/max/max_desktop.sqlite3`)
  - Override path anytime with `MAX_LOCAL_DB_PATH` or `MAX_DESKTOP_DB_URL`.
- **Optional local PostgreSQL:** If you set `MAX_DATABASE_URL` (must be loopback `127.0.0.1` / `localhost` / `::1` with Alembic migrations applied), MAX uses that PostgreSQL instance instead.
- **Important `.env` note:** If your `.env` defines `MAX_DATABASE_URL` for PostgreSQL development while PostgreSQL is stopped or unmigrated, the desktop app will report `task_store_not_ready` (`Local database is not ready`) instead of falling back silently. Comment out `MAX_DATABASE_URL` in `.env` (or set `MAX_LOCAL_DB_PATH`) to use the local SQLite database.

---

## What Works in This Local Build

### Native Desktop Window (`max_core.native_desktop`)
- **Default orb:** `python -m max_core.native_desktop` opens M25's minimal local-only orb. The new cloud chat, local-tool and voice controls live in the separately opened `python -m max_core.native_desktop --dashboard` window until an explicitly reviewed orb integration is built.
- **In-process Core bridge (`backend.py`)**: Uses FastAPI's in-process ASGI `TestClient` with an ephemeral per-run bearer token—no open TCP port, no external HTTP listener, and no browser token handoff.
- **Four-tab workspace (`qt_ui.py`)**:
  - **Overview**: Cyan/turquoise holographic orb (`IDENTITY.md`, driven by `OrbReducer`), live multi-turn assistant chat transcript, tool execution outcomes panel, voice controls, and provider/storage summary cards.
  - **Tasks**: Persistent Task Journal (`Local SQLite ready` or `Local PostgreSQL ready`) to queue tracked tasks (`1–500` characters) and inspect signed task timeline events across restarts.
  - **Trust**: Interactive **Trust Inbox** listing pending `ApprovalGrant` items persisted in the database, with explicit **Approve selected** and **Deny selected** buttons.
  - **System**: Live Core/storage readiness and honest capability inventory.
- **Command & chat bar**: Supports `/help`, `/status`, `/tasks`, `/trust`, `/tools`, `/voice`, and `/task <objective>`, and sends all other input to live assistant chat off the UI thread (`QThreadPool`).
- **Quiet mode**: Stops orb animation in-window and revokes active microphone capture; it does not modify OS-level notification settings.

### Live Cloud Text Chat (`max_core.assistant_chat`, `max_core.gateway`)
- Connects over `httpx` to **OpenAI** (`/v1/chat/completions`, default `gpt-4o-mini`) or **Anthropic** (`/v1/messages`, default `claude-sonnet-4-5`).
- Applies `with_persona()` (`SOUL.md` / `persona.py`), per-turn spend caps (`MAX_COST_USD`), and pre-egress credential inspection (`privacy_guard`) so secrets or API-key-like strings are blocked locally before leaving the process.
- Surfaces missing keys, `401`/`403` auth errors, `429` rate limits, `5xx` errors, timeouts, and network errors as sanitized status messages without leaking API keys or raw tracebacks.

### Bounded Local Tools & MAX Trust (`max_core.local_tools`, `max_core.trust`)
The trusted local tool registry contains 10 typed allowlisted tools, validated against strict JSON schemas and audited in `max_trust_audit` (metadata only, never plaintext secrets). **Cloud chat does not advertise private local read tools** (workspace, note, task, or system status) because their outputs could disclose non-secret private data to a remote model without separate export consent:

| Risk Tier | Behavior | Tools |
| --- | --- | --- |
| **R0** (read-only / deterministic) | Auto-audited and executed immediately | `dev.health`, `dev.add`, `system.status`, `task.status`, `task.list`, `workspace.list_files`, `workspace.read_file`, `notes.list` |
| **R1 / R2** (state-changing) | Creates a single-use `ApprovalGrant` in **Trust Inbox**; runs only after explicit **Approve** click | `task.create` (`R1`), `notes.save` (`R2`) |

- `workspace.list_files` and `workspace.read_file` are confined to `MAX_WORKSPACE_ROOT` (defaulting to the current working directory) and block `.git`, `.venv`, `.env*`, private keys, and `.sqlite3`/`.db` files.
- `notes.save` and `notes.list` operate only inside `MAX_NOTES_DIR` (defaulting to `%LOCALAPPDATA%\MAX\notes` on Windows).
- In remote/cloud chat, `dev.health` and `dev.add` may be offered for deterministic safe computation; Trust-gated `task.create` and `notes.save` require an owner approval. **No private local read tool** may be offered automatically. A cloud model's undeclared request to read files is denied.
- Any undeclared tool (for example `shell.run`) is denied before execution.

### Remote MCP connections (`max_core.mcp_desktop`)
- Add trusted HTTPS MCP servers via the System tab; explicitly discover schemas and enable individual tools. Never inherits ChatGPT connector tokens. Credential values remain outside the profile and the repository.
- Every invocation is Trust-gated with a single-use approval and live schema-drift checks. See [configuration, GitHub setup and limitations](docs/MCP_DESKTOP.md).

### Voice Interaction (`max_core.voice`)
- **Opt-in Push-to-Talk STT**: Bounded microphone capture (`QtMicrophoneRecorder` + `LocalActivationGate`) starts only on an explicit **Push to Talk** click—never automatically on startup. With an explicitly configured remote provider, **microphone audio is uploaded to the external speech provider** (OpenAI-compatible `/v1/audio/transcriptions`, `whisper-1`). A visible per-recording consent dialog is required before capture; the trusted voice controller blocks remote recording without that consent and clears consent after every turn or cancellation.
  - *Note:* Speech-to-text requires `OPENAI_API_KEY` even if you choose `MAX_LLM_PROVIDER=anthropic` for text chat.
  - All speech transcripts enter the chat flow strictly as untrusted user text—never as direct slash commands or self-approved tool calls.
- **Text-to-Speech (TTS)**: Uses local Qt `QTextToSpeech` (`winrt` / `sapi` on Windows where available) honoring `MAX_VOICE_LOCALE=en-GB` and `MAX_VOICE_PRESENTATION=feminine` preferences when matching OS voices exist, controlled by the **Speak: on/off** toggle and **Stop** barge-in button (`BargeInGate`).
- **Graceful headless/hardware fallback**: Under `QT_QPA_PLATFORM=offscreen` or when no microphone/audio device is present, MAX stays usable and reports the fallback state clearly without crashing.

---

## External Components, Licenses, Costs & Trust Boundaries

| Component | Role | License | Cost & Trust Boundary |
| --- | --- | --- | --- |
| **FastAPI / Starlette / Uvicorn** | In-process Core API & local schemas | MIT / BSD-3-Clause | Local open-source dependencies; desktop binds no TCP socket |
| **SQLAlchemy / Alembic / psycopg** | Task Journal, Trust & audit persistence | MIT / LGPL-3.0 | Local SQLite (default) or user-configured loopback PostgreSQL |
| **httpx** | Cloud LLM & OpenAI STT HTTP client | BSD-3-Clause | Outbound HTTPS only on explicit chat or STT turn; `trust_env=False` |
| **PySide6** (`[desktop]` extra) | Native desktop UI, `QTextToSpeech`, `QAudioSource` | LGPLv3 / Commercial Qt | Local UI & OS speech/audio bindings |
| **OpenAI API** | Chat Completions + Whisper STT (`/v1/audio/transcriptions`) | Proprietary cloud API | User-billed API key; bounded by `MAX_COST_USD` and `privacy_guard`; responses treated as untrusted |
| **Anthropic API** | Messages API for text & tool proposals | Proprietary cloud API | User-billed API key; bounded by `MAX_COST_USD` and `privacy_guard`; responses treated as untrusted |
| **Windows `winrt` / `sapi` voices** | Local OS speech synthesis via Qt | OS / Platform runtime | Local OS voices only; no celebrity/actor voice cloning |

---

## What Is Still Planned / Not Implemented

To keep verified capabilities distinct from future design goals, the following are **not** included in this build:

- **No unrestricted OS/shell/browser control** or unattended background computer automation.
- **No local stdio MCP subprocesses or interactive OAuth login** yet. Reviewed HTTPS MCP tools can be enabled individually and executed only after owner approval. See [desktop MCP setup](docs/MCP_DESKTOP.md).
- **No always-on wake-word daemon, global OS-wide hotkeys, or gaming overlay**.
- **No Arch Linux / Hyprland real-device verification** or **iPhone mobile web client** in this desktop build.
- **No signed standalone `.exe` installer** (launch runs from `.venv` via `start-max.cmd`).
- **No multi-user accounts or hosted/remote deployment**.

---

## Architecture & Project Documentation

- [Identity & principles](MAX.md) · [Personality](SOUL.md) · [Operating model](CORE.md) · [Visual & voice identity](IDENTITY.md) · [Security baseline](SECURITY.md)
- [Engineering rules](AGENTS.md) · [Architecture overview](ARCHITECTURE.md)
- Detailed design records in [`docs/`](docs/): [Product specification](docs/PRODUCT_SPECIFICATION.md), [Technical design](docs/TECHNICAL_DESIGN.md), [Security architecture](docs/SECURITY_ARCHITECTURE.md), [Roadmap](docs/ROADMAP.md).

## Development & Verification Commands

```powershell
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
.\.venv\Scripts\mypy.exe src/max_core
.\.venv\Scripts\pytest.exe -q
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m max_core.native_desktop --smoke-test
```
