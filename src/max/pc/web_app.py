"""Local WebView2 desktop app and guarded Python bridge.

The HTML is fully self-contained; no CDN, remote scripts or external server.
Bridge methods are exposed through pywebview only in the local native window.
Real AI is never simulated by the mock provider in the PC application.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from ..config import Config
from ..core.factory import build_agent
from .control import PCController, greeting
from .voice import mic_wav, say, transcribe

_OLLAMA = "http://127.0.0.1:11434"


def detect_ollama(*, opener=None) -> list[str]:
    """Query only the explicitly fixed loopback Ollama endpoint."""
    request = urllib.request.Request(
        _OLLAMA + "/api/tags", headers={"Accept": "application/json"}
    )
    with (opener or urllib.request.urlopen)(request, timeout=2.0) as res:
        raw = res.read(256 * 1024)
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("invalid Ollama model list")
    names = [
        item.get("name") for item in data["models"]
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and 1 <= len(item["name"]) <= 128
    ]
    return names[:50]


class PCBridge:
    def __init__(self, controller: PCController, *, config: Config, builder=build_agent,
                 scanner=detect_ollama, speaker=say, recorder=mic_wav, stt=transcribe):
        self.controller = controller
        self.config = config
        self._builder = builder
        self._scanner = scanner
        self._speaker = speaker
        self._recorder = recorder
        self._stt = stt
        self._busy = threading.Lock()
        self._voice_on = True
        self.model_name = ""
        self.model_error = ""
        if config.provider not in ("mock", "test") and config.model and config.base_url:
            try:
                self._activate(config)
            except Exception as exc:
                self.model_error = "Model setup failed: " + type(exc).__name__

    def _activate(self, config: Config) -> None:
        agent = self._builder(config)
        self.controller.ask = lambda text, session: agent.ask(text, session_id=session)
        self.model_name = config.model
        self.model_error = ""

    def state(self) -> dict:
        return {
            "awake": self.controller.awake,
            "ready": bool(self.controller.ask),
            "model": self.model_name or "NO MODEL CONNECTED",
            "model_error": self.model_error,
            "voice": self._voice_on,
            "greeting": greeting(),
            "version": "DESKTOP / V2",
        }

    def submit(self, text: str) -> dict:
        if not isinstance(text, str) or len(text) > 4000:
            return {"text": "Message must be at most 4,000 characters.",
                    "ok": False, "action": "error", "state": self.state()}
        if not self._busy.acquire(blocking=False):
            return {"text": "MAX is processing the previous request.",
                    "ok": False, "action": "busy", "state": self.state()}
        try:
            result = self.controller.handle(text)
            if (
                not self.controller.ask and result.action == "chat"
                and self.controller.awake
            ):
                result = type(result)(
                    "AI model not connected. Click CONNECT MODEL and select an "
                    "installed Ollama model, or configure your .env with a real provider.",
                    "setup", False
                )
            response = {
                "text": result.text, "ok": result.ok,
                "action": result.action, "state": self.state(),
            }
            if self._voice_on and result.action not in {"sleeping", "busy", "setup"}:
                threading.Thread(
                    target=self._speak_safely,
                    args=(result.text,), daemon=True,
                ).start()
            return response
        except Exception as exc:
            return {
                "text": "MAX couldn't finish that request (" + type(exc).__name__ + ").",
                "ok": False, "action": "error", "state": self.state(),
            }
        finally:
            self._busy.release()

    def _speak_safely(self, text: str) -> None:
        try:
            self._speaker(text)
        except Exception:
            pass

    def set_voice(self, enabled: bool) -> dict:
        self._voice_on = enabled is True
        return self.state()

    def record(self) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "text": "MAX is busy."}
        try:
            wav = self._recorder(seconds=4.5)
            text = self._stt(wav)
            return {"ok": bool(text), "text": text or "No speech detected."}
        except Exception as exc:
            return {
                "ok": False,
                "text": "Microphone/STT unavailable (" + type(exc).__name__ +
                        "). Check voice dependencies and local Whisper model.",
            }
        finally:
            self._busy.release()

    def connect_ollama(self, selected: str = "") -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "message": "MAX is busy.", "state": self.state()}
        try:
            try:
                models = self._scanner()
            except (OSError, ValueError, urllib.error.URLError) as exc:
                return {
                    "ok": False, "message": "Ollama isn't reachable at 127.0.0.1:11434 (" +
                        type(exc).__name__ + "). Start Ollama and pull a model first.",
                    "state": self.state(),
                }
            if not models:
                return {"ok": False, "message": "Ollama is running but has no installed models. Run: ollama pull llama3.2",
                        "state": self.state()}
            if selected and selected not in models:
                return {"ok": False, "message": "That model is not installed locally.",
                        "state": self.state(), "models": models}
            model = selected or models[0]
            config = replace(self.config, provider="openai",
                             base_url=_OLLAMA + "/v1", model=model)
            self._activate(config)
            self.config = config
            return {
                "ok": True, "message": "Connected to local Ollama model: " + model +
                    ". This connection lasts until MAX closes; configure .env to persist it.",
                "models": models, "state": self.state(),
            }
        except Exception as exc:
            return {"ok": False, "message": "Model setup failed: " + type(exc).__name__,
                    "state": self.state()}
        finally:
            self._busy.release()


def run(controller: PCController, *, config: Config) -> None:
    """Start the native desktop window. No HTTP listener, no external web assets."""
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "Cinematic desktop requires pywebview. Reinstall with "
            'python -m pip install -e ".[pc]"'
        ) from exc
    html = (Path(__file__).parent / "ui" / "index.html").read_text(encoding="utf-8")
    bridge = PCBridge(controller, config=config)
    webview.create_window(
        "MAX // PERSONAL AI",
        html=html, js_api=bridge,
        width=1180, height=800, min_size=(720, 540),
        background_color="#080504", text_select=True,
    )
    webview.start(debug=False)
