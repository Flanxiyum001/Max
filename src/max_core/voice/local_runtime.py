"""Opt-in Windows local voice profile. Model acquisition is separate from inference."""

from __future__ import annotations

import importlib
import io
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx

from max_core.assistant_session import default_data_dir
from max_core.voice.desktop_stt import DesktopSpeechToText, pcm16_to_wav
from max_core.voice.desktop_tts import DesktopTextToSpeech
from max_core.voice.pipeline import VoiceError

CHAT = "qwen3.5:4b"
REASON = "qwen3:8b"
ORIGIN = "http://127.0.0.1:11434"
REFERENCE = (
    "Hello. I'm MAX. Ready when you are. "
    "I'll keep you informed, and we'll take care of things one step at a time."
)
DIRECTION = (
    "An original adult feminine voice with a light Irish English accent, "
    "warm mid-low register, calm confidence, clear articulation and relaxed natural rhythm. "
    "Subtle expression and gentle pauses; conversational, never theatrical or robotic."
)
MODEL_IDS = {
    "whisper": "Systran/faster-whisper-small",
    "design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "base": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
}


def voice_dir() -> Path:
    return default_data_dir(os.environ) / "local-voice-v1"


def installed_models() -> set[str]:
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        response = client.get(ORIGIN + "/api/tags")
        response.raise_for_status()
        return {item["name"] for item in response.json()["models"]}


def local_settings(models: set[str]) -> dict[str, str]:
    if CHAT not in models:
        raise ValueError("local_chat_model_missing")
    advanced = REASON if REASON in models else CHAT
    return {
        "MAX_TRIAL_DESTINATION": "verified_local",
        "MAX_TRIAL_DIALECT": "openai_chat",
        "MAX_TRIAL_ENDPOINT": ORIGIN + "/v1/chat/completions",
        "MAX_TRIAL_ENDPOINT_LOCAL": "yes",
        "MAX_TRIAL_LOCAL_EGRESS_REVIEWED": "yes",
        "MAX_TRIAL_CLOUD_ALLOWED": "no",
        "MAX_TRIAL_API_KEY": "",
        "MAX_TRIAL_MODEL": CHAT,
        "MAX_TRIAL_CONTEXT_TOKENS": "4096",
        "MAX_TRIAL_PERSONA": "yes",
        "MAX_TRIAL_MAX_COST_USD": "0",
        "MAX_TRIAL_INPUT_USD_PER_MILLION": "0",
        "MAX_TRIAL_OUTPUT_USD_PER_MILLION": "0",
        "MAX_ROUTING": "on",
        "MAX_ROUTE_QUICK": CHAT,
        "MAX_ROUTE_CHAT": CHAT,
        "MAX_ROUTE_CODE": advanced,
        "MAX_ROUTE_REASONING": advanced,
        "MAX_ENABLE_WEB": "no",
        "MAX_ENABLE_SHELL": "no",
        "MAX_LOCAL_NEURAL_VOICE": "yes",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }


def _load_qwen(path: Path):  # type: ignore[no-untyped-def]
    import torch  # type: ignore[import-not-found]
    from qwen_tts import Qwen3TTSModel  # type: ignore[import-not-found]

    torch.set_num_threads(6)
    return Qwen3TTSModel.from_pretrained(
        str(path), device_map="cpu", dtype=torch.float32, local_files_only=True
    )


def prepare() -> None:
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    sf = importlib.import_module("soundfile")

    root = voice_dir()
    root.mkdir(parents=True, exist_ok=True)
    # Use cached snapshots when available; only the explicit setup command can download.
    for kind, repo in MODEL_IDS.items():
        if kind == "design" and (root / "reference.wav").is_file():
            continue
        target = root / kind
        marker = root / (kind + ".ready")
        if not marker.is_file() or not (target / "config.json").is_file():
            print("[MAX] Preparing " + kind + " model...", flush=True)
            snapshot_download(repo_id=repo, local_dir=str(target))
            marker.touch()
    reference = root / "reference.wav"
    if not reference.is_file():
        print("[MAX] Creating the original voice reference (CPU)...", flush=True)
        model = _load_qwen(root / "design")
        wavs, rate = model.generate_voice_design(
            text=REFERENCE, language="English", instruct=DIRECTION
        )
        temporary = root / "reference.pending.wav"
        sf.write(str(temporary), wavs[0], rate)
        temporary.replace(reference)


def local_stt() -> DesktopSpeechToText:
    model = None
    lock = threading.Lock()

    def transcribe(payload: bytes) -> str:
        nonlocal model
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        with lock:
            try:
                if model is None:
                    model = WhisperModel(
                        str(voice_dir() / "whisper"),
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=6,
                        local_files_only=True,
                    )
                wav = payload if payload.startswith(b"RIFF") else pcm16_to_wav(payload)
                segments, _ = model.transcribe(
                    io.BytesIO(wav), language="en", beam_size=3, vad_filter=True
                )
                return " ".join(segment.text.strip() for segment in segments).strip()
            except VoiceError:
                raise
            except Exception:
                raise VoiceError("local_stt_failed") from None

    return DesktopSpeechToText(transcribe_fn=transcribe, model="faster-whisper-small")


class LocalNeuralTTS(DesktopTextToSpeech):
    """Windows playback in a cancellable child; never blocks on model inference."""

    def __init__(self) -> None:
        super().__init__(headless=True)
        self._headless = False
        self._engine_name = "qwen3-tts-local"
        self._available = os.name == "nt" and (voice_dir() / "reference.wav").is_file()
        self._reason = None if self._available else "local_voice_not_prepared"
        self._selected_voice_name = "MAX original voice v1"
        self._process: subprocess.Popen[bytes] | None = None

    def is_speaking(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def speak(self, text: str) -> bool:
        self.stop()
        if not self.available or not isinstance(text, str) or not text.strip():
            return False
        data = text.strip()[:1500].encode("utf-8")
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "max_core.voice.local_runtime", "speak"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if self._process.stdin is None:
                self.stop()
                return False
            self._process.stdin.write(data)
            self._process.stdin.close()
            return True
        except OSError:
            self.stop()
            return False

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            else:
                process.wait()


def speak_child() -> None:
    sf = importlib.import_module("soundfile")

    if os.name != "nt":
        raise RuntimeError("windows_playback_required")
    winsound = importlib.import_module("winsound")

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    text = sys.stdin.buffer.read(6001).decode("utf-8")
    if not text.strip() or len(text) > 1500:
        return
    root = voice_dir()
    model = _load_qwen(root / "base")
    wavs, rate = model.generate_voice_clone(
        text=text, language="English", ref_audio=str(root / "reference.wav"), ref_text=REFERENCE
    )
    # In-memory WAV: interrupted playback never leaves speech recordings on disk.
    buffer = io.BytesIO()
    sf.write(buffer, wavs[0], rate, format="WAV", subtype="PCM_16")
    winsound.PlaySound(buffer.getvalue(), winsound.SND_MEMORY)


def main() -> int:
    if sys.argv[1:] == ["speak"]:
        speak_child()
        return 0
    if sys.argv[1:] != ["launch"] or os.name != "nt":
        return 2
    try:
        models = installed_models()
        if CHAT not in models:
            print("[MAX] Downloading " + CHAT + "...", flush=True)
            subprocess.run(
                ["ollama", "pull", CHAT],
                env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"},
                check=True,
                timeout=3600,
            )
            models = installed_models()
        settings = local_settings(models)
        prepare()
        os.environ.update(settings)
    except Exception:
        print("[MAX] Local setup failed. Check Ollama, disk space and model downloads.")
        return 1

    from max_core.native_desktop.__main__ import main as desktop_main

    sys.argv = [sys.argv[0]]
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
