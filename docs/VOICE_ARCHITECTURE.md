# MAX voice architecture

**Status:** Proposed, pending device benchmarks.

Opt-in microphone -> local wake or push-to-talk -> voice activity detection -> speech recognition -> committed transcript -> task coordinator -> streamed response -> speech synthesis. Interim transcript fragments never invoke tools.

Support interruption, cancellation, echo handling, clear mic state, quiet mode and offline/text fallback. The owner prefers an original or licensed **female-presenting** British-English voice, with provider and voice ID to be selected later. A gendered assistant name does not dictate speech voice. Never imitate an actor's identifiable voice without permission.

Track wake false positives, speech latency, first audio latency, interruption time, CPU/GPU cost and privacy. Audio should not be retained by default or silently sent to cloud providers.

Windows and Hyprland require real microphone/speaker tests; iPhone browser restrictions require a foreground interaction proof of concept. Telephony is deferred pending compliance, authentication and pricing review.

## Phase 0B evaluated candidates and acceptance notes

Potential local wake-word: [openWakeWord](https://github.com/dscripka/openWakeWord), but its repository distinguishes **Apache-2.0 code** from bundled CC BY-NC-SA pretrained weights. Do not silently use those weights without confirming the license fits MAX. Candidate local STT: [faster-whisper](https://github.com/SYSTRAN/faster-whisper), code MIT; actual quality/latency and speech model licensing require a device benchmark. Candidate local TTS: [Piper](https://github.com/rhasspy/piper), code MIT in cited repo; voice file licensing and lifecycle are separate gates. A custom, original British-English sound must not imitate a real actor without authorization.

**Proposed latency measurements:** wake false activations/hour, wake miss rate on user voice in noise, 50th/95th percentile utterance-end-to-first-audio, interruption-to-silence, CPU/GPU peak while gaming, and percent of sessions requiring fallback. No targets or pass rates asserted without owner device measurements. Disabled mic means no recording, cloud transfer or hidden buffer retention. Consent before audio capture; distinguish live transcript from approved durable memory. Calling via PSTN remains behind [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md).
