# M33: MAX's own voice (Piper) with backups

Order used for every spoken reply:
1. **Piper**, MAX's own offline voice. It runs on the server's CPU, with no API, key or usage limit.
   Default: `en_US-hfc_female-medium` (Hi-Fi CAPTAIN dataset, CC BY-NC-SA 4.0, so personal,
   non-commercial use only). It downloads once from rhasspy/piper-voices on Hugging Face at startup,
   about 60 MB, into `$MAX_DATA_DIR/voices`.
2. **edge-tts** neural voice (`MAX_WEB_VOICE`), used while Piper is still loading or if it fails.
3. **FreeLLMAPI `/v1/audio/speech`**, the same key as chat (`MAX_TTS_*` to override).
4. **The browser's built-in voice**.

## Settings
- `MAX_PIPER_VOICE=<name>`: any voice from the Piper voice list, e.g. `en_GB-jenny_dioco-medium`.
- `MAX_PIPER_MODEL=/path/voice.onnx`: a local model, with `voice.onnx.json` next to it.
- `MAX_PIPER_LENGTH_SCALE=0.95`: speaking speed; lower is faster.
- `MAX_PIPER=off`: skip Piper.

Measured locally with a medium-size Piper model: about 0.2 s to synthesize one sentence, and
about 190 MB peak memory for the process.
