# Voice V2 — On-device activation policy

**Status:** new local software policy, **not** a running wake-word listener, streaming
STT/TTS device adapter or Windows/Hyprland microphone service.

Existing V1 already has an injectable `VoiceTurnPipeline`, sentence-chunked
streaming and a verified-AEC barge-in gate. V2 adds `LocalActivationGate`
for a local detector's wake-word/VAD confidence scores or direct push-to-talk.

## Contract

- Voice activation requires owner opt-in, actual OS microphone consent and
  foreground presence. An iPhone PWA uses **push-to-talk only**; no unsupported
  background wake-word promise.
- Wake-word activation requires a trusted *device-local* detector AND sustained
  voice activity. Never send pre-activation audio, detector buffers or private
  text to a remote server.
- Push-to-talk requires a direct user gesture; both modes have short activation
  leases and immediate `revoke()` / `stop_turn()` behavior.
- Device session orchestration **must call `revoke`** on mic-off, permission loss,
  lock screen, game/focus privacy mode, disconnect and background transitions.
  The gate alone cannot observe those OS events.
- This gate never activates tools, authorizes MAX Trust actions or asserts
  end-to-end latency; untrusted transcript text is ordinary user input.

## Future device integration and real-world tests

1. Connect the OS capture controller to V2, with verified foreground, privacy
   consent and verified local inference. Ensure that audio transport is
   unavailable until `streaming_allowed` and microphone capture stops on revoke.
2. Benchmark openWakeWord + Silero VAD separately against actual device audio
   frame formats and sample rates; pin and audit code plus model/voice weights.
   Model assets have independent licenses. Do **not** bundle unlicensed wake
   weights just because the code package is open source.
3. Add STT/TTS adapters (an original/licensed British English voice, local
   Kokoro/Piper or consented API), AEC and hardware playback flush on barge-in.
   The existing V1 cancellation test only verifies Python coroutine cancellation.
4. Measure wake false positives/misses, p50/p95 latency, gameplay overhead,
   privacy revocation and device interruptions on Windows and Hyprland.
   No benchmark results are claimed by this PR.

Rollback: revert this PR. No runtime routes, microphone capture, permission
prompts, dependencies, provider accounts or model downloads are changed.
