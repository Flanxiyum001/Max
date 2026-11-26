# Local voice preview (Windows)

This is an experimental, CPU-first, no-paid-API desktop profile. The normal
launcher, existing memory, and Trust policies are unchanged.

## Start

Run `start-max-local.cmd` with Python 3.12 installed and Ollama running on
127.0.0.1:11434. Accept the setup prompt. MAX creates an isolated Python
environment, checks Ollama's installed models, pulls qwen3.5:4b if needed,
and downloads Faster Whisper Small and the official Qwen3-TTS voice models.
Allow substantial free disk space; first setup can be slow. Existing qwen3:8b
is used for coding/reasoning if present; it is not downloaded automatically.
Do not expose the Ollama port or configure its listener as a remote proxy.

The orb uses Space to start and stop a recording. Speech is transcribed
locally on the CPU. The first setup generates an original warm, composed
feminine voice with a light Irish English accent, then saves its reference WAV
in MAX's local data directory, under local-voice-v1. No actor or commercial
assistant recording is used. Future turns use the saved reference and the
0.6B Base model. Delete reference.wav only if you intend to generate a new
voice; keep a backup to retain the same voice across reinstalls.

The profile disables paid chat/STT and routes to a fixed loopback Ollama
endpoint. Web and shell tools are disabled; existing Trust rules still apply.
Model acquisition is the only intended external network activity of the
speech components. Existing approved MCP connections retain their own rules.
The model packages are optional and separately installed. Qwen3-TTS and
Faster Whisper are Apache-2.0 and MIT respectively; review upstream model
and dependency licenses before distribution.

## Use Qwen only for speech output

To preserve your existing chat and microphone setup, run
`start-max-local.cmd --tts-only` instead. It starts the orb without checking
Ollama or downloading any model weights. Initial setup can still install the
optional Python dependencies in the existing isolated environment.

1. Place the official **Qwen3-TTS-12Hz-1.7B-Base** checkpoint under
   `%USERPROFILE%\Models\Qwen3-TTS-12Hz-1.7B-Base`, or set
   `MAX_QWEN_MODEL_DIR` to the folder containing `config.json`.
2. If you previously generated the original MAX voice, it is reused. Otherwise,
   use a local recording you own or have permission to use and its exact text:
   set `MAX_QWEN_REFERENCE_WAV` to the WAV file and
   `MAX_QWEN_REFERENCE_TEXT_FILE` to its UTF-8 transcript. If you already
   have local VoiceDesign weights, you can generate the original with
   `python -m max_core.voice.local_runtime prepare-reference <folder>` from
   the local-voice environment instead.
3. Run the launcher from the same shell so it sees the paths. It reports if the
   required files are missing. No chat routing or remote microphone consent is
   changed. The existing desktop voice handles speech when Qwen is unavailable
   or fails before audio is ready. Use `start-max.cmd` for the standard setup.

The first utterance loads the model; subsequent sentences reuse one local
worker. Interrupting removes that worker and stops playback or synthesis
without waiting on the UI thread. The next utterance starts fresh. The worker
is also removed when the microphone is revoked or the orb closes. Speech input
and synthesized WAV audio are not written to disk by the worker.

## Measure on your Windows PC

After setup, run this in PowerShell from the checkout:

    .\.venv-local-voice\Scripts\python.exe -m max_core.voice.local_runtime benchmark-tts

This reports one cold and three warm audio-ready timings using a fixed phrase
and the generated original voice. It excludes speaker-device latency. To check
playback, use the orb, press Esc during synthesis and during playback, then
type `continue`. No model benchmarks or physical audio verification are
claimed from the software-only checks.

## Limitations / acceptance

Windows hardware acceptance is pending. Verify first launch, restart without
re-downloading, microphone capture, local conversation, voice consistency,
and interruption during synthesis and playback. Inference is CPU-first and
prepares each full sentence before playback; it is not streaming TTS.
No claim of real-time voice or actor likeness. The default launcher remains
the rollback path.
