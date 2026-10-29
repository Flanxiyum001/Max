# MAX PC — Windows-first Jarvis milestone

## MAX LOOK — first screen-aware desktop milestone

**Status:** read-only local PC feature. From the compact companion (default)
or full dashboard, choose **Premiere**, **Notepad**, or **VS Code** and click
**LOOK**. MAX must be awake, and you must have connected a **vision-capable**
model and passed **TEST AI RESPONSE** first.

1. The app asks for **explicit per-image consent** and warns whether the
   connected provider is local or external. **Cancel** means no screenshot.
2. After confirming, switch to the selected Windows app within **3 seconds**.
3. MAX verifies the actual foreground process executable and title, then
   captures only the window region on the **primary monitor** (no video or
   continuous monitoring). If the window is wrong, minimized, offscreen, or
   changes during capture, it fails closed without uploading.
4. The JPEG is compressed to at most 1280 × 900 pixels / 1.2 MB. It is kept
   in memory, **not written to MAX's disk or chat history**, and passed to the
   configured model as an image attachment. This is one model request in the
   normal OpenAI-compatible client workflow; the provider adapter may retry
   on transient errors and such retries can consume credits.
5. MAX returns a **text-only observation**. It cannot yet click the elements
   it sees, guarantee that a model correctly interpreted the image, or verify
   a subsequent task has happened.

**Privacy matters.** Window pixels may show private documents, notification
overlays or credentials. Use a disposable/safe window, close sensitive popups,
and inspect what is visible *before* approving. A cloud model provider receives
the image and applies its own data retention/processing policy; "not saved by
MAX" does not mean the provider does not retain it. For local image processing
use a vision-capable local Ollama model. Text-only models may reject image
messages. There is no automatic, spoken, model-generated, or phone-triggered
screen upload.

**Windows setup:** Install the optional desktop packages via
\`python -m pip install -e ".[pc,stt,desktop]"\`. This capability does not run
on the deployed headless server and cannot be end-to-end proven in GitHub CI.
If capture is denied, ensure the selected app is foreground on the primary
monitor, not the MAX window. No executable data, window title or screenshot
is written to the app logs.

**Next Jarvis milestone:** Read-only scene interpretation → proposed
multi-step plan → explicit action approval → one action at a time → fresh
observation and actual outcome verification. Real-time wake-word audio, VAD,
streaming ASR, low-latency TTS, user memory and OS service integrations
remain separate engineering work. This release does not claim autonomy.

## Reference reel: voice-first desktop (October 2026)

The user-supplied 72-second reel shows an assistant working **beside ordinary
Windows applications**: opening an editor, manipulating a Premiere Pro timeline,
performing web/video searches, and talking through a real telephone call.
It does **not** show a giant amber orb as the primary application.

For that reason `python -m max pc run` now opens the **compact voice
companion** by default. It occupies a small movable window while Windows
applications remain visible. The original holographic interface is preserved
at `python -m max pc run --dashboard`.

The compact app has wake/sleep, opt-in hands-free speech, push-to-talk,
Ollama connection, Whisper setup and approval cards for Windows actions.

### Demonstration-aligned commands

| Say/type after `hi max` | Actual implementation |
|---|---|
| `open premiere` | Starts an installed Adobe Premiere Pro Start Menu shortcut, when found |
| `switch to premiere` | Proposes foreground focus, requiring a PC click to confirm |
| `export video in premiere` | Proposes **Ctrl+M to open Export settings**, not an actual rendered file |
| `play funny cat videos on YouTube` | Opens YouTube search results; selecting and playing a result is manual |
| `find biryani restaurants near me` | Opens a browser search, not a purchase |
| `order biryani to my house` | Explicitly refuses checkout until an authorized ordering integration exists |
| `type into notepad: hello` | Shows a one-time approval card and types only in the confirmed target window |

After saying hi max, you can also say **"Max, add a marker in Premiere"**,
**"Max, cut at playhead in Premiere"**, **"Max, go to beginning of timeline
in Premiere"**, or **"Max, what can you do in Premiere?"**. The exact shortcut
appears in a local one-time approval card. Ctrl+K changes the open sequence
and Ctrl+M only opens export settings; success depends on the active panel
and your Premiere keymap.

See the [OpenJarvis reference comparison](OPENJARVIS-REFERENCE.md) for
local-first architecture patterns and an honest feature gap assessment.

The reel's **actual phone call, complete autonomous Premiere editing/export,
and confirmed online ordering** are not implemented. The existing private
phone-browser companion is **not** a phone call. The assistant cannot claim
or verify completion of unsupported workflows.



MAX PC is a **local Windows desktop UI** that runs on your gaming PC.
It does **not** run inside the Railway Discord container, cannot access your
Windows desktop from Railway, and never starts the microphone without pressing
the TALK button. Discord remains a separate, compatible interface.

## Windows 10/11 setup (PowerShell)

Open PowerShell in your cloned Max repository:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[pc,stt,desktop]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m max pc status --json
.\.venv\Scripts\python.exe -m max pc smoke
.\.venv\Scripts\python.exe -m max pc run
```

Or double-click `scripts\launch-max-pc.bat` after installing.
The desktop now uses **pywebview + Windows WebView2** with a compact voice-first
companion by default. Use `--dashboard` to see the optional cinematic amber
holographic core. Both retain chat, push-to-talk, speech status, and guarded PC control. No external scripts or web-hosted UI are required.
Install/update WebView2 Runtime if the GUI refuses to start; standard Windows
11 installations usually include it. On Windows use **Python 3.12** for
predictable native dependency support.

The legacy Tkinter mock screen is no longer launched by `max pc run`.
The chat composer remains visible at normal laptop resolutions.

**A real LLM is required for intelligent answers.** When no real provider is
configured, the UI displays **MODEL / NOT CONNECTED** rather than silently
returning mock text. Click **CONNECT MODEL → DETECT LOCAL OLLAMA** to connect
to an already installed local Ollama model, without installing or downloading
anything. Alternatively, expand **CONNECT MODEL** and enter a trusted HTTPS
OpenAI-compatible endpoint ending in `/v1`, model ID, and optional API key.
That key is used only for requests to that endpoint; it is never written to
disk by the app. Both quick connections are session-only. That connection is session-only: to persist across restarts set
the same model in `.env`:

```dotenv
MAX_PROVIDER=openai
MAX_BASE_URL=http://127.0.0.1:11434/v1
MAX_MODEL=your-locally-installed-ollama-model
```

Alternatively, configure a hosted OpenAI-compatible model in `.env`:

```dotenv
MAX_PROVIDER=openai
MAX_BASE_URL=https://your-provider.example/v1
MAX_MODEL=your-model-name
MAX_API_KEY_ENV=YOUR_PROVIDER_KEY
```

Set `YOUR_PROVIDER_KEY` as a secret in your local environment; don't commit it.
For fully local inference you can use an OpenAI-compatible local model server
such as Ollama (`http://127.0.0.1:11434/v1`) **for the model only**.
No agent code or credentials must be copied to an unreviewed external server.

## Voice quality and API reliability

The ChatGPT Live conversation interface uses dedicated streaming audio
models rather than MAX's small local Whisper model. The installed PC app
does not yet reproduce full duplex speech, semantic VAD, interruptions
or the exact GPT-Live conversation experience.

The Korean-language transcription bug came from leaving multilingual Whisper
language auto-detection enabled on very short clips. MAX now defaults to
**English-only local transcription** (`MAX_PC_STT_LANGUAGE=en`). To allow
automatic multilingual detection, deliberately set
`MAX_PC_STT_LANGUAGE=auto`.

For better English accuracy without cloud cost, in the same PowerShell
session before launching MAX set:

```powershell
$env:MAX_PC_STT_LANGUAGE = "en"
$env:MAX_STT_MODEL_SIZE = "base.en"
```

Open **SPEECH SETUP** to install that model explicitly. Larger models use
more CPU/memory, and may respond more slowly.

### Optional OpenAI cloud speech recognition

MAX also offers an opt-in recorded-audio route based on `gpt-transcribe`
(official OpenAI /v1/audio/transcriptions). This is **separate from your
free LLM chat API** and may require paid OpenAI audio credits:

```powershell
$env:MAX_PC_STT_BACKEND = "cloud"
$env:OPENAI_API_KEY = "<set-your-own-private-OpenAI-key>"
```

Never commit your key to Git. Audio is sent to OpenAI only if this mode
is explicitly enabled. In default `local` mode, microphone audio is processed
on your PC. The provider is fixed to OpenAI HTTPS; a random free chat API
key does not imply it can transcribe audio.

### Verify your free LLM API

In the default compact MAX window, open **CONNECT AI**, enter your
provider's **exact OpenAI-compatible /v1 base URL**, model ID and API key.
Then click **TEST AI RESPONSE**. It performs one small real network call,
which may consume provider tokens, and reports whether it succeeded or
whether credentials, quota, model name or endpoint were rejected. Simply
configuring a client does not prove it is working. The key is kept in
memory only for the current session. The speech recognizer, text model
and voice output are separate components.

## Fixing Microphone/STT unavailable (SttError)

The Ollama chat model and local speech transcription are **separate**.
Connecting Qwen in Ollama does not install Whisper. When you see a speech
failure, open **SPEECH SETUP** in the desktop app:

1. Check that the faster-whisper backend is installed. If missing, run
   `python -m pip install -e ".[pc,stt,desktop]"` using the same Python
   environment that launches MAX.
2. Click **INSTALL LOCAL WHISPER MODEL** and explicitly accept the model
   download from Hugging Face. The default is multilingual `tiny`.
   The installer caches model weights; speech continues to run locally.
3. Click **TALK** and say "hi max". After a successful transcription,
   **HANDS-FREE** can be enabled separately.

For a one-time environment workaround, run
`$env:MAX_PC_STT_ALLOW_DOWNLOAD = "1"` before launching MAX, which
permits the *first* transcription to fetch the missing model. You can
unset it again when the model is cached.

If installation reports a model-load problem despite the model already
being cached, use `MAX_STT_DEVICE=cpu` and
`MAX_STT_COMPUTE_TYPE=int8`. For Windows microphone failures,
check **Settings → Privacy & security → Microphone**, select a working
Windows input device, and ensure SoundDevice/PortAudio can access it.

Speech model downloads happen only after explicit opt-in; MAX does not
record or send speech audio to Ollama. Speech status is separate from
the AI model status.

## Voice

- Text always works even without audio packages.
- Click **TALK** to capture a bounded 4.5-second local recording.
- Install the optional `pc,stt` extras for `sounddevice`, `pyttsx3` and
  `faster-whisper`. The Windows installed TTS voices are used; a male voice
  (David/Mark or a male-identified voice) is selected where available.
- Faster Whisper never downloads a model automatically at startup.
  Download the model manually, or **explicitly opt in once** in your
  local PowerShell session with `$env:MAX_PC_STT_ALLOW_DOWNLOAD="1"` before
  clicking TALK. Expect the first transcription to load the model.
  After it is downloaded, unset the opt-in for local-only operation.
- If the installed Windows voice does not include a male option, the
  system's available voice is used. No voice is impersonated or cloned.
- Voice is **push-to-talk**, not a passive always-on hotword listener.
  `hi max` and `bye max` work through text and transcribed speech.

## Local commands you can try

| Say/type | What happens |
|---|---|
| `hi max` | MAX wakes up |
| `open notepad`, `open calculator`, `open paint` | Launches a fixed Windows application |
| `open discord`, `open spotify`, `open valorant` | Uses installed app launcher or an official webpage |
| `open https://www.example.com` | Opens a public HTTPS link; no local/private endpoints |
| `search the web for Valorant sensitivity` | Opens a web search in the default browser |
| `volume up`, `volume down`, `mute` | Uses one Windows media key action at a time (requires desktop extra) |
| `what time is it`, `what can you do` | Local clock and command help |
| `play Spotify playlist spotify:playlist:<playlist-id>` | Opens that playlist; **not** automatic playback |
| `remember my Valorant settings: sens 0.4 crouch C walk shift` | Saves a local default profile |
| `remember my Valorant settings for alt: sens 0.35 crouch V` | Saves a second account profile (no credentials) |
| `apply my Valorant settings for alt` | Recalls settings with a **guided apply** reminder |
| `bye max` | MAX stops responding to normal queries |

**Important:** The first release saves and recalls Valorant presets. It does
*not* claim to automatically alter the game's UI, inject into its process,
circumvent Vanguard, or synchronize settings between accounts. Real automatic
application needs on-device UI calibration, a reviewed consent flow and a
manual test with the game open. The original game settings remain untouched.
No usernames, passwords, tokens or session cookies are stored in profiles.

**Spotify:** opening a playlist is not the same as pressing Play. The Spotify
Web API's start/resume playback endpoint requires Spotify Premium, OAuth, and
`user-modify-playback-state`. That is future optional integration, not a
pretend-success in this release.

## Agent-powered PC actions

For arbitrary approved desktop workflows, Max's existing
`MAX_DESKTOP_ENABLE=1` MCP desktop server supports screenshots,
mouse/keyboard and guarded terminal commands on the PC. This is **off by
default** and requires installing `.[desktop]`. State-changing commands
must receive an explicit console approval via:

```powershell
.\.venv\Scripts\python.exe -m max desktop status
.\.venv\Scripts\python.exe -m max desktop approvals
.\.venv\Scripts\python.exe -m max desktop approve <approval-id>
```

Do not enable unrestricted app/system permissions in a public Discord
channel. Keep local and cloud agents separate. This app does not launch
arbitrary program names or execute shell text supplied by chat.

## Jarvis-like automation and phone companion

See [hands-free speech, confirmed Windows actions, and private phone
companion](PC-JARVIS-WORKFLOWS.md). The reference reel demonstrates voice
interaction, cross-app computer tasks, and a phone-call interface. Max now
supports opt-in local hands-free speech, reviewed window typing/shortcuts,
and a private **phone browser** companion via Tailscale Serve. Actual
cellular telephone calls require separate telephony integration.

## Windows test checklist

1. `max pc status --json` reports Windows and optional dependencies.
2. `max pc smoke` passes without touching the desktop.
3. `max pc run` launches the animated orb, and `hi max` activates it.
4. Open Notepad and Calculator; verify ordinary URLs open in the browser.
5. Save `default` and `alt` Valorant profiles; close and reopen MAX;
   verify both can be recalled without any account secrets.
6. Click TALK and verify a real spoken `hi max` transcribes (after
   installing a local Whisper model). Check the installed male voice.
7. Configure a real LLM and test text conversation and the current
   approval-based desktop tool on a *non-sensitive* application.
8. Say `bye max`; confirm further ordinary text is ignored.
9. Verify Discord on Railway is unaffected.

CI validates the cross-platform controller and an additional Windows runner
checks Python import, smoke and profile logic. **Only on your actual PC** can
GUI responsiveness, audio drivers, installed apps, Riot Client and the model's
live performance be proven.

## Third-party agent technology review (not blindly installed)

- [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent):
  MIT; Windows support, skills, memory, MCP/tool gateways. Strong inspiration
  for capability discovery. Rather than replacing Max's core or migrating
  secrets, a separately tested optional adapter can be evaluated later.
- [pywinauto](https://github.com/pywinauto/pywinauto): BSD 3-Clause;
  Windows UI Automation and Win32 control inspection. Recommended for a
  future **calibrated**, foreground-only Valorant settings workflow.
- [Open Interpreter](https://www.openinterpreter.com/docs/desktop):
  reference for workspace-scoped desktop control and review checkpoints,
  not a runtime dependency of MAX PC.
- `faster-whisper`, `sounddevice`, `pyttsx3`: optional local audio stack;
  these are installed only when you request PC voice extras.

**Next phases:** calibrated UI Automation, authenticated Spotify playback,
persistent wake-word audio pipeline with mic privacy indicators, on-device
latency instrumentation, and optional Hermes integration through audited MCP.

## Troubleshooting the desktop GUI

- **MODEL / NOT CONNECTED** means PC commands may work but intelligence is
  not configured. Use Connect Model for installed Ollama or set
  `MAX_PROVIDER`, `MAX_BASE_URL`, `MAX_MODEL` and your API-key environment
  variable in a local `.env`. Restart Max afterward.
- **WebView2 missing**: install Microsoft Edge WebView2 Runtime and rerun
  `python -m max pc run`. Do not use an old installed Max package; reinstall
  using `pip install -e ".[pc,stt,desktop]"`.
- **Controls off-screen**: this release uses a responsive CSS layout with
  a permanently visible composer; shrink the window to test small-screen mode.
- **No audio**: speech packages must be installed and a local Whisper model
  available. Text is independent of audio.
- **Tools cannot control PC**: arbitrary app controls still require the
  pre-existing approved desktop MCP flow (`MAX_DESKTOP_ENABLE=1`).
