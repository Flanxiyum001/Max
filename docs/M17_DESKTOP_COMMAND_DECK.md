# M17 — MAX Desktop Command Deck

## Reference and intent

Dexter Copilot (https://dextercopilot.ai/) demonstrates the value of a
compact voice-and-text co-pilot, connected services, fast commands and clear
assistant feedback. This milestone adapts those interaction patterns to MAX's
personal-desktop use case. It uses original MAX visuals and source code. No
Dexter assets, branding, OBS, streaming, Twitch or YouTube tools are copied.

## What works after owner merge

- Open http://127.0.0.1:8765/desktop on the same trusted machine as MAX Core.
- Log into the **temporary local owner session** using the Core bootstrap bearer
  token. It is held in tab JavaScript memory only, never stored in cookies,
  local/session storage, URLs, service workers, analytics or HTML assets.
- Read task records from GET /v1/tasks and their metadata history from
  GET /v1/tasks/{id}/events.
- Read pending owner Trust requests from GET /v1/approvals. No approvals can be
  issued from this screen.
- Explicitly queue task records with POST /v1/tasks and an idempotency key.
  A queued record does NOT execute a model, OS tool or background worker.
- Type /help, /tasks, /trust, /status and /task followed by a description into
  the command bar. The last command pre-fills an editor and requires an
  additional deliberate click before anything is queued.
- Check GET /health/ready for the difference between a healthy bootstrap API
  and a migrated PostgreSQL task store.
- Hold the microphone icon to test local audio permission. No audio is uploaded,
  stored, transcribed or routed to an AI model; releasing or losing focus stops
  the tracks.
- Navigate via keyboard (Ctrl+K inside the tab, Alt+1..4 for sections).
  No global hotkey, native desktop overlay, autonomous assistant or AI text
  chat is claimed.

Static HTML/CSS/JS is packaged with MAX Core and served under three fixed
localhost routes, with restrictive Content Security Policy, anti-frame headers,
no-store and same-origin-only requests. API-provided fields are inserted only
through textContent; there are no external scripts or CDNs.

## First-run instructions (Windows PowerShell)

Use Python 3.11 or 3.13. From a trusted repository checkout:

    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
    $env:MAX_API_BEARER_TOKEN = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    .\.venv\Scripts\python.exe -m max_core

Visit http://127.0.0.1:8765/desktop in the same machine's browser and paste
the token from your own PowerShell session. It is available locally as
$env:MAX_API_BEARER_TOKEN. Do not share it, paste it into third-party pages,
or include it in screenshots.

The default configuration has no task database. Thus Core readiness reports
bootstrap-only and task/Trust APIs return HTTP 503. To enable the journal,
configure a local migrated PostgreSQL 17 database using MAX_DATABASE_URL and
the setup procedure in CORE_BOOTSTRAP.md and TASK_PERSISTENCE.md. Never point
a prototype at a production database.

## Important limitations and next gates

- The bootstrap bearer token is temporary development authentication, NOT a
  production login or elevated approval method. Browser extensions, local
  malware or compromised same-origin scripts can still read it.
- No native tray app, Tauri wrapper, Windows/Hyprland compositor proof,
  global push-to-talk, speech recognition, TTS or real voice conversation.
- No real FreeLLMAPI/model verification, confidential-cloud egress approval,
  remote machine enrolment, PC control, permissioned MCP execution, coding
  sandbox, or independent external-action evidence.
- No customer deployment, backups, restore drills, credential rotation or
  production penetration test has been completed.

Next milestones should separately deliver:
1. Physical Windows/Hyprland window, focus and overlay validation.
2. Short-lived device-bound owner sessions and separate step-up approval flow.
3. Live model text interaction via reviewed Gateway, with privacy budgets.
4. Explicitly approved OS/MCP execution in a sandbox and verified effects.
5. Voice integration, memory review, fault recovery and production hardening.

## Acceptance, risks, rollback

Before manual merge: verify lint, Ruff formatting, strict mypy, both Python CI
matrices, ephemeral PostgreSQL migration/integration, full security tests,
dependency audit, localhost host checks, keyboard/mic interaction and desktop
viewport behavior. Source and API tests are not physical-device tests.

No database migration or provider call is made by this milestone. Roll back
by reverting the packaged desktop files, three asset routes, package-data
setting, tests and documentation. Never auto-merge or deploy this PR.
