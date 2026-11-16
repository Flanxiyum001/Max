# Voice milestone V1 — cancellable streaming turn core

**Status:** software-only groundwork. Not a functional microphone assistant or a latency certification.

## What was implemented

- `VoiceTurnPipeline` connects an injected audio source, streaming STT adapter, a **trusted** factory of existing `GatewayRequest`s, `Gateway.stream()`, punctuation/length-based speech segmentation, injected streaming TTS, and an injected audio output.
- The input is byte-bounded; the recognition result is treated as **untrusted user text** and never used to grant permissions. Only the final committed recognition result reaches Gateway. Inert tool proposals are not voiced or executed.
- Bounded two-chunk queue overlaps model text delivery with speech production while limiting speculative speech backlog. Cancellation interrupts both tasks and invokes the hardware sink's `stop()` to drain queued audio.
- `BargeInGate` consumes VAD probabilities and requires both an active microphone and independently verified echo suppression. It **does not** implement AEC, VAD, or a microphone service.
- The pipeline neither records to disk nor starts listening on import. The host must provide explicit user opt-in.

## Next device-specific gates (not shipped)

1. Implement opt-in OS microphone capture, operating-system audio session controls and WebRTC/system AEC. Validate with speaker output and real microphone, including two voices and gaming conditions. AEC must be verified by the device adapter, never inferred from a model response.
2. Integrate and license-check an actual STT model (`faster-whisper` or a consented remote provider) and an original/licensed British-English voice model (Piper, Kokoro, ElevenLabs or equivalent). Never add model weights or cloud upload by default.
3. Provide a native audio sink that flushes already-buffered playback immediately upon `stop()`. Measure interrupt-to-silence; do not assume Python task cancellation flushes the OS device.
4. Benchmark utterance-end→first-audio, first token→first audio, p50/p95, actual GPU/CPU pressure and VAD false triggers on Windows 11 and Hyprland. `first_audio_ms_after_transcript` is **only** a pipeline-level metric, not end-to-end microphone latency. **600 ms remains a target**, not an achieved result.
5. Load test rapid barge-ins, disconnect/reconnect, rejected gateway requests, model stalls, privacy/price gates and malformed audio. All device sessions should have hard deadlines.

## Safety / rollback

Only new `max_core.voice` code and offline tests are added. There is no API endpoint, no implicit microphone access, no dependency or model download, and no change to `Gateway` routing, secrets or MAX Trust. Rollback by reverting this milestone PR.
