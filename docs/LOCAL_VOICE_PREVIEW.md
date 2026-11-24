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

## Limitations / acceptance

Windows hardware acceptance is pending. Verify first launch, restart without
re-downloading, microphone capture, local conversation, voice consistency,
and interruption during synthesis and playback. The preview starts one
CPU synthesis child per reply; response latency may be high and streaming,
wake word, persistent TTS workers, gaming profiles, and spoken execution
progress are not implemented by this milestone. No claim of real-time voice
or actor likeness. The default launcher remains the rollback path.
