# MAX voice architecture

**Status:** Proposed, pending device benchmarks.

Opt-in microphone -> local wake or push-to-talk -> voice activity detection -> speech recognition -> committed transcript -> task coordinator -> streamed response -> speech synthesis. Interim transcript fragments never invoke tools.

Support interruption, cancellation, echo handling, clear mic state, quiet mode and offline/text fallback. Use an original or licensed British-English voice, not an unauthorized imitation.

Track wake false positives, speech latency, first audio latency, interruption time, CPU/GPU cost and privacy. Audio should not be retained by default or silently sent to cloud providers.

Windows and Hyprland require real microphone/speaker tests; iPhone browser restrictions require a foreground interaction proof of concept. Telephony is deferred pending compliance, authentication and pricing review.
