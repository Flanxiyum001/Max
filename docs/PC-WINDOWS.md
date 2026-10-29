# MAX PC — Windows-first Jarvis milestone

MAX PC is a **local Windows desktop UI** that runs on your gaming PC.
It does **not** run inside the Railway Discord container, cannot access your
Windows desktop from Railway, and never starts the microphone without pressing
the TALK button. Discord remains a separate, compatible interface.

## Windows 10/11 setup (PowerShell)

Open PowerShell in your cloned Max repository:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[pc,stt,desktop]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m max pc status --json
.\.venv\Scripts\python.exe -m max pc smoke
.\.venv\Scripts\python.exe -m max pc run
```

Or double-click `scripts\launch-max-pc.bat` after installing.
The window uses Tkinter (bundled with the standard Windows Python installer):
dark theme, animated orb, text input, voice output toggle, and a push-to-talk
button. The sphere pulses while listening/responding.

**A real LLM is required for intelligent answers.** Without a configured
provider, Max uses its deterministic mock provider. In `.env`, configure:

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
