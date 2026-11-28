"""Opt-in Windows local voice profile. Model acquisition is separate from inference."""

from __future__ import annotations

import importlib
import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import redirect_stdout
from pathlib import Path

import httpx

from max_core.assistant_session import default_data_dir
from max_core.voice.desktop_stt import DesktopSpeechToText, pcm16_to_wav
from max_core.voice.desktop_tts import DesktopTextToSpeech
from max_core.voice.pipeline import VoiceError
from max_core.voice.qwen_worker import QwenWorker, read_request

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


# MAX counts prompt size in bytes (1 byte = 1 token, deliberately conservative).
# On Windows the desktop-control tools push the first prompt past 8K, so local
# models need a 16K window. Ollama must be started with the same window
# (OLLAMA_CONTEXT_LENGTH); its OpenAI-compatible endpoint ignores per-request sizes.
LOCAL_CONTEXT_TOKENS = 16_384


def ollama_context_hint(env: Mapping[str, str]) -> str | None:
    """Warn when this shell has no matching Ollama window; Ollama may run elsewhere."""
    raw = env.get("OLLAMA_CONTEXT_LENGTH", "")
    if raw.isdigit() and int(raw) >= LOCAL_CONTEXT_TOKENS:
        return None
    return (
        f"[MAX] Set OLLAMA_CONTEXT_LENGTH={LOCAL_CONTEXT_TOKENS} for Ollama and restart it, "
        "or long requests will be cut off."
    )


def installed_models() -> set[str]:
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        response = client.get(ORIGIN + "/api/tags")
        response.raise_for_status()
        return {item["name"] for item in response.json()["models"]}


def local_settings(models: set[str], *, gaming: bool = False) -> dict[str, str]:
    if CHAT not in models:
        raise ValueError("local_chat_model_missing")
    advanced = REASON if REASON in models and not gaming else CHAT
    return {
        "MAX_TRIAL_DESTINATION": "verified_local",
        "MAX_TRIAL_DIALECT": "openai_chat",
        "MAX_TRIAL_ENDPOINT": ORIGIN + "/v1/chat/completions",
        "MAX_TRIAL_ENDPOINT_LOCAL": "yes",
        "MAX_TRIAL_LOCAL_EGRESS_REVIEWED": "yes",
        "MAX_TRIAL_CLOUD_ALLOWED": "no",
        "MAX_TRIAL_API_KEY": "",
        "MAX_TRIAL_MODEL": CHAT,
        "MAX_TRIAL_CONTEXT_TOKENS": str(LOCAL_CONTEXT_TOKENS),
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
        "MAX_LOCAL_GAMING": "yes" if gaming else "no",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }


def selected_voice_assets() -> tuple[Path, Path, str]:
    """Resolve already-present model weights and a local, authorized voice reference."""
    root = voice_dir()
    explicit_model = os.environ.get("MAX_QWEN_MODEL_DIR")
    if explicit_model:
        model = Path(explicit_model).expanduser()
    elif os.environ.get("MAX_LOCAL_TTS") == "qwen":
        model = Path.home() / "Models" / "Qwen3-TTS-12Hz-1.7B-Base"
    else:
        model = root / "base"
    if not (model / "config.json").is_file():
        raise ValueError("model_missing")

    explicit_reference = os.environ.get("MAX_QWEN_REFERENCE_WAV")
    explicit_text = os.environ.get("MAX_QWEN_REFERENCE_TEXT_FILE")
    if bool(explicit_reference) != bool(explicit_text):
        raise ValueError("reference_pair_required")
    if explicit_reference and explicit_text:
        wav = Path(explicit_reference).expanduser()
        with Path(explicit_text).expanduser().open("rb") as file:
            transcript = file.read(6001).decode("utf-8").strip()
        if not transcript or len(transcript) > 1500:
            raise ValueError("reference_transcript_invalid")
    else:
        wav, transcript = root / "reference.wav", REFERENCE
    if not wav.is_file() or wav.stat().st_size > 32_000_000:
        raise ValueError("reference_missing_or_invalid")
    return model, wav, transcript


def prepare_original_reference(design_dir: Path) -> None:
    """Generate MAX's original voice from locally installed VoiceDesign weights."""
    if not (design_dir / "config.json").is_file():
        raise ValueError("voice_design_missing")
    sf = importlib.import_module("soundfile")
    root = voice_dir()
    root.mkdir(parents=True, exist_ok=True)
    reference = root / "reference.wav"
    if reference.is_file():
        return
    model = _load_qwen(design_dir)
    wavs, rate = model.generate_voice_design(text=REFERENCE, language="English", instruct=DIRECTION)
    temporary = root / "reference.pending.wav"
    sf.write(str(temporary), wavs[0], rate)
    temporary.replace(reference)


def _load_qwen(path: Path):  # type: ignore[no-untyped-def]
    import torch  # type: ignore[import-not-found]
    from qwen_tts import Qwen3TTSModel  # type: ignore[import-not-found]

    torch.set_num_threads(2 if os.environ.get("MAX_LOCAL_GAMING") == "yes" else 6)
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
                        cpu_threads=2 if os.environ.get("MAX_LOCAL_GAMING") == "yes" else 6,
                        local_files_only=True,
                    )
                wav = payload if payload.startswith(b"RIFF") else pcm16_to_wav(payload)
                segments, _ = model.transcribe(
                    io.BytesIO(wav),
                    language="en",
                    beam_size=1 if os.environ.get("MAX_LOCAL_GAMING") == "yes" else 3,
                    vad_filter=True,
                )
                return " ".join(segment.text.strip() for segment in segments).strip()
            except VoiceError:
                raise
            except Exception:
                raise VoiceError("local_stt_failed") from None

    return DesktopSpeechToText(transcribe_fn=transcribe, model="faster-whisper-small")


class LocalNeuralTTS(DesktopTextToSpeech):
    """Prefer the opt-in Qwen worker; keep the existing system voice available."""

    def __init__(self, fallback: DesktopTextToSpeech) -> None:
        super().__init__(headless=True, preference=fallback.preference)
        self._fallback = fallback
        self._headless = fallback.headless
        self._engine_name = "qwen3-tts-local"
        root = voice_dir()
        reference: Path | None = None
        if os.name == "nt":
            try:
                _, reference, _ = selected_voice_assets()
            except (OSError, ValueError):
                pass
        self._qwen_available = reference is not None
        self._available = self._qwen_available or fallback.available
        self._reason = None if self._available else "local_voice_not_prepared"
        self._selected_voice_name = fallback.selected_voice_name
        if self._qwen_available:
            self._selected_voice_name = "Local reference"
            if reference == root / "reference.wav":
                self._selected_voice_name = "MAX original voice v1"
        if not self._qwen_available:
            self._engine_name = fallback.engine_name
        self._worker = QwenWorker()
        self._pending: str | None = None

    def is_speaking(self) -> bool:
        if self._pending is not None:
            state = self._worker.status()
            if state == "busy":
                return True
            pending, self._pending = self._pending, None
            # Never replay a sentence on the system voice after Qwen started audio.
            if state == "failed" and not self._worker.audio_ready:
                return self._fallback.speak(pending)
        return self._fallback.is_speaking()

    def speak(self, text: str) -> bool:
        if not isinstance(text, str) or not text.strip():
            return False
        if self._pending is not None:
            self._worker.cancel()
            self._pending = None
        self._fallback.stop()
        if self._qwen_available and self._worker.start(text):
            self._pending = text
            return True
        return self._fallback.speak(text)

    def stop(self) -> None:
        self._pending = None
        self._worker.cancel()
        self._fallback.stop()


def _render_wav(model: object, text: str, reference: Path, transcript: str) -> bytes:
    sf = importlib.import_module("soundfile")
    wavs, rate = model.generate_voice_clone(  # type: ignore[attr-defined]
        text=text, language="English", ref_audio=str(reference), ref_text=transcript
    )
    buffer = io.BytesIO()
    sf.write(buffer, wavs[0], rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def serve_speech() -> None:
    if os.name != "nt":
        raise RuntimeError("windows_playback_required")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    output = sys.stdout.buffer
    with redirect_stdout(sys.stderr):
        player = importlib.import_module("winsound")
        checkpoint, reference, transcript = selected_voice_assets()
        model = None
        while (text := read_request(sys.stdin.buffer)) is not None:
            try:
                if model is None:
                    model = _load_qwen(checkpoint)
                wav = _render_wav(model, text, reference, transcript)
                output.write(b"MAX_AUDIO_READY\n")
                output.flush()
                player.PlaySound(wav, player.SND_MEMORY | player.SND_NODEFAULT)
            except Exception:
                output.write(b"MAX_FAILED\n")
                output.flush()
                return
            output.write(b"MAX_DONE\n")
            output.flush()


def benchmark() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    checkpoint, reference, transcript = selected_voice_assets()
    start = time.perf_counter()
    model = _load_qwen(checkpoint)
    wav = _render_wav(model, REFERENCE, reference, transcript)
    print(f"First audio ready: {(time.perf_counter() - start) * 1000:.0f} ms ({len(wav)} bytes)")
    for index in range(3):
        start = time.perf_counter()
        _render_wav(model, REFERENCE, reference, transcript)
        print(f"Warm {index + 1}: {(time.perf_counter() - start) * 1000:.0f} ms")


def main() -> int:
    args = sys.argv[1:]
    if args == ["worker"]:
        serve_speech()
        return 0
    if args == ["benchmark-tts"]:
        os.environ.setdefault("MAX_LOCAL_TTS", "qwen")
        try:
            benchmark()
        except Exception:
            print("[MAX] Benchmark failed. Check local Qwen weights and voice reference.")
            return 1
        return 0
    if len(args) == 2 and args[0] == "prepare-reference":
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            prepare_original_reference(Path(args[1]).expanduser())
        except Exception:
            print("[MAX] Could not prepare the voice reference from local VoiceDesign weights.")
            return 1
        print("[MAX] Original voice reference is ready.")
        return 0
    if args == ["launch", "--tts-only"] and os.name == "nt":
        os.environ["MAX_LOCAL_TTS"] = "qwen"
        try:
            selected_voice_assets()
        except (OSError, ValueError):
            print(
                "[MAX] Qwen needs local Base weights and a WAV/transcript. Using the system voice."
            )
            print(
                "[MAX] See docs/LOCAL_VOICE_PREVIEW.md for the default paths and reference setup."
            )
        else:
            print("[MAX] Local Qwen files found. Speech will load the model on the first reply.")
        from max_core.native_desktop.__main__ import main as desktop_main

        sys.argv = [sys.argv[0]]
        return desktop_main()
    if args not in (["launch"], ["launch", "--gaming"]) or os.name != "nt":
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
        settings = local_settings(models, gaming="--gaming" in sys.argv)
        hint = ollama_context_hint(os.environ)
        if hint:
            print(hint)
        os.environ["MAX_LOCAL_GAMING"] = settings["MAX_LOCAL_GAMING"]
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
