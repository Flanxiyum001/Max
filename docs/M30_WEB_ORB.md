# M30 — MAX web orb (hosted on Render)

**Status:** Implemented. Server tested with FastAPI TestClient; page driven in
headless Chromium (login, typing, approval, answers; no JS errors).
**Not yet verified:** real model, real neural voice (the TTS host was unreachable
from the build sandbox), real microphone, the live Render deployment.

## What it is

The orb in a browser: black page, animated glass orb, nothing else.

- **Passcode** screen first (`MAX_WEB_PASSCODE`, ≥ 8 chars). A signed, HttpOnly,
  SameSite=Strict cookie keeps me logged in for 7 days. 8 wrong tries / 10 min → locked.
- **Type** anywhere to chat; **Space** or **tap the orb** to speak (browser speech
  recognition — Chrome/Edge; in Chrome the audio is processed by Google).
- Effectful tools show **Allow / Deny** buttons (Enter / Esc). No answer in 120 s = deny.
- Spoken questions get **spoken answers** in a natural neural female voice
  (`en-IE-EmilyNeural`, an Irish English voice; change with `MAX_WEB_VOICE`,
  `MAX_WEB_VOICE_RATE`, `MAX_WEB_VOICE_PITCH`). Esc stops speech.
- Due reminders are shown and spoken (checked every 30 s while the page is open).

## Tools on the hosted server

Memory, reminders, web search/read, weather, clock and maths. **Never** shell,
app launching, PC status or workspace files (those would act on Render's
machine). Personal tools (memory, reminders) are sent to the cloud model only
with `MAX_CLOUD_PERSONAL_TOOLS=yes`.

## Render setup

`render.yaml` defines a free web service (`python -m max_core.web`). Set in the
dashboard: `MAX_WEB_PASSCODE`, `MAX_TRIAL_ENDPOINT`, `MAX_TRIAL_MODEL`,
`MAX_TRIAL_API_KEY` (free OpenAI-compatible provider). Free instances sleep when
idle (first request can take ~1 min) and their disk is wiped on restart, so
memory/reminders do not survive restarts unless a persistent disk is added.

## Voice provider notes

Speech is generated with the `edge-tts` library (LGPL-3.0), which uses Microsoft
Edge's online read-aloud service. It needs no key but is not an official,
supported API and may change or be rate-limited. The voice is a stock Microsoft
voice, not a copy of any actor's voice.

## Run locally

```
pip install -e '.[web]'
MAX_WEB_PASSCODE=... MAX_WEB_INSECURE_COOKIE=yes python -m max_core.web   # http://127.0.0.1:10000
```

Rollback: revert this PR and delete the Render service.
