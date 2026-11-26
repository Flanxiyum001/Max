# mypy: ignore-errors
"""MAX orb interface: a black window with one living particle orb.

Interaction is deliberately minimal:
  * start typing anywhere -> a faint input line appears; Enter sends it
  * the answer fades in under the orb
  * drag anywhere to move, double-click to toggle fullscreen
  * Esc clears the input / answer, or closes MAX when nothing is shown
  * right-click for Dashboard / Close

The orb only animates "thinking" while a real model request is running.
"""

from __future__ import annotations

import json
import math
import sys
import threading
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import (
    QObject,
    QPointF,
    QRectF,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from max_core.activity import activity_line
from max_core.native_desktop.backend import (
    LocalModelReply,
    NativeDesktopCore,
    NativeDesktopError,
)
from max_core.voice.turn_resume import SpeechCursor, is_resume_request

IDLE, THINKING, SPEAKING, ERROR = "idle", "thinking", "speaking", "error"
LISTENING = "listening"


# Particle-sphere orb, the same animation as the web client (src/max_core/web/static/orb.js).
# Adapted from "Speaking Orb" by aqualang89 (21st.dev), MIT licence.
_ORB_STATES = (LISTENING, THINKING, SPEAKING, ERROR)
_ORB_COLORS = ((255, 185, 105), (178, 151, 255), (255, 150, 214), (255, 90, 90))
_TAU = math.tau
_ORB_N = 1400


def _rnd(v: float) -> float:
    q = math.sin(v * 12.9898) * 43758.5453
    return q - math.floor(q)


def _sphere() -> list[tuple[float, float, float, float, float, float]]:
    jit = (1 - 1000 / _ORB_N) * 1.6 / math.sqrt(_ORB_N)
    pts = []
    for i in range(_ORB_N):
        y = max(-1.0, min(1.0, 1 - 2 * (i + 0.5) / _ORB_N + (_rnd(i + 0.37) - 0.5) * jit))
        a = i * 2.399963229728653 + (_rnd(i) - 0.5) * jit * 6
        s = math.sqrt(1 - y * y)
        pts.append((math.cos(a) * s, y, math.sin(a) * s, a, i / _ORB_N, math.sin(a)))
    return pts


_SPHERE = _sphere()


def _sprite(rgb: tuple[int, int, int], core: float) -> QPixmap:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    g = QRadialGradient(QPointF(32, 32), 32)
    r, gr, b = rgb
    g.setColorAt(0.0, QColor(r, gr, b, 255))
    g.setColorAt(core, QColor(r, gr, b, 255))
    g.setColorAt(core + 0.03, QColor(r, gr, b, 17))
    g.setColorAt(0.94, QColor(r, gr, b, 17))
    g.setColorAt(1.0, QColor(r, gr, b, 0))
    p = QPainter(pix)
    p.setPen(Qt.PenStyle.NoPen)
    p.fillRect(0, 0, 64, 64, g)
    p.end()
    return pix


class OrbView(QWidget):
    """Particle sphere: ripples while listening, knots while thinking, rings on syllables."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(260, 260)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.t = 0.0
        self.state = IDLE
        self.level = 0.0  # 0..1 live audio level, when a caller can measure one
        self.weights = [1.0, 0.0, 0.0, 0.0]
        self.env = 0.0
        self.onsets: list[float] = []
        self._sprite_key: tuple[int, int, int] | None = None
        self._sprite = QPixmap()
        self._spark = _sprite((255, 255, 255), 0.5)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, state: str) -> None:
        self.state = state

    def set_level(self, level: float) -> None:
        self.level = max(0.0, min(1.0, float(level)))

    def _tick(self) -> None:
        dt = 0.016
        self.t += dt
        key = LISTENING if self.state == IDLE else self.state
        k = 1 - math.exp(-dt * 7)
        self.weights = [
            w + ((1.0 if s == key else 0.0) - w) * k
            for w, s in zip(self.weights, _ORB_STATES, strict=True)
        ]
        # Speech envelope: fast attack, soft release. Without a measurable level,
        # follow a syllable-like rhythm (about 5 per second), as the web orb does.
        ph = (self.t * 4.6) % 1
        pulse = 0.55 + 0.45 * math.sin(math.pi * ph) * (0.6 + 0.4 * math.sin(self.t * 1.7))
        synth = pulse * (1.0 if ph < 0.7 else 0.3)
        target = (self.level if self.level > 0.04 else synth) if self.state == SPEAKING else 0.0
        prev = self.env
        self.env += (target - self.env) * (0.55 if target > self.env else 0.18)
        if self.env - prev > 0.06 and self.t - (self.onsets[-1] if self.onsets else -1) > 0.12:
            self.onsets.append(self.t)
        self.onsets = [o for o in self.onsets if self.t - o < 1.2]
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, hh = self.width(), self.height()
        scale = min(w, hh) * 0.86 / 720
        p.translate(w / 2 - 360 * scale, hh / 2 - 360 * scale)
        p.scale(scale, scale)
        h, env, weights = self.t, self.env, self.weights
        col = tuple(
            round(sum(c[i] * wt for c, wt in zip(_ORB_COLORS, weights, strict=True)))
            for i in range(3)
        )
        if col != self._sprite_key:
            self._sprite_key = col
            self._sprite = _sprite(col, 0.2)  # type: ignore[arg-type]
        wl, wt, wsp, we = weights
        ga = 0.075 + 0.05 * env * wsp
        glow = QRadialGradient(QPointF(360, 360), 305 + 25 * env * wsp, QPointF(360, 360))
        glow.setColorAt(0.0, QColor(*col, int(255 * ga)))
        glow.setColorAt(0.57, QColor(*col, int(255 * ga * 0.47)))
        glow.setColorAt(1.0, QColor(*col, 0))
        p.fillRect(0, 0, 720, 720, glow)

        cos, sin = math.cos(h * 0.23), math.sin(h * 0.23)
        mic = self.level if self.state == LISTENING else 0.0
        amp = 8 + mic * 22
        rings = [(-1.15 + (h - o) * 2.1, (1 - (h - o) / 1.2) ** 2) for o in self.onsets]
        calm = wl + we
        sprite, spark = self._sprite, self._spark
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        msin, mcos, mexp = math.sin, math.cos, math.exp
        for x0, y0, z0, a, u, sa in _SPHERE:
            x = y = z = lit = 0.0
            rx, rz = x0 * cos + z0 * sin, z0 * cos - x0 * sin
            if calm > 0.001:
                r = (
                    192 + msin(y0 * 11 - h * 3 + a * 0.03) * amp + msin(a * 0.17 + h * 2) * 5
                ) * calm
                x += rx * r
                y += y0 * r
                z += rz * r  # noqa: E702
            if wt > 0.001:
                b, l2 = u * _TAU * 3 + h * 0.57, u * _TAU * 8 - h * 0.48
                q = 137 + 41 * mcos(l2) + 24 * sa
                ax, ay, d = q * mcos(b), q * msin(b), 65 * msin(l2) + 17 * y0
                x += ax * wt
                y += (d * 0.77 - ay * 0.52) * wt
                z += (d * 0.52 + ay * 0.77) * wt  # noqa: E702
            if wsp > 0.001:
                n = msin(rx * 2.3 + h * 1.4) * msin(y0 * 2.9 - h * 1.1) * mcos(rz * 2.1 + h * 0.8)
                r = 184 + 16 * env + n * (5 + 19 * env) + msin(y0 * 7 - h * 2.2) * 3 * env
                for ry, rg in rings:
                    e = mexp(-(((y0 - ry) / 0.15) ** 2)) * rg
                    r += 7 * e
                    lit += e
                x += rx * r * wsp
                y += y0 * r * wsp
                z += rz * r * wsp
                lit *= wsp  # noqa: E702
            depth, persp = (z + 240) / 480, 850 / (850 - z)
            alpha = min(1.0, 0.22 + 0.72 * depth + 0.35 * env * wsp + 1.2 * lit)
            size = (
                (0.8 + depth * 1.35)
                * (0.75 + 0.25 * msin(a + h * 1.6))
                * (1 + 0.5 * min(1.0, lit))
                * 9
            )
            px, py = 360 + x * persp, 360 + y * persp
            p.setOpacity(alpha)
            p.drawPixmap(
                QRectF(px - size / 2, py - size / 2, size, size), sprite, QRectF(0, 0, 64, 64)
            )
            if z > 75 or lit > 0.25:
                s_a = (max(alpha - 0.5, 0.0) + lit * 0.8) * 0.75
                if s_a > 0:
                    s2 = size * 0.2
                    p.setOpacity(min(1.0, s_a))
                    p.drawPixmap(
                        QRectF(px - s2 / 2, py - s2 / 2, s2, s2), spark, QRectF(0, 0, 64, 64)
                    )
        p.end()


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    step = Signal(str, str)
    approval = Signal(str, object, object)  # tool name, arguments, _Approval
    heard = Signal(str)
    resume = Signal()


class _Approval:
    """Worker thread blocks here until the owner answers in the orb window."""

    def __init__(self, on_answer: Any = None) -> None:
        self.event = threading.Event()
        self.allowed = False
        self.on_answer = on_answer  # main-thread callback (voice consent)


class _Ask(QRunnable):
    """Runs one assistant turn (model + tools) off the GUI thread."""

    def __init__(
        self, window: OrbWindow, prompt: str, transcribe: Any = None, can_resume: bool = False
    ) -> None:
        super().__init__()
        self.window, self.prompt, self.signals = window, prompt, _Signals()
        self.transcribe = transcribe  # voice turn: callable returning text or None
        self.can_resume = can_resume

    def _approve(self, name: str, arguments: dict[str, Any]) -> bool:
        request = _Approval()
        self.signals.approval.emit(name, arguments, request)
        # No answer within 90 s means "no".
        return request.event.wait(90) and request.allowed

    def run(self) -> None:
        try:
            if self.transcribe is not None:
                heard = self.transcribe()
                if not heard:
                    self.signals.failed.emit("voice:" + (self.window.voice_error() or "no_speech"))
                    return
                self.prompt = heard
                self.signals.heard.emit(heard)
                if self.can_resume and is_resume_request(heard):
                    self.signals.resume.emit()
                    return
            session = self.window.ensure_session()
            if session is None:
                self.signals.failed.emit(self.window.session_error or "local_model_not_configured")
                return
            result = session.ask(
                self.prompt,
                approve=self._approve,
                on_step=lambda step: self.signals.step.emit(step.tool_name, step.status),
            )
            self.signals.done.emit(result)
        except NativeDesktopError as exc:
            self.signals.failed.emit(exc.code)
        except Exception:
            self.signals.failed.emit("unavailable")


_VOICE_ERRORS = {
    "stt_not_configured": (
        "Voice isn't set up yet. Add MAX_STT_API_KEY (and MAX_STT_ENDPOINT for a free "
        "Whisper provider) to .env — see docs/M29_ORB_VOICE.md."
    ),
    "microphone_unavailable": "I can't find a microphone.",
    "microphone_start_failed": "I couldn't open the microphone.",
    "empty_audio": "I didn't hear anything.",
    "empty_transcript": "I didn't catch that.",
    "no_speech": "I didn't catch that.",
    "remote_stt_consent_required": "Recording cancelled.",
    "activation_denied": "Microphone isn't allowed right now.",
}


def _voice_message(code: str) -> str:
    return _VOICE_ERRORS.get(code, f"Voice problem: {code}")


_ERRORS = {
    "local_model_not_configured": (
        "No model connected yet. Put your MAX_TRIAL_* settings in a .env file "
        "(see docs/M28_ASSISTANT_TOOLKIT.md)."
    ),
    "local_model_only": "This window only talks to a local model.",
    "magnitude_completion_failed": (
        "Magnitude didn't finish this reply. Check the model in Magnitude, "
        "or rerun start-max.cmd --check-magnitude, then retry."
    ),
    "invalid_model_prompt": "Say that again, a bit shorter.",
}


class OrbWindow(QWidget):
    open_dashboard = Signal()

    def __init__(
        self, core: NativeDesktopCore, session_factory: Any = None, voice_factory: Any = None
    ) -> None:
        super().__init__()
        self.core = core
        self._session_factory = session_factory
        self._session: Any = None
        self._session_lock = threading.Lock()
        self.session_error: str | None = None
        self._pending_approval: _Approval | None = None
        self.setWindowTitle("MAX")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("OrbWindow { background: #000000; border: 1px solid #161616; }")
        self.resize(760, 560)
        self.setMinimumSize(360, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._busy = False
        self._closing = False
        self._close_requested = False
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.timeout.connect(self._idle_after_reply)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 26)
        layout.setSpacing(8)
        self.orb = OrbView()
        layout.addWidget(self.orb, 1)

        # "What MAX is doing" row: terminal icon + plain-English tool activity.
        self.activity = QWidget()
        row = QHBoxLayout(self.activity)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addStretch(1)
        icon = QLabel(">_")
        icon.setFixedSize(24, 24)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFont(QFont("Consolas", 8, QFont.Weight.DemiBold))
        icon.setStyleSheet(
            "color: #9a9a9a; background: transparent; border: 1.5px solid #9a9a9a;"
            " border-radius: 6px;"
        )
        self.activity_text = QLabel("")
        self.activity_text.setFont(QFont("Segoe UI", 12))
        self.activity_text.setStyleSheet("color: #9a9a9a; background: transparent;")
        row.addWidget(icon)
        row.addWidget(self.activity_text)
        row.addStretch(1)
        self.activity.hide()
        layout.addWidget(self.activity)

        self.answer = QLabel("")
        self.answer.setWordWrap(True)
        self.answer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.answer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.answer.setFont(QFont("Segoe UI", 12))
        self.answer.setStyleSheet("color: #bfefff; background: transparent;")
        self.answer.setMaximumHeight(140)
        self._fade = QGraphicsOpacityEffect(self.answer)
        self._fade.setOpacity(0.0)
        self.answer.setGraphicsEffect(self._fade)
        layout.addWidget(self.answer)

        self.input = QLineEdit()
        self.input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.input.setFont(QFont("Segoe UI", 12))
        self.input.setStyleSheet(
            "QLineEdit { color: #7fdfff; background: transparent; border: none;"
            " border-bottom: 1px solid #0f3a48; padding: 6px; }"
        )
        self.input.setMaxLength(2000)
        self.input.returnPressed.connect(self.send)
        self.input.textChanged.connect(lambda text: self.input.setVisible(bool(text)))
        self.input.hide()
        layout.addWidget(self.input)

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(30)
        self._fade_timer.timeout.connect(self._fade_step)
        self._fade_target = 0.0
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(lambda: self._show_answer(None))
        self._voice_factory = voice_factory
        self._voice: Any = None
        self._voice_failed: str | None = None
        self._recorder_proxy: Any = None
        self._voice_turn = False
        self._speech_cursor = SpeechCursor()
        self._speak_poll = QTimer(self)
        self._speak_poll.setInterval(300)
        self._speak_poll.timeout.connect(self._poll_speaking)
        self._reminder_timer = QTimer(self)
        self._reminder_timer.setInterval(20_000)
        self._reminder_timer.timeout.connect(self._check_reminders)
        self._reminder_timer.start()
        QTimer.singleShot(1500, self._check_reminders)

    # --- assistant session --------------------------------------------------
    def ensure_session(self) -> Any:
        """Create the model/tool session on first use (any thread)."""
        with self._session_lock:
            if self._session is None and self.session_error is None:
                try:
                    if self._session_factory is not None:
                        self._session = self._session_factory()
                    else:
                        from max_core.assistant_session import AssistantSession

                        self._session = AssistantSession.from_env(mcp_core=self.core)
                except Exception:
                    self.session_error = "local_model_not_configured"
            return self._session

    def _check_reminders(self) -> None:
        if self._closing or self._busy or self._pending_approval is not None:
            return
        session = self.ensure_session()
        if session is None:
            return
        due = session.due_reminders()
        if due:
            self.orb.set_state(SPEAKING)
            self._state_timer.start(4000)
            self._show_answer("Reminder: " + " · ".join(item["text"] for item in due))
            QApplication.beep()

    def _set_activity(self, text: str | None) -> None:
        if text:
            self.activity_text.setText(text)
            self.activity.show()
        else:
            self.activity.hide()

    def _on_step(self, tool: str, status: str) -> None:
        if self._closing:
            return
        line = activity_line(tool, status)
        if line is not None:
            self._set_activity(line)

    def _on_approval(self, tool: str, arguments: dict, request: _Approval) -> None:
        if self._closing:
            request.event.set()
            return
        self._pending_approval = request
        if tool.startswith("mcp."):
            # Never approve a remote operation based on a truncated orb label.
            # Show the exact proposed arguments in a dedicated modal dialog.
            details = json.dumps(arguments, indent=2, ensure_ascii=True)
            if len(details) > 2_000:
                self._answer_approval(False)
                return
            dialog = QMessageBox(self)
            dialog.setWindowTitle("MAX MCP: approve remote call?")
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setText(f"Allow one remote tool invocation of {tool}?")
            dialog.setInformativeText(
                "The following arguments will be sent to the server. "
                "The result may be shared with the selected AI model.\n\n" + details
            )
            dialog.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            allowed = dialog.exec() == QMessageBox.StandardButton.Yes
            self._answer_approval(allowed)
            return
        detail = ", ".join(f"{k}: {str(v)[:160]}" for k, v in arguments.items())
        self.orb.set_state(SPEAKING)
        self._hide_timer.stop()
        self.answer.setText(f"MAX wants to run {tool}\n{detail}\n\nEnter = allow   ·   Esc = deny")
        self._fade_target = 1.0
        self._fade_timer.start()
        self.setFocus()

    def _answer_approval(self, allowed: bool) -> None:
        request, self._pending_approval = self._pending_approval, None
        if request is None:
            return
        request.allowed = allowed
        request.event.set()
        if request.on_answer is not None:
            request.on_answer(allowed)
            return
        self.orb.set_state(THINKING)
        self._show_answer("⋯ allowed" if allowed else "⋯ denied")

    # --- voice ----------------------------------------------------------------
    def ensure_voice(self) -> Any:
        """Create the voice controller on the GUI thread (microphone is Qt-owned)."""
        if self._voice is None and self._voice_failed is None:
            try:
                if self._voice_factory is not None:
                    self._voice = self._voice_factory()
                else:
                    self._voice = self._default_voice()
            except Exception:
                self._voice_failed = "voice_unavailable"
        return self._voice

    def _default_voice(self) -> Any:
        from max_core.assistant_session import merged_environment
        from max_core.local_config import load_desktop_config
        from max_core.native_desktop.qt_ui import _MainThreadRecorderProxy
        from max_core.voice.controller import VoiceController
        from max_core.voice.desktop_stt import QtMicrophoneRecorder, desktop_stt_from_environment

        env = merged_environment()
        config = load_desktop_config()
        tts = None
        if env.get("MAX_LOCAL_NEURAL_VOICE") == "yes":
            from max_core.voice.local_runtime import LocalNeuralTTS, local_stt

            stt = local_stt()
            tts = LocalNeuralTTS()
        else:
            stt = desktop_stt_from_environment(config, env=env)
        self._recorder_proxy = _MainThreadRecorderProxy(QtMicrophoneRecorder(), parent=self)
        self._always_allow_remote_stt = env.get(
            "MAX_VOICE_ALWAYS_ALLOW_REMOTE_STT", ""
        ).lower() in {"yes", "true", "1"}
        return VoiceController(config=config, stt=stt, tts=tts, recorder=self._recorder_proxy)

    def voice_error(self) -> str | None:
        return None if self._voice is None else self._voice.last_error

    def _is_speaking(self) -> bool:
        return self._voice is not None and bool(self._voice.is_speaking)

    def _speak(self, text: str) -> bool:
        self._speech_cursor.begin(text[:1500])
        return self._play_current_chunk()

    def _play_current_chunk(self) -> bool:
        voice = self._voice
        chunk = self._speech_cursor.current
        if voice is None or not chunk:
            return False
        try:
            return bool(voice.speak_text(chunk))
        except Exception:
            return False

    def _resume_speech(self) -> None:
        self._busy = False
        self._voice_turn = False
        if self._closing or self._close_requested:
            if self._close_requested:
                QTimer.singleShot(0, self.close)
            return
        if self._play_current_chunk():
            self.orb.set_state(SPEAKING)
            self._speak_poll.start()
        else:
            self._idle_after_reply()

    def _poll_speaking(self) -> None:
        if self._closing:
            self._speak_poll.stop()
            return
        if not self._speak_poll.isActive():
            return
        if not self._is_speaking():
            self._speech_cursor.advance()
            if self._play_current_chunk():
                return
            self._speak_poll.stop()
            self._idle_after_reply()

    def toggle_listening(self) -> None:
        """Space: start listening, Space again: stop and answer."""
        if self._closing:
            return
        voice = self.ensure_voice()
        if voice is None:
            self.orb.set_state(ERROR)
            self._state_timer.start(2500)
            self._show_answer(_voice_message(self._voice_failed or "voice_unavailable"))
            return
        if voice.is_recording:
            self._busy = True
            self._voice_turn = True
            self.orb.set_state(THINKING)
            self._show_answer(None)
            self._set_activity("Transcribing")
            self._start_job(
                _Ask(
                    self,
                    "",
                    transcribe=voice.stop_push_to_talk_and_transcribe,
                    can_resume=bool(self._speech_cursor.current),
                )
            )
            return
        if self._busy:
            return
        if self._speak_poll.isActive():
            self._speak_poll.stop()
            if not self._is_speaking():
                # The current sentence finished before the next timer tick.
                self._speech_cursor.advance()
        if self._is_speaking():
            voice.interrupt_speech()
        stt = voice.stt
        if (
            stt.available
            and getattr(stt, "requires_remote_upload", False)
            and not voice.remote_stt_consent
        ):
            if getattr(self, "_always_allow_remote_stt", False):
                voice.grant_remote_stt_consent()
            else:
                host = urlsplit(getattr(stt, "endpoint", "")).hostname or "the speech service"
                self._ask_consent(
                    f"Your voice will be sent to {host} to turn it into text.\n\n"
                    "Enter = allow this recording   ·   Esc = cancel",
                    lambda ok: self._consented(ok),
                )
                return
        self._begin_listening()

    def _ask_consent(self, text: str, callback: Any) -> None:
        self._pending_approval = _Approval(on_answer=callback)
        self.orb.set_state(SPEAKING)
        self._hide_timer.stop()
        self.answer.setText(text)
        self._fade_target = 1.0
        self._fade_timer.start()
        self.setFocus()

    def _consented(self, allowed: bool) -> None:
        if not allowed or self._voice is None:
            self._idle_after_reply()
            self._show_answer("Okay, not recording.")
            return
        self._voice.grant_remote_stt_consent()
        self._begin_listening()

    def _begin_listening(self) -> None:
        voice = self._voice
        if not voice.start_push_to_talk(direct_user_gesture=True):
            self.orb.set_state(ERROR)
            self._state_timer.start(2500)
            self._show_answer(_voice_message(voice.last_error or "voice_unavailable"))
            return
        self.orb.set_state(LISTENING)
        self._show_answer("Listening…  (Space to send)")
        self._hide_timer.stop()

    def _on_heard(self, text: str) -> None:
        if not self._closing:
            if not is_resume_request(text):
                self._speech_cursor.clear()
            self._show_answer(f"“{text[:300]}”")

    # --- conversation -------------------------------------------------------
    def send(self) -> None:
        prompt = self.input.text().strip()
        if not prompt or self._busy or self._closing:
            return
        self._speech_cursor.clear()
        self._speak_poll.stop()
        if self._is_speaking():
            self._voice.interrupt_speech()
        self.input.clear()
        self.setFocus()
        self._busy = True
        self.orb.set_state(THINKING)
        self._show_answer(None)
        self._set_activity("Thinking")
        self._start_job(_Ask(self, prompt))

    def _start_job(self, job: _Ask) -> None:
        job.signals.done.connect(self._done)
        job.signals.failed.connect(self._failed)
        job.signals.step.connect(self._on_step)
        job.signals.approval.connect(self._on_approval)
        job.signals.heard.connect(self._on_heard)
        job.signals.resume.connect(self._resume_speech)
        self._job = job
        self._pool.start(job)

    def _done(self, reply: Any) -> None:
        self._busy = False
        self._set_activity(None)
        if self._close_requested:
            QTimer.singleShot(0, self.close)
            return
        if self._closing:
            return
        self._speech_cursor.clear()
        if not isinstance(reply, LocalModelReply):  # assistant LoopResult
            if reply.stopped_reason == "model_failed":
                if getattr(getattr(self._session, "settings", None), "spec", None) is not None:
                    if self._session.settings.spec.provider_id == "magnitude":
                        self._failed("magnitude_completion_failed")
                        return
                self._failed("unavailable")
                return
            text = reply.answer.strip() or "…"
            self.orb.set_state(SPEAKING)
            self._show_answer(text)
            if self._voice_turn and self._speak(text):
                self._speak_poll.start()
            else:
                self._state_timer.start(2500)
            self._voice_turn = False
            return
        if reply.state == "failed":
            self._failed(reply.error_code or "unavailable")
            return
        if reply.state == "review_required" or reply.proposed_tools:
            # A proposed tool is not an executed action. Do not display a
            # potentially misleading model completion claim as task evidence.
            self.orb.set_state(ERROR)
            self._state_timer.start(2500)
            self._show_answer(
                "The model proposed tool actions. Nothing was executed; review required."
            )
            return
        self.orb.set_state(SPEAKING)
        self._state_timer.start(2500)
        self._show_answer(reply.text.strip() or "…")

    def _failed(self, code: str) -> None:
        self._busy = False
        self._set_activity(None)
        self._voice_turn = False
        if self._close_requested:
            QTimer.singleShot(0, self.close)
            return
        if self._closing:
            return
        self.orb.set_state(ERROR)
        self._state_timer.start(2500)
        if code.startswith("voice:"):
            self._show_answer(_voice_message(code[6:]))
            return
        self._show_answer(_ERRORS.get(code, "I couldn't reach my model just now."))

    def _idle_after_reply(self) -> None:
        if not self._busy and not self._closing:
            self.orb.set_state(IDLE)

    def _show_answer(self, text: str | None) -> None:
        self._hide_timer.stop()  # old answer cannot hide a newer reply
        if text is None:
            self._fade_target = 0.0
        else:
            self.answer.setText(text[:1200])
            self._fade_target = 1.0
            self._hide_timer.start(max(12_000, min(60_000, len(text) * 90)))
        self._fade_timer.start()

    def _fade_step(self) -> None:
        current = self._fade.opacity()
        step = 0.06 if self._fade_target > current else -0.06
        new = min(1.0, max(0.0, current + step))
        self._fade.setOpacity(new)
        if (step > 0 and new >= self._fade_target) or (step < 0 and new <= self._fade_target):
            self._fade_timer.stop()
            if new == 0.0:
                self.answer.setText("")

    # --- input --------------------------------------------------------------
    def keyPressEvent(self, event: Any) -> None:
        if self._pending_approval is not None:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._answer_approval(True)
            elif event.key() == Qt.Key.Key_Escape:
                self._answer_approval(False)
            return  # nothing else is typed while an approval is pending
        if event.key() == Qt.Key.Key_Space and not self.input.text() and not event.isAutoRepeat():
            self.toggle_listening()
            return
        if event.key() == Qt.Key.Key_Escape and (
            self._is_speaking() or self._speak_poll.isActive()
        ):
            if self._is_speaking():
                self._voice.interrupt_speech()
            self._speech_cursor.clear()
            self._speak_poll.stop()
            self._idle_after_reply()
            return
        if event.key() == Qt.Key.Key_Escape:
            if self.input.text():
                self.input.clear()
                self.setFocus()
            elif self.answer.text():
                self._show_answer(None)
            else:
                self.close()
            return
        text = event.text()
        if text and text.isprintable():
            self.input.show()
            self.input.setFocus()
            self.input.insert(text)
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle() is not None:
            self.windowHandle().startSystemMove()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        del event
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def contextMenuEvent(self, event: Any) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #050a0d; color: #9feaff; border: 1px solid #0f3a48; }"
            "QMenu::item:selected { background: #0f3a48; }"
        )
        dash = menu.addAction("Open dashboard")
        reset = menu.addAction("New conversation")
        menu.addSeparator()
        close = menu.addAction("Close MAX")
        chosen = menu.exec(event.globalPos())
        if chosen is dash:
            self.open_dashboard.emit()
        elif chosen is reset and self._session is not None and not self._busy:
            self._session.reset()
            self._show_answer("New conversation.")
        elif chosen is close:
            self.close()

    def closeEvent(self, event: Any) -> None:
        if self._pending_approval is not None:
            self._answer_approval(False)
        if self._busy:
            # The worker may be waiting on a local model for up to 45 seconds.
            # Never freeze the GUI in waitForDone during an in-flight request.
            self._close_requested = True
            event.ignore()
            return
        self._closing = True
        self._state_timer.stop()
        self._hide_timer.stop()
        self._fade_timer.stop()
        self.orb._timer.stop()
        self._reminder_timer.stop()
        self._speak_poll.stop()
        if self._voice is not None:
            try:
                if self._recorder_proxy is not None:
                    self._recorder_proxy.mark_closing()
                self._voice.revoke_microphone()
            except Exception:
                pass
        self._pool.waitForDone()
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self.core.close()
        super().closeEvent(event)


def launch_orb(*, smoke_test: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("MAX")
    try:
        core = NativeDesktopCore.from_env()
    except NativeDesktopError as exc:
        print(f"MAX configuration error: {exc.code}", file=sys.stderr)
        return 2
    window = OrbWindow(core)
    dashboards: list[QWidget] = []

    def open_dashboard() -> None:
        from max_core.native_desktop.qt_ui import MaxWindow

        if not dashboards:
            dashboards.append(MaxWindow(NativeDesktopCore.from_env()))
        dashboards[0].show()
        dashboards[0].raise_()

    window.open_dashboard.connect(open_dashboard)
    window.show()
    window.setFocus()
    if smoke_test:
        app.processEvents()
        healthy = window.isVisible() and window.orb.state == IDLE
        window.close()
        return 0 if healthy else 1
    app.setQuitOnLastWindowClosed(False)
    window.destroyed.connect(app.quit)
    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    return app.exec()


__all__ = ["OrbView", "OrbWindow", "launch_orb"]
