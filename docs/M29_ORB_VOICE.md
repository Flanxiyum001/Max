# M29 — Talk to MAX in the orb

**Status:** Implemented; tested offscreen with fake microphone / transcription / speech.
**Not yet tried with a real microphone, real Whisper provider or real Windows voices.**

## How it works

1. Press **Space** (with nothing typed). The orb shows ripple rings: it is
   **listening** to the microphone.
2. Speak, then press **Space** again. The audio is turned into text, shown as
   “…”, and MAX answers using the normal assistant (tools, approvals, memory).
3. MAX **speaks the answer aloud** (voice replies only — typed questions get a
   silent text answer). Press **Esc** to stop him talking.

If transcription happens online, the orb asks **before every recording**:
*“Your voice will be sent to <host> to turn it into text. Enter = allow · Esc = cancel”*,
following the one-consent-per-recording rule from M2–M5 (#57).
`MAX_VOICE_ALWAYS_ALLOW_REMOTE_STT=yes` turns that into a standing permission.

## Setup (`.env`)

Speech-to-text needs any OpenAI-compatible `/v1/audio/transcriptions` service:

```
# Free tier example (check the provider's current docs and limits):
MAX_STT_ENDPOINT=https://api.groq.com/openai/v1/audio/transcriptions
MAX_STT_MODEL=whisper-large-v3
MAX_STT_API_KEY=<your key>

# Or fully local (no upload, no consent prompt) with a local Whisper server
# that speaks the same API, e.g. on port 8000; any non-empty key works:
# MAX_STT_ENDPOINT=http://127.0.0.1:8000/v1/audio/transcriptions
# MAX_STT_MODEL=<model name your server expects>
# MAX_STT_API_KEY=local
```

### Precision recognition (optional)

For a quality-first configuration, select a higher-accuracy model supported by
your own speech service. The example above uses Groq's Whisper Large V3;
the turbo variant remains selectable for speed. OpenAI's
`gpt-4o-transcribe` is another option on the official OpenAI transcription
endpoint (metered API charges). Model choice is explicit: MAX does not switch
providers, buy API usage, or retry against another provider automatically.

```dotenv
MAX_STT_VOCAB=MAX, Aventas, Ryoko, GitHub, Cloudflare, pull request
MAX_STT_AUDIO_MODE=raw
# Optional language hint, not needed for automatic detection:
# MAX_STT_LANGUAGE=en
# Optional web-only setting; preserve literal short replies and punctuation:
# MAX_STT_VERBATIM=yes
```

- Spelling hints are sent to the selected provider with the recording. Use only
  non-sensitive words, not credentials or personal information. They are hints,
  not hard-coded transcript substitutions.
- Raw PCM bypasses the native adaptive noise gate for quiet consonants and
  natural pauses; the default filtered mode remains available for noisy rooms.
  Compare them on your microphone. Pre-encoded WAV is already passed through.
- The native orb and dashboard use the same environment configuration. A custom
  STT endpoint does not inherit a chat provider's key.
- The web verbatim setting disables the existing phrase blacklist and prior
  assistant-answer context. This can preserve genuine short replies but can
  also surface recognizer hallucinations on noise. Leave it off if that occurs.
- A loopback endpoint may be used with a separately installed local Whisper
  service; verify that service does not relay audio remotely. This PR does not
  install a local model or start a server.
- No changes to microphone activation, remote consent or tool approval gates.

**Acceptance:** record the same spoken commands in quiet and noisy rooms,
including mixed-language speech, corrections such as "no, not PR 12, PR 18",
and technical terms. Compare exact words, numbers, names, and time-to-transcript
between models and raw/filtered modes. No real-device accuracy or latency
benchmark is claimed. Continuous streaming, acoustic echo cancellation and
automatic endpointing are outside this increment.

References: [Groq speech-to-text](https://console.groq.com/docs/speech-to-text),
[OpenAI speech-to-text](https://platform.openai.com/docs/guides/speech-to-text).

Without `MAX_STT_*`, MAX falls back to the OpenAI settings from `local_config`
(`OPENAI_API_KEY`, endpoint `api.openai.com`).

Speech output uses the voices installed in Windows (Qt TextToSpeech, WinRT/SAPI);
`MAX_VOICE_LOCALE` and `MAX_VOICE_PRESENTATION` choose the voice.

## Safety

- The microphone only opens on a key press in the focused orb window, and closes
  on the next Space, on Esc during consent, or when the window closes.
- Audio buffers are bounded and cleared after transcription (existing `VoiceController`).
- The transcript is untrusted user text; it goes through the same tool loop and
  approvals as typed text.

## Tests

`tests/test_native_orb.py`: Space → listening → transcript → answer spoken; Esc stops
speech; typed turns are not spoken; remote consent per recording (allow / cancel,
consent used up after one recording); silence and missing voice stack explained.

Rollback: revert this PR.
