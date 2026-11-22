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

## Siri-style voice (added)

- **Tap the orb / Space** → chime, listens, and **sends by itself** when you stop talking.
- **“Hey MAX”** (toggle top-right, off by default, remembered per browser): while the
  tab is open and visible, the browser listens for “Hey MAX” / “OK MAX”. Say
  “Hey MAX, what's the weather?” in one go, or “Hey MAX”, pause, then ask.
  In Chrome this means microphone audio is streamed to Google's speech service the
  whole time it is on; it stops when the tab is hidden or the toggle is off.
- **Follow-up:** after a spoken answer MAX listens ~6 s more for a follow-up, then
  goes back to waiting.
- **Living orb:** the orb swells and glows with your voice while listening and with
  MAX's voice while speaking (Web Audio level meter).

Tested in headless Chromium with a simulated speech recogniser and fake mic
(wake + command, wake then pause, follow-up, silence, approval by voice); not yet
with a real microphone.
