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
MAX_STT_MODEL=whisper-large-v3-turbo
MAX_STT_API_KEY=<your key>

# Or fully local (no upload, no consent prompt) with a local Whisper server
# that speaks the same API, e.g. on port 8000; any non-empty key works:
# MAX_STT_ENDPOINT=http://127.0.0.1:8000/v1/audio/transcriptions
# MAX_STT_MODEL=<model name your server expects>
# MAX_STT_API_KEY=local
```

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
