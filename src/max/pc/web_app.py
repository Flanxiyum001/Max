"""Local WebView2 desktop app and guarded Python bridge.

The HTML is fully self-contained; no CDN, remote scripts or external server.
Bridge methods are exposed through pywebview only in the local native window.
Real AI is never simulated by the mock provider in the PC application.
"""
from __future__ import annotations

import ipaddress
from collections import deque
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Config
from ..core.factory import build_agent
from ..providers.openai_compat import OpenAICompatProvider
from .control import PCController, greeting, _WAKE_RE
from .automation import ApprovalQueue, parse_action
from .voice import mic_wav, say, transcribe, install_speech_model, speech_model_status, speech_failure

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
                 scanner=detect_ollama, speaker=say, recorder=mic_wav, stt=transcribe,
                 speech_installer=install_speech_model, speech_inspector=speech_model_status):
        self.controller = controller
        self.config = config
        self._builder = builder
        self._scanner = scanner
        self._speaker = speaker
        self._recorder = recorder
        self._stt = stt
        self._speech_installer = speech_installer
        self._speech_inspector = speech_inspector
        self._busy = threading.Lock()
        self._voice_on = True
        self._approvals = ApprovalQueue()
        self._listening_stop = threading.Event()
        self._listening_thread = None
        self._events = deque(maxlen=32)
        self._events_lock = threading.Lock()
        self._speaking = threading.Event()
        self._voice_error = ""
        self._remote = None
        self.model_name = ""
        self.model_error = ""
        self._active_provider = None
        if config.provider not in ("mock", "test") and config.model and config.base_url:
            try:
                self._activate(config)
            except Exception as exc:
                self.model_error = "Model setup failed: " + type(exc).__name__

    def _activate(self, config: Config, *, provider=None) -> None:
        agent = (
            self._builder(config, provider=provider)
            if provider is not None else self._builder(config)
        )
        self.controller.ask = lambda text, session: agent.ask(text, session_id=session)
        self._active_provider = provider or getattr(agent, "provider", None)
        self.model_name = config.model
        self.model_error = ""

    def state(self) -> dict:
        return {
            "awake": self.controller.awake,
            "ready": bool(self.controller.ask),
            "model": self.model_name or "NO MODEL CONNECTED",
            "model_verified": False,  # Never infer live connectivity from constructing a client
            "model_error": self.model_error,
            "voice": self._voice_on,
            "handsfree": self._listening_active(),
            "voice_error": self._voice_error,
            "speech": self._speech_inspector(),
            "remote": bool(self._remote and self._remote.active),
            "greeting": greeting(),
            "version": "DESKTOP / V2",
        }

    def test_ai_connection(self) -> dict:
        """Make ONE explicit, billable test request when user presses TEST AI."""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "message": "MAX is busy. Try again."}
        try:
            provider = self._active_provider
            if provider is None:
                return {"ok": False, "message": "No testable provider configured. Connect a model first."}
            try:
                response = provider.complete([
                    {"role": "user", "content": "Reply with the single word OK."}
                ], tools=None)
            except Exception as exc:
                from ..providers.base import ProviderAuthError, ProviderRateLimit, ProviderTimeout, ProviderError
                if isinstance(exc, ProviderAuthError):
                    desc = "Authentication failed (401/403). Check the API key and account permissions."
                elif isinstance(exc, ProviderRateLimit):
                    desc = "Rate limit or credits exhausted (429). Check your provider quota."
                elif isinstance(exc, ProviderTimeout):
                    desc = "Request timed out. Check provider URL/network or try a smaller model."
                elif isinstance(exc, ProviderError):
                    code = getattr(exc, "code", "") or "provider_error"
                    if code in {"http_404", "http_400", "http_422"}:
                        desc = "Provider rejected the model or endpoint (" + code + "). Verify exact /v1 URL and model ID."
                    else:
                        desc = "Provider request failed (" + str(code)[:40] + "). Check service availability."
                else:
                    desc = "Model test failed (" + type(exc).__name__ + ")."
                return {"ok": False, "message": desc}
            if not str(getattr(response, "text", "")).strip():
                return {"ok": False, "message": "Provider returned an empty response. Check model support and quota."}
            return {"ok": True, "message": "AI test succeeded: provider returned actual text from " + self.model_name + "."}
        finally:
            self._busy.release()

    def install_voice_model(self) -> dict:
        """Explicit one-click local model download; never triggered by listening."""
        if self._listening_active():
            return {
                "ok": False, "text": "Turn HANDS-FREE OFF before installing speech.",
                "state": self.state(),
            }
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "text": "MAX is busy. Retry installation after the current request.",
                    "state": self.state()}
        try:
            message = self._speech_installer()
            self._voice_error = ""
            return {"ok": True, "text": message, "state": self.state()}
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "isn't installed" in str(exc):
                detail = "Faster Whisper isn't installed. Reinstall MAX using .[pc,stt,desktop]."
            elif isinstance(exc, ValueError):
                detail = "Invalid speech model setting. Use MAX_STT_MODEL_SIZE=tiny."
            else:
                detail = "Speech model installation failed (" + type(exc).__name__ + "). Check internet access, Hugging Face availability and disk space."
            self._voice_error = detail
            return {"ok": False, "text": detail, "state": self.state()}
        finally:
            self._busy.release()

    def _listening_active(self) -> bool:
        return bool(self._listening_thread and self._listening_thread.is_alive()
                    and not self._listening_stop.is_set())

    def _emit(self, event: dict) -> None:
        with self._events_lock:
            self._events.append(event)

    def submit_remote(self, text: str) -> dict:
        # Remote commands travel through exactly the same wake/permission
        # checks; approvals can only be clicked in the native PC window.
        reply = self.submit(text)
        if reply.get("action") != "busy":
            self._emit({"type": "remote", "heard": text, "response": reply})
        return reply

    def remote_start(self) -> dict:
        from .remote import PhoneCompanion
        if self._remote and self._remote.active:
            return {"ok": False, "message": "Phone Companion is already running."}
        companion = PhoneCompanion(self)
        try:
            result = companion.start()
        except OSError as exc:
            return {"ok": False, "message": "Could not bind the local port (" +
                    type(exc).__name__ + "). Nothing is exposed."}
        self._remote = companion
        result["state"] = self.state()
        return result

    def remote_stop(self) -> dict:
        if self._remote:
            self._remote.stop()
            self._remote = None
        return {"ok": True, "message": "Phone Companion stopped. Pairing token revoked.",
                "state": self.state()}

    def poll_events(self) -> list[dict]:
        """The local UI polls events without exposing network/remote listeners."""
        with self._events_lock:
            events = list(self._events)
            self._events.clear()
        return events

    def set_handsfree(self, enabled: bool) -> dict:
        """User-initiated continuous short clips, never enabled at startup."""
        if enabled is not True:
            self._listening_stop.set()
            return self.state()
        if self._listening_active():
            return self.state()
        self._listening_stop.clear()
        self._voice_error = ""
        self._listening_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="max-local-microphone"
        )
        self._listening_thread.start()
        return self.state()

    def _listen_loop(self) -> None:
        while not self._listening_stop.is_set():
            if self._speaking.is_set():
                self._listening_stop.wait(.15)
                continue
            try:
                wav = self._recorder(seconds=3.0)
                if self._listening_stop.is_set():
                    break
                heard = self._stt(wav).strip()
            except Exception as exc:
                self._voice_error = speech_failure(exc)
                self._emit({"type": "error", "text": self._voice_error})
                self._listening_stop.set()
                break
            if not heard or len(heard) > 4000:
                continue
            # Ignore other conversations until an explicit wake phrase.
            if not self.controller.awake and not _WAKE_RE.fullmatch(heard):
                continue
            reply = self.submit(heard)
            if reply.get("action") == "busy":
                continue
            self._emit({"type": "speech", "heard": heard, "response": reply})
        self._emit({"type": "microphone_off"})

    def submit(self, text: str) -> dict:
        if not isinstance(text, str) or len(text) > 4000:
            return {"text": "Message must be at most 4,000 characters.",
                    "ok": False, "action": "error", "state": self.state()}
        if not self._busy.acquire(blocking=False):
            return {"text": "MAX is processing the previous request.",
                    "ok": False, "action": "busy", "state": self.state()}
        try:
            stripped = text.strip()
            # A model response cannot authorize PC automation. Only explicit
            # user text or transcription generates a pending local approval.
            plan = parse_action(stripped) if self.controller.awake else None
            if plan is not None:
                pending = self._approvals.plan(plan)
                return {
                    "text": "Approval required before Windows control. Review the exact target and action.",
                    "ok": True, "action": "approval",
                    "approval": pending, "state": self.state(),
                }
            result = self.controller.handle(text)
            if result.action == "sleep":
                self._approvals.cancel()
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
            if self._voice_on and result.action not in {"sleeping", "busy", "setup", "approval"}:
                # Block microphone capture before the speech thread starts,
                # avoiding MAX transcribing its own synthesized reply.
                self._speaking.set()
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
        self._speaking.set()
        try:
            self._speaker(text)
        except Exception:
            pass
        finally:
            self._speaking.clear()

    def approve_action(self, token: str) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "text": "MAX is busy; approval not executed."}
        try:
            ok, message = self._approvals.approve(
                token, awake=self.controller.awake
            )
            return {"ok": ok, "text": message, "state": self.state()}
        finally:
            self._busy.release()

    def deny_action(self) -> dict:
        self._approvals.cancel()
        return {"ok": True, "text": "Cancelled. No PC actions executed.", "state": self.state()}

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
                "text": speech_failure(exc),
            }
        finally:
            self._busy.release()

    def connect_provider(self, base_url: str, model: str, api_key: str = "") -> dict:
        """Connect an explicitly supplied model in memory; no secret persistence.

        HTTPS is required for external providers; only 127.0.0.1 can use
        HTTP. Never logs the key, and never guesses a provider endpoint.
        """
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "message": "MAX is busy.", "state": self.state()}
        try:
            url = base_url.strip().rstrip("/") if isinstance(base_url, str) else ""
            model = model.strip() if isinstance(model, str) else ""
            if (
                len(url) > 250 or len(model) > 128 or not model
                or not isinstance(api_key, str) or len(api_key) > 4096
            ):
                return {"ok": False, "message": "Enter a valid endpoint and model (limited length)."}
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower()
                local = parsed.scheme == "http" and host == "127.0.0.1"
                remote = parsed.scheme == "https" and host and host not in {
                    "localhost", "127.0.0.1", "0.0.0.0",
                }
                if not (local or remote) or parsed.username or parsed.password or parsed.fragment or parsed.query:
                    raise ValueError("unsafe endpoint")
                if remote:
                    try:
                        ip = ipaddress.ip_address(host)
                        if not ip.is_global:
                            raise ValueError("private HTTPS endpoint")
                    except ValueError as exc:
                        if str(exc) == "private HTTPS endpoint":
                            raise
                if not parsed.path.endswith("/v1"):
                    raise ValueError("API base URL must end in /v1")
            except (ValueError, TypeError):
                return {"ok": False, "message": "Use a HTTPS OpenAI-compatible /v1 URL, or http://127.0.0.1:11434/v1 for Ollama."}
            config = replace(self.config, provider="openai", base_url=url, model=model)
            provider = OpenAICompatProvider(
                base_url=url, model=model, api_key=api_key or None,
                timeout=config.timeout_s, retries=config.retries,
            )
            self._activate(config, provider=provider)
            self.config = config
            return {
                "ok": True,
                "message": "Provider configured: " + model +
                    ". Press TEST AI to verify a real response. The key stays in memory for this session.",
                "state": self.state(),
            }
        except Exception as exc:
            return {"ok": False, "message": "Model setup failed: " + type(exc).__name__,
                    "state": self.state()}
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
                "ok": True, "message": "Ollama model configured: " + model +
                    ". Press TEST AI to verify that it actually responds. This connection lasts until MAX closes.",
                "models": models, "state": self.state(),
            }
        except Exception as exc:
            return {"ok": False, "message": "Model setup failed: " + type(exc).__name__,
                    "state": self.state()}
        finally:
            self._busy.release()


def run(controller: PCController, *, config: Config, dashboard: bool = False) -> None:
    """Run a compact voice-first companion; legacy holographic UI is optional.

    The video reference shows normal Windows apps, not a large Jarvis UI.
    Launch compact by default and let users explicitly select --dashboard.
    """
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "Cinematic desktop requires pywebview. Reinstall with "
            'python -m pip install -e ".[pc]"'
        ) from exc
    ui_file = "index.html" if dashboard else "companion.html"
    html = (Path(__file__).parent / "ui" / ui_file).read_text(encoding="utf-8")
    bridge = PCBridge(controller, config=config)
    if dashboard:
        webview.create_window(
            "MAX // PERSONAL AI", html=html, js_api=bridge,
            width=1180, height=800, min_size=(720, 540),
            background_color="#080504", text_select=True,
        )
    else:
        webview.create_window(
            "MAX // Voice Companion", html=html, js_api=bridge,
            width=500, height=430, min_size=(390, 330),
            background_color="#101216", text_select=True,
            on_top=False,
        )
    try:
        webview.start(debug=False)
    finally:
        bridge.set_handsfree(False)
        bridge.remote_stop()
        bridge.deny_action()
