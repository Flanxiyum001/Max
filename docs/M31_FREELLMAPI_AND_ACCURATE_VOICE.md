# M31 — FreeLLMAPI "auto" model and high-accuracy voice input

**Status:** Implemented; unit/mock tested and driven in headless Chromium with a fake
microphone and fake transcriber. **Not yet verified against a real FreeLLMAPI instance.**

## FreeLLMAPI with `auto`

[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) is self-hosted: it runs on my
machine (default `http://localhost:3001/v1`) and routes `model: "auto"` to whichever
free provider is available. The reply then names the real model (e.g.
`groq/llama-3.3-70b-versatile`), which MAX previously rejected as `model_mismatch`.
Now a configured model of `auto` or `auto:<profile>` (e.g. `auto:fast`) accepts the
routed model; every other model id is still matched strictly.

Settings (Render → Environment, or `.env`):

```
MAX_TRIAL_DESTINATION=cloud          # FreeLLMAPI forwards to cloud providers
MAX_TRIAL_CLOUD_ALLOWED=yes
MAX_TRIAL_ENDPOINT=https://<public address of my FreeLLMAPI>/v1/chat/completions
MAX_TRIAL_MODEL=auto
MAX_TRIAL_API_KEY=freellmapi-...     # unified key from the FreeLLMAPI dashboard
MAX_TRIAL_MAX_COST_USD=0
MAX_TRIAL_INPUT_USD_PER_MILLION=0
MAX_TRIAL_OUTPUT_USD_PER_MILLION=0
```

**Render cannot reach `localhost:3001` on my PC.** The hosted MAX needs FreeLLMAPI on a
public HTTPS address — e.g. a tunnel from my PC (Cloudflare Tunnel / ngrok) or
FreeLLMAPI deployed as its own service. Running MAX locally
(`python -m max_core.web`) can use `http://127.0.0.1:3001/...` directly
(`MAX_TRIAL_ENDPOINT_LOCAL=yes` is not needed for a cloud-routing proxy: keep
`MAX_TRIAL_DESTINATION=cloud`, as decided in ADR-005).

## Accurate voice input

Browser speech recognition is fast but makes many mistakes. When speech-to-text is
configured, the web orb now **records your voice and sends the audio to a
Whisper-class transcription model** (OpenAI-compatible `/v1/audio/transcriptions`),
the same kind of model used by modern voice assistants:

- It **ends by itself** when you stop talking (voice-activity detection: ~1.1 s of quiet),
  gives up after 8 s of silence (6 s for follow-ups), max 25 s. Tap again to send early.
- By default it uses **the same provider and key as chat**: the endpoint is derived from
  `MAX_TRIAL_ENDPOINT` (`…/chat/completions` → `…/audio/transcriptions`), model
  `whisper-large-v3`, with a hint so “MAX” is spelled right.
- Override: `MAX_STT_ENDPOINT`, `MAX_STT_API_KEY`, `MAX_STT_MODEL`, `MAX_STT_LANGUAGE`
  (e.g. `en`); `MAX_STT=off` returns to browser recognition. Only `https://` (or
  loopback) endpoints are accepted.
- The “Hey MAX” wake word still uses the browser recogniser (it must run continuously);
  after “Hey MAX” + pause the question itself is recorded and transcribed accurately.

FreeLLMAPI's README lists `/v1/audio/transcriptions`, but which transcription models
it serves depends on the providers enabled in my instance. If it fails, MAX shows
“I couldn't transcribe that”; set `MAX_STT_*` to another Whisper provider.

Audio goes from the browser to the MAX server and on to the transcription provider; it
is not stored.

Rollback: revert this PR (and set `MAX_TRIAL_MODEL` to a fixed model id).
