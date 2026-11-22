"""MAX's own offline voice: a Piper model, downloaded once and run on the server's CPU.

Default voice: en_US-hfc_female-medium (CC BY-NC-SA 4.0, personal non-commercial use).
Override with MAX_PIPER_VOICE (another rhasspy/piper-voices name) or MAX_PIPER_MODEL
(a local .onnx path with its .onnx.json next to it). MAX_PIPER=off disables it.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Any

log = logging.getLogger("max_core.web.piper")

DEFAULT_PIPER_VOICE = "en_US-hfc_female-medium"
_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
_NAME = re.compile(r"^([a-z]{2})_([A-Z]{2})-([a-z0-9_]+)-(x_low|low|medium|high)$")


def voice_urls(name: str) -> tuple[str, str]:
    match = _NAME.match(name)
    if not match:
        raise ValueError("invalid_piper_voice")
    lang, region, speaker, quality = match.groups()
    base = f"{_HF}/{lang}/{lang}_{region}/{speaker}/{quality}/{name}.onnx"
    return base, base + ".json"


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as out:  # noqa: S310
        total = 0
        while chunk := response.read(1 << 16):
            total += len(chunk)
            if total > 200_000_000:
                raise ValueError("voice_too_large")
            out.write(chunk)
    tmp.replace(dest)


class PiperSpeaker:
    """Loads lazily in the background so startup and health checks stay fast."""

    def __init__(self, env: dict[str, str], cache_dir: Path) -> None:
        self.enabled = env.get("MAX_PIPER", "on").strip().lower() not in {"off", "no", "0"}
        self._model = env.get("MAX_PIPER_MODEL", "").strip()
        self._name = env.get("MAX_PIPER_VOICE", DEFAULT_PIPER_VOICE).strip()
        self._cache = cache_dir
        self._length = float(env.get("MAX_PIPER_LENGTH_SCALE", "0.95"))
        self._voice: Any = None
        self._failed = False
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._load, daemon=True).start()
        else:
            self._ready.set()

    def _paths(self) -> tuple[Path, Path]:
        if self._model:
            model = Path(self._model).expanduser()
            return model, Path(str(model) + ".json")
        model_url, config_url = voice_urls(self._name)
        self._cache.mkdir(parents=True, exist_ok=True)
        model = self._cache / f"{self._name}.onnx"
        config = self._cache / f"{self._name}.onnx.json"
        for url, dest in ((config_url, config), (model_url, model)):
            if not dest.is_file():
                log.info("downloading piper voice file %s", dest.name)
                _download(url, dest)
        return model, config

    def _load(self) -> None:
        try:
            from piper import PiperVoice  # type: ignore[import-not-found,unused-ignore]

            model, config = self._paths()
            self._voice = PiperVoice.load(model, config_path=config)
            log.info("piper voice ready: %s", model.name)
        except Exception as exc:
            self._failed = True
            log.warning("piper voice unavailable: %s", type(exc).__name__)
        finally:
            self._ready.set()

    @property
    def ready(self) -> bool:
        return self._voice is not None

    def wait(self, timeout: float) -> bool:
        self._ready.wait(timeout)
        return self.ready

    def synthesize(self, text: str) -> bytes:
        """WAV bytes. Blocking: call from a worker thread."""
        if self._voice is None:
            raise RuntimeError("piper_not_ready")
        from piper import SynthesisConfig  # type: ignore[import-not-found,unused-ignore]

        buf = io.BytesIO()
        with self._lock, wave.open(buf, "wb") as wav:
            self._voice.synthesize_wav(
                text,
                wav,
                syn_config=SynthesisConfig(
                    length_scale=self._length, noise_scale=0.5, noise_w_scale=0.333
                ),
            )
        return buf.getvalue()
