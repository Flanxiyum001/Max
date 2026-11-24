# mypy: ignore-errors
"""MAX orb interface: a black window with one living glass orb.

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
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from max_core.native_desktop.backend import (
    LocalModelReply,
    NativeDesktopCore,
    NativeDesktopError,
)
from max_core.voice.turn_resume import SpeechCursor, is_resume_request

IDLE, THINKING, SPEAKING, ERROR = "idle", "thinking", "speaking", "error"
LISTENING = "listening"


class OrbView(QWidget):
    """Painted glass sphere with drifting inner currents and a cyan rim glow."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(260, 260)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.t = 0.0
        self.state = IDLE
        self.energy = 0.0  # smoothed 0..1, drives speed and brightness
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # bounded redraw rate while MAX is idle
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, state: str) -> None:
        self.state = state

    def _tick(self) -> None:
        target = {IDLE: 0.0, THINKING: 1.0, SPEAKING: 0.6, ERROR: 0.2, LISTENING: 0.75}[self.state]
        self.energy += (target - self.energy) * 0.05
        self.t += 0.016 * (1.0 + 2.2 * self.energy)
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        breathe = 1.0 + 0.012 * math.sin(self.t * 1.3) + 0.02 * self.energy * math.sin(self.t * 6)
        r = min(w, h) * 0.30 * breathe
        c = QPointF(w / 2, h / 2)
        err = self.state == ERROR
        rim = QColor(255, 90, 90) if err else QColor(90, 230, 255)
        p.setPen(Qt.PenStyle.NoPen)

        # 1. Outer glow halo
        glow = QRadialGradient(c, r * 1.28)
        a = int(110 + 90 * self.energy)
        glow.setColorAt(0.74, QColor(rim.red(), rim.green(), rim.blue(), a))
        glow.setColorAt(0.84, QColor(rim.red(), rim.green(), rim.blue(), a // 3))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(glow)
        p.drawEllipse(c, r * 1.28, r * 1.28)

        # 2. Sphere body: black core, deep-blue mid, cyan near the edge
        sphere = QPainterPath()
        sphere.addEllipse(c, r, r)
        body = QRadialGradient(QPointF(c.x() - r * 0.08, c.y() + r * 0.05), r)
        body.setColorAt(0.0, QColor(0, 0, 0))
        body.setColorAt(0.6, QColor(0, 1, 8))
        body.setColorAt(0.84, QColor(3, 14, 90) if not err else QColor(60, 6, 12))
        body.setColorAt(0.95, QColor(30, 150, 220) if not err else QColor(160, 40, 40))
        body.setColorAt(1.0, QColor(150, 245, 255) if not err else QColor(255, 150, 150))
        p.setBrush(body)
        p.drawPath(sphere)

        # 3. Inner currents: translucent blue ribbons rotating inside, clipped to sphere
        p.save()
        p.setClipPath(sphere)
        p.translate(c)
        for i, (speed, hue, alpha, scale) in enumerate(
            (
                (0.21, QColor(20, 50, 255), 150, 1.0),
                (-0.15, QColor(10, 90, 255), 110, 0.9),
                (0.11, QColor(40, 20, 200), 120, 0.8),
                (-0.27, QColor(30, 200, 255), 60, 0.95),
            )
        ):
            if err:
                hue = QColor(200, 30, 50)
            p.save()
            p.rotate(math.degrees(self.t * speed) + i * 70)
            wob = 0.18 * math.sin(self.t * (0.7 + 0.2 * i) + i)
            ribbon = QPainterPath()
            ribbon.moveTo(-r * 0.95, r * (0.10 + wob))
            ribbon.cubicTo(
                -r * 0.35,
                -r * (0.95 - wob) * scale,
                r * 0.35,
                -r * (0.70 + wob) * scale,
                r * 0.95,
                -r * 0.05,
            )
            ribbon.cubicTo(
                r * 0.45,
                -r * (0.25 - wob) * scale,
                -r * 0.25,
                -r * (0.15 + wob) * scale,
                -r * 0.95,
                r * (0.10 + wob),
            )
            grad = QRadialGradient(QPointF(0, -r * 0.4), r * 1.1)
            col = QColor(hue)
            col.setAlpha(int(alpha * (0.75 + 0.35 * self.energy)))
            grad.setColorAt(0.0, col)
            grad.setColorAt(1.0, QColor(hue.red(), hue.green(), hue.blue(), 0))
            p.setBrush(grad)
            p.drawPath(ribbon)
            edge = QPen(QColor(90, 160, 255, int(25 + 70 * self.energy)))
            edge.setWidthF(1.0)
            p.strokePath(ribbon, edge)
            p.restore()
        # dark centre keeps depth
        core = QRadialGradient(QPointF(0, r * 0.05), r * 0.7)
        core.setColorAt(0.0, QColor(0, 0, 0, 235))
        core.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(core)
        p.drawEllipse(QPointF(0, r * 0.05), r * 0.72, r * 0.72)
        p.restore()

        # 4. Glass rim: conical gradient ring, slowly rotating
        cone = QConicalGradient(c, math.degrees(self.t * 0.25))
        stops = (
            (0.0, QColor(230, 255, 255)),
            (0.18, rim),
            (0.42, QColor(60, 200, 170) if not err else rim),
            (0.62, rim),
            (0.82, QColor(170, 250, 255)),
            (1.0, QColor(230, 255, 255)),
        )
        for pos, col in stops:
            cone.setColorAt(pos, col)
        pen = QPen(cone, max(2.0, r * 0.045))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, r * 0.975, r * 0.975)
        thin = QPen(QColor(200, 255, 255, 70), 1.0)
        p.setPen(thin)
        p.drawEllipse(c, r * 0.9, r * 0.9)

        # 4b. Listening: soft rings ripple outward from the rim
        if self.state == LISTENING:
            for k in range(3):
                phase = (self.t * 0.9 + k / 3) % 1.0
                ring = QPen(QColor(150, 240, 255, int(150 * (1 - phase))), 1.6)
                p.setPen(ring)
                p.drawEllipse(c, r * (1.0 + 0.35 * phase), r * (1.0 + 0.35 * phase))

        # 5. Specular highlights: white crescent top-right, cyan sheen bottom
        p.setPen(Qt.PenStyle.NoPen)
        p.save()
        p.setClipPath(sphere)
        spec = QPainterPath()
        spec.addEllipse(c, r * 0.96, r * 0.96)
        cut = QPainterPath()
        cut.addEllipse(QPointF(c.x() - r * 0.09, c.y() + r * 0.09), r * 0.93, r * 0.93)
        crescent = spec.subtracted(cut)
        hl = QRadialGradient(QPointF(c.x() + r * 0.5, c.y() - r * 0.6), r * 0.9)
        hl.setColorAt(0.0, QColor(255, 255, 255, 240))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(hl)
        p.drawPath(crescent)
        sheen = QRadialGradient(QPointF(c.x() - r * 0.15, c.y() + r * 0.95), r * 0.55)
        sheen.setColorAt(0.0, QColor(rim.red(), rim.green(), rim.blue(), 200))
        sheen.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(sheen)
        p.drawEllipse(QPointF(c.x() - r * 0.15, c.y() + r * 0.95), r * 0.6, r * 0.35)
        p.restore()
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

    def _on_step(self, tool: str, status: str) -> None:
        if self._closing:
            return
        if status == "proposed":
            self._show_answer(f"⋯ {tool}")
        elif status in {"denied", "failed"}:
            self._show_answer(f"⋯ {tool} {status}")

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
            self._show_answer("⋯ transcribing")
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
        if self._is_speaking():
            voice.interrupt_speech()
            self._speak_poll.stop()
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
            self._show_answer(f"“{text[:300]}”")

    # --- conversation -------------------------------------------------------
    def send(self) -> None:
        prompt = self.input.text().strip()
        if not prompt or self._busy or self._closing:
            return
        self._speech_cursor.clear()
        if self._is_speaking():
            self._voice.interrupt_speech()
            self._speak_poll.stop()
        self.input.clear()
        self.setFocus()
        self._busy = True
        self.orb.set_state(THINKING)
        self._show_answer(None)
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
        if self._close_requested:
            QTimer.singleShot(0, self.close)
            return
        if self._closing:
            return
        self._speech_cursor.clear()
        if not isinstance(reply, LocalModelReply):  # assistant LoopResult
            if reply.stopped_reason == "model_failed":
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
        if event.key() == Qt.Key.Key_Escape and self._is_speaking():
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
