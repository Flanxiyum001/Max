# Local gaming and interruption preview

Run `start-max-local.cmd --gaming` to use the small installed chat model for
all task categories, two CPU threads for the speech models, a smaller Whisper
beam and a below-normal-priority Windows synthesis subprocess. The normal
launcher remains the default. This is a static resource profile, not measured
FPS protection or dynamic GPU scheduling. Ollama can still use the GPU.

The orb now speaks sentence by sentence and remembers the interrupted sentence.
While it speaks, press Space to stop playback and start a recording. Press
Space again to send. Say "continue", "go on", or "resume" to replay the
interrupted sentence and continue the rest. A new question goes through the
normal assistant and replaces the old answer. Esc stops and clears playback.
This is manual, foreground interruption; no open microphone, wake word,
automatic voice-activity detection or verified echo cancellation is enabled.
Per-sentence neural synthesis may add latency; benchmark before enabling this
as an automatic listening experience.

Acceptance: verify sentence order, repeated interruption, new question,
denied microphone permission, close during recording, and that "continue"
does not invoke a model/tool when playback is paused. Compare game FPS,
speech latency, CPU, VRAM and RAM with normal mode. No hardware measurements
are claimed. Revert the PR to restore the previous playback path.
