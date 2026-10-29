# MAX — Jarvis desktop workflows (reference: short video demo)

The user-supplied reel shows a **real PC assistant** working across programs,
speaking in natural language and receiving phone-assisted commands. This is
not an animated-orb reference. We keep the cinematic UI, but focus on real
local controls and voice behavior, not cosmetic matching.

## Tested Windows workflow added in this PR

```
hi max
open notepad
type into notepad: Hello from Max
  [review target and exact text, then click CONFIRM & EXECUTE]
press ctrl+s in notepad
  [review shortcut, then click CONFIRM & EXECUTE]
switch to vscode
  [review focus change, then click CONFIRM & EXECUTE]
bye max
```

The action parser only accepts a fixed list of applications and keyboard
shortcuts. It does not support arbitrary shell commands, arbitrary window
selection, privilege escalation, game memory modification or unapproved
typing. The app is brought to the foreground only when the exact action is
confirmed in MAX's desktop interface. Confirmations expire and can be used
only once. Moving Max to sleep cancels outstanding plans.

Supported visible app targets: Notepad, VS Code, Chrome, Edge, File Explorer,
Spotify and Discord. App titles vary by installation, so live success depends
on Windows foreground activation and each program being open.
`pyautogui` and `pygetwindow` are provided by the `desktop` optional extra.
Move your mouse to a screen corner to engage PyAutoGUI's FAILSAFE.

## Hands-free mode

The user explicitly clicks **HANDS-FREE OFF** to opt in to microphone
recording. Max then records short 3-second speech chunks and transcribes
locally using the installed faster-whisper model, with no always-on capture at
startup. The red indicator remains visible while recording. Say `hi max`
to wake; further speech is treated as requests until `bye max`. Sensitive
foreground keyboard operations still need an on-screen confirmation; spoken
approval is intentionally not accepted.

Click **HANDS-FREE ON** again to stop. The current recording chunk may
complete before the microphone closes. Closing the Max window also requests
shutdown. The microphone is muted during MAX's own synthesized speech to
reduce feedback. Audio and transcripts are not written to audit logs by this
feature. Voice behavior depends on the microphone driver and available STT
model; CI uses simulated audio only.

For first-use transcription, follow the
[PC Windows guide](PC-WINDOWS.md) to prepare a model. No model download
is performed automatically by switching on hands-free.

## What's *not* the same as the demo yet

- **Phone remote:** not yet available. No network listener or unprotected
  HTTP port is exposed on the gaming PC.
- **Arbitrary natural-language computer use:** Max's AI can reason about
  tasks, but approved foreground Windows actions are currently a limited
  deterministic syntax set, not a general self-driving desktop operator.
- **Full media control:** Spotify playlist links open; authenticated Spotify
  playback is a separate integration.
- **Valorant:** profiles can be stored and recalled, but the protected game
  is not automatically reconfigured.
- **Voice:** short local chunks rather than robust always-on streaming
  wake-word detection. CPU/GPU throughput varies by machine.

## Windows verification

Install with `python -m pip install -e ".[pc,stt,desktop]"`.
Run `python -m max pc run`. Enable hands-free *only after* the local model
is available. Verify the text approval flow in Notepad before testing with
other programs; leave Valorant closed while testing keyboard automation.

Windows CI tests the action parser, TTL approvals, rejected shortcuts, GUI
adapter fakes, speech events and the existing headless PC application. It
does not prove genuine foreground GUI control on the user's PC.
