# M34: More accurate hearing

What changed in how MAX hears you:
- **"Hey MAX, <command>" in one breath** is now transcribed by Whisper on the server (a rolling
  recording runs beside the wake detector). Before, the browser's weaker recogniser handled it.
- **Clean microphone input**: mono, echo cancellation, noise suppression and auto gain, recorded
  at 128 kbps Opus.
- **No more cut-offs**: end-of-speech waits 1.6 s of quiet instead of 1.1 s, and the
  speech/silence thresholds adapt to your room's background noise.
- **Context-aware transcription**: Whisper is told what MAX just said, plus your own vocabulary,
  so names and follow-up answers come out spelled right.
- **No invented phrases**: known Whisper silence artefacts ("Thanks for watching!", "Please subscribe.")
  are dropped.

## Settings
- `MAX_STT_VOCAB`: comma-separated names and terms you use, e.g. `Pourush, Aventas, Railway, FreeLLMAPI`.
- `MAX_STT_LANGUAGE=en`: already set; keeps Whisper from guessing the language.
- `MAX_STT_MODEL`: default `whisper-large-v3`, the most accurate Whisper model (Groq through
  FreeLLMAPI).
