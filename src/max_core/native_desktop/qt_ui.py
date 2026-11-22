# mypy: ignore-errors
"""Original MAX Qt Widgets desktop application.

PySide6 is optional. This module is smoke-tested in a dedicated Qt CI job and
never imported by the regular headless MAX Core/server process.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRectF,
    QRunnable,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from max_core.assistant_chat import AssistantChatService, ChatTurnResult, ToolExecutionSummary
from max_core.gateway.adapters import _chunked_scan_messages, _walk_strings
from max_core.gateway.privacy_guard import contains_high_confidence_secret
from max_core.local_config import DesktopConfig
from max_core.local_tools import LocalToolExecutor, LocalToolRegistry
from max_core.native_desktop.backend import (
    ApprovalResolution,
    DesktopSnapshot,
    NativeDesktopCore,
    NativeDesktopError,
)
from max_core.native_desktop.title_bar import AccentStrip, IslandState, IslandTitleBar
from max_core.presence.notifications import FocusState
from max_core.presence.orb import OrbReducer, OrbState, OrbView, OrbVisibility
from max_core.voice.controller import VoiceCapabilities, VoiceController
from max_core.voice.desktop_stt import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE, QtMicrophoneRecorder

STYLE = """
QWidget { color: #e6f4f9; font-family: "Segoe UI", "Inter", sans-serif; font-size: 12px; }
QWidget#root { background: #060911; }
QFrame#sidebar { background: #090e18; border-right: 1px solid #162438; }
QFrame#header { background: #080c15; border-bottom: 1px solid #152336; }
QFrame#hero {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #081322, stop:1 #0b1c2e);
    border: 1px solid #134e6f;
    border-radius: 20px;
}
QFrame#panel, QFrame#stat, QFrame#commandPanel {
    background: #0c1320; border: 1px solid #182b42; border-radius: 14px;
}
QFrame#warning { background: #0d1c29; border: 1px solid #1b5e7a; border-radius: 12px; }
QLabel#brand { color: #e0fbff; font-size: 21px; font-weight: 850; letter-spacing: 4px; }
QLabel#eyebrow { color: #38e2f7; font-size: 10px; font-weight: 700; }
QLabel#heading { color: #f0fcff; font-size: 28px; font-weight: 700; }
QLabel#heroTitle { color: #e8fcff; font-size: 23px; font-weight: 700; }
QLabel#subtle { color: #94b3c8; font-size: 12px; }
QLabel#statValue { color: #7ef9ff; font-size: 22px; font-weight: 700; }
QLabel#sectionTitle { font-size: 17px; font-weight: 700; color: #dcf9ff; }
QLabel#status { color: #38e2f7; font-size: 11px; font-weight: 600; }
QLabel#sidebarNote { color: #83a0b5; font-size: 11px; }
QPushButton { border-radius: 10px; padding: 10px 16px; font-weight: 650; }
QPushButton#nav { text-align: left; border: 1px solid transparent;
                  background: transparent; color: #8da9be; }
QPushButton#nav:hover { background: #102033; color: #e0fbff; }
QPushButton#nav[active="true"] { background: #102d45; color: #7ef9ff;
                                 border: 1px solid #1c6b8c; }
QPushButton#primary { background: #22d3ee; color: #04151f; border: none; }
QPushButton#primary:hover { background: #5be7fa; }
QPushButton#secondary { background: #101e30; border: 1px solid #204564; color: #d8f5ff; }
QPushButton#secondary:hover { background: #162a42; }
QPushButton:disabled { color: #526a7d; background: #0e1622; border-color: #162436; }
QPlainTextEdit, QListWidget { background: #070d17; border: 1px solid #1d354f;
                               border-radius: 10px; padding: 10px; selection-background-color: #154e6b; }
QPlainTextEdit:focus, QListWidget:focus { border: 1px solid #22d3ee; }
QListWidget::item { padding: 10px; border-bottom: 1px solid #132336; }
QListWidget::item:selected { background: #133852; color: #ffffff; border-radius: 8px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: #09101a; width: 9px; }
QScrollBar::handle:vertical { background: #1f4563; border-radius: 4px; min-height: 20px; }
"""


def _label(text: str, style: str = "", *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    if style:
        label.setObjectName(style)
    label.setWordWrap(wrap)
    return label


def _button(text: str, style: str = "secondary") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(style)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _panel() -> tuple[QFrame, QVBoxLayout]:
    panel = QFrame()
    panel.setObjectName("panel")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(10)
    return panel, layout


def _icon() -> QIcon:
    image = QPixmap(64, 64)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#06b6d4"))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 16, 16)
    painter.setPen(QColor("#ecfeff"))
    font = QFont("Segoe UI", 29)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "M")
    painter.end()
    return QIcon(image)


def _isolated_fallback_config() -> DesktopConfig:
    cwd = Path.cwd().resolve()
    return DesktopConfig(
        database_url="",
        is_sqlite_local=False,
        workspace_root=cwd,
        notes_dir=(cwd / "notes").resolve(),
        active_provider=None,
        openai=None,
        anthropic=None,
    )


def _format_provider_badge(status: Any) -> str:
    if getattr(status, "configured", False) and getattr(status, "provider", None):
        model = getattr(status, "model_id", None) or "default"
        return f"Provider: {status.provider} / {model}"
    return "Provider: not configured (set API key in .env)"


def _format_voice_status(caps: VoiceCapabilities) -> str:
    if caps.headless:
        return "Voice: headless/offscreen (hardware fallback)"
    tts_part = f"TTS {caps.tts_engine}" if caps.tts_available else "TTS unavailable"
    mic_part = "Mic ready" if caps.mic_available else "Mic unavailable"
    stt_part = "STT ready" if caps.stt_available else "STT needs OPENAI_API_KEY"
    return f"Voice: {tts_part}  •  {mic_part}  •  {stt_part}"


_REDACTED_USER_INPUT_LINE = "You: [REDACTED — sensitive credential omitted]"
_REDACTED_TOOL_OUTPUT = "[REDACTED — contains high-confidence credential]"


def _contains_secret_value(value: object) -> bool:
    texts: list[str] = []
    _walk_strings(value, texts)
    if not isinstance(value, str):
        try:
            texts.append(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
        except (TypeError, ValueError, RecursionError):
            try:
                text_repr = str(value)
            except (TypeError, ValueError, RecursionError):
                text_repr = ""
            if text_repr:
                texts.append(text_repr)
    if not texts:
        return False
    return contains_high_confidence_secret(_chunked_scan_messages(texts))


def _format_tool_output_preview(output: object) -> str:
    if _contains_secret_value(output):
        return _REDACTED_TOOL_OUTPUT
    if isinstance(output, str):
        text = output
    else:
        try:
            text = json.dumps(output, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(output)
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= 180 else cleaned[:177] + "..."


class Orb(QWidget):
    """Cyan holographic presence orb driven by ``OrbReducer`` state."""

    _STATE_LABELS: dict[OrbState, str] = {
        OrbState.IDLE: "MAX / IDLE",
        OrbState.LISTENING: "MAX / LISTENING",
        OrbState.THINKING: "MAX / THINKING",
        OrbState.SPEAKING: "MAX / SPEAKING",
        OrbState.AWAITING_APPROVAL: "MAX / APPROVAL",
    }

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(225, 235)
        self.phase = 0.0
        self.quiet = False
        self.orb_state = OrbState.IDLE
        self.orb_visibility = OrbVisibility.PASSIVE
        self.primary_ring_hex = "#00f0ff"
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_quiet(self, quiet: bool) -> None:
        self.quiet = quiet
        if quiet:
            self._timer.stop()
        else:
            self._timer.start()
        self.update()

    def set_orb_view(self, view: OrbView) -> None:
        self.orb_state = view.state
        self.orb_visibility = view.visibility
        self.update()

    def _tick(self) -> None:
        step = 0.7
        if self.orb_state == OrbState.LISTENING:
            step = 1.6
        elif self.orb_state == OrbState.THINKING:
            step = 2.2
        elif self.orb_state == OrbState.SPEAKING:
            step = 1.9
        self.phase = (self.phase + step) % 360
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        if self.orb_visibility == OrbVisibility.HIDDEN:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = self.width() / 2
        cy = self.height() / 2 - 5
        radius = min(self.width(), self.height()) * 0.31

        # Palette calibrated to IDENTITY.md: dark nearly-black centre, bright cyan/turquoise
        # outer ring, electric-blue internal reflections, subtle green highlights.
        active = not self.quiet and self.orb_visibility == OrbVisibility.ACTIVE
        outer_cyan = "#00f0ff" if active else "#00c8d9"
        electric_blue = "#0088ff" if active else "#0066cc"
        green_highlight = "#16f2b3"
        if self.orb_state == OrbState.LISTENING and active:
            outer_cyan = "#26fff0"
        elif self.orb_state == OrbState.SPEAKING and active:
            outer_cyan = "#38bdf8"
        elif self.orb_state == OrbState.AWAITING_APPROVAL and active:
            outer_cyan = "#22d3ee"
            green_highlight = "#34d399"
        self.primary_ring_hex = outer_cyan

        painter.setPen(Qt.PenStyle.NoPen)
        glow = QRadialGradient(QPointF(cx, cy), radius * 1.95)
        glow.setColorAt(0, QColor(0, 224, 255, 115 if not self.quiet else 32))
        glow.setColorAt(0.55, QColor(0, 128, 255, 52 if not self.quiet else 15))
        glow.setColorAt(1, QColor(2, 12, 24, 0))
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(cx, cy), radius * 1.95, radius * 1.95)

        for tilt, shade, width in (
            (-30, outer_cyan, 2.0),
            (34, electric_blue, 1.8),
            (2, green_highlight, 1.15),
        ):
            painter.save()
            painter.translate(cx, cy)
            direction = 0.16 if tilt < 0 else (-0.12 if tilt > 10 else 0.08)
            painter.rotate(tilt + (self.phase * direction))
            pen = QPen(QColor(shade))
            pen.setWidthF(width)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(
                QRectF(-radius * 1.56, -radius * 0.56, radius * 3.12, radius * 1.12)
            )
            painter.restore()

        # Dark nearly-black centre with electric-blue internal gradient and cyan rim.
        sphere = QRadialGradient(QPointF(cx, cy), radius)
        sphere.setColorAt(0.0, QColor("#02060d"))
        sphere.setColorAt(0.45, QColor("#051222"))
        sphere.setColorAt(0.76, QColor("#093259"))
        sphere.setColorAt(0.92, QColor("#00a8cc"))
        sphere.setColorAt(1.0, QColor(outer_cyan))
        painter.setPen(QPen(QColor(outer_cyan), 1.6))
        painter.setBrush(sphere)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # Electric-blue internal reflection & subtle green holographic shimmer.
        caustic = QRadialGradient(
            QPointF(cx - radius * 0.28, cy - radius * 0.30),
            radius * 0.72,
        )
        caustic.setColorAt(0.0, QColor(0, 195, 255, 95 if not self.quiet else 45))
        caustic.setColorAt(0.5, QColor(22, 242, 179, 45 if not self.quiet else 20))
        caustic.setColorAt(1.0, QColor(0, 119, 255, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(caustic)
        painter.drawEllipse(QPointF(cx, cy), radius * 0.94, radius * 0.94)

        # Glass specular arc for polished holographic finish.
        glass_pen = QPen(QColor(210, 252, 255, 130))
        glass_pen.setWidthF(1.6)
        painter.setPen(glass_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(
            QRectF(cx - radius * 0.78, cy - radius * 0.78, radius * 1.56, radius * 1.56),
            112 * 16,
            52 * 16,
        )

        caption = (
            "MAX / QUIET" if self.quiet else self._STATE_LABELS.get(self.orb_state, "MAX / IDLE")
        )
        painter.setPen(QColor("#7ee8fa"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        painter.drawText(
            QRectF(0, self.height() - 28, self.width(), 22),
            Qt.AlignmentFlag.AlignCenter,
            caption,
        )
        painter.end()


class _MainThreadRecorderProxy(QObject):
    """Marshals QtMultimedia microphone stop/discard calls onto the Qt main thread."""

    _invoke = Signal(str, object, object)

    def __init__(self, inner: QtMicrophoneRecorder, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._inner = inner
        self._closing = False
        self._invoke.connect(self._execute_on_main, Qt.ConnectionType.QueuedConnection)

    def mark_closing(self) -> None:
        self._closing = True

    @property
    def available(self) -> bool:
        return self._inner.available

    @property
    def headless(self) -> bool:
        return self._inner.headless

    @property
    def reason(self) -> str | None:
        return self._inner.reason

    @property
    def sample_rate(self) -> int:
        return getattr(self._inner, "sample_rate", DEFAULT_SAMPLE_RATE)

    @property
    def channels(self) -> int:
        return getattr(self._inner, "channels", DEFAULT_CHANNELS)

    def start(self) -> bool:
        return self._inner.start()

    def _run_on_main(self, op: str) -> list[bytes]:
        if QThread.currentThread() == self.thread():
            if op == "stop":
                return self._inner.stop_and_drain()
            self._inner.discard()
            return []
        if self._closing:
            return []
        done = threading.Event()
        box: list[Any] = []
        self._invoke.emit(op, done, box)
        if not done.wait(timeout=5.0):
            return []
        if box and isinstance(box[0], Exception):
            raise box[0]
        return list(box[0]) if box else []

    def _execute_on_main(self, op: str, done: threading.Event, box: list[Any]) -> None:
        try:
            if op == "stop":
                box.append(self._inner.stop_and_drain())
            else:
                self._inner.discard()
                box.append([])
        except Exception as exc:
            box.append(exc)
        finally:
            done.set()

    def stop_and_drain(self) -> list[bytes]:
        return self._run_on_main("stop")

    def discard(self) -> None:
        self._run_on_main("discard")


class JobSignals(QObject):
    done = Signal(int, str, object)
    failed = Signal(int, str, str)


class WorkItem(QRunnable):
    """Call local Core off the UI thread, report sanitized errors only."""

    def __init__(self, sequence: int, action: str, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.sequence = sequence
        self.action = action
        self.fn = fn
        self.signals = JobSignals()

    def run(self) -> None:
        try:
            result = self.fn()
            self.signals.done.emit(self.sequence, self.action, result)
        except NativeDesktopError as exc:
            self.signals.failed.emit(self.sequence, self.action, exc.code)
        except Exception:
            self.signals.failed.emit(self.sequence, self.action, "local_core_unavailable")


class MaxWindow(QMainWindow):
    """Always user-visible, owner-controlled MAX desktop command centre."""

    def __init__(
        self,
        core: NativeDesktopCore,
        *,
        transport: Any | None = None,
        chat_service: Any | None = None,
        tool_executor: Any | None = None,
        voice_controller: VoiceController | None = None,
        focus_probe: Callable[[], FocusState] | None = None,
    ) -> None:
        super().__init__()
        self.core = core
        self._config = core.config if core.config is not None else _isolated_fallback_config()
        self._tool_registry = LocalToolRegistry(config=self._config, core=core)

        if tool_executor is not None:
            self._tool_executor = tool_executor
        elif getattr(core, "tool_executor", None) is not None:
            self._tool_executor = core.tool_executor
        elif core.engine is not None:
            self._tool_executor = LocalToolExecutor(
                registry=self._tool_registry,
                core=core,
                config=self._config,
                lifecycle_guard=getattr(core, "call_guard", None),
            )
        else:
            self._tool_executor = None

        resolved_transport = (
            transport if transport is not None else getattr(core, "_chat_transport", None)
        )
        if chat_service is not None:
            self._chat_service = chat_service
        elif transport is None and getattr(core, "has_injected_chat_service", False):
            self._chat_service = core.chat_service
        else:
            self._chat_service = AssistantChatService(
                self._config,
                transport=resolved_transport,
                tools=self._tool_registry.tool_definitions(),
            )
        if hasattr(core, "configure_chat"):
            core.configure_chat(
                transport=resolved_transport,
                chat_service=self._chat_service,
                tool_executor=self._tool_executor if tool_executor is not None else None,
            )

        self._orb_reducer = OrbReducer(device_id="desktop")
        self._recorder_proxy: _MainThreadRecorderProxy | None = None
        if voice_controller is not None:
            self._voice = voice_controller
        elif getattr(core, "_voice_controller", None) is not None:
            self._voice = core._voice_controller
        else:
            self._recorder_proxy = _MainThreadRecorderProxy(QtMicrophoneRecorder(), parent=self)
            self._voice = VoiceController(
                config=self._config,
                recorder=self._recorder_proxy,
                orb=self._orb_reducer,
                focus_probe=focus_probe,
            )
        if hasattr(core, "configure_voice"):
            core.configure_voice(self._voice)

        self.setWindowTitle("MAX — Personal Desktop Companion")
        self.setWindowIcon(_icon())
        self.resize(1260, 820)
        self.setMinimumSize(880, 600)
        self.setStyleSheet(STYLE)

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._running: dict[int, WorkItem] = {}
        self._sequence = 0
        self._latest: dict[str, int] = {}
        self._closing = False
        self._store_ready = False
        self._quiet = False
        self._in_flight_chats = 0
        self._pending_approval_count = 0
        self._task_ids: list[str] = []
        self._history: list[int] = [0]
        self._history_index = 0
        # Keep an OS frame available on tiling window managers.
        self._custom_frame = os.environ.get("MAX_NATIVE_SYSTEM_FRAME") != "yes"
        if self._custom_frame:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        voice_caps = self._voice.capabilities()
        self._speak_enabled = bool(self._config.tts_enabled_default and voice_caps.tts_available)

        self._speak_poll_timer = QTimer(self)
        self._speak_poll_timer.setInterval(150)
        self._speak_poll_timer.timeout.connect(self._poll_speaking_state)

        outer = QWidget()
        outer.setObjectName("root")
        self.setCentralWidget(outer)
        shell = QVBoxLayout(outer)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.title_bar = IslandTitleBar(self)
        self.title_bar.back.connect(self.go_back)
        self.title_bar.forward.connect(self.go_forward)
        self.title_bar.inbox.connect(lambda: self.switch(2))
        self.title_bar.help.connect(self.show_help)
        if not self._custom_frame:
            for button in (
                self.title_bar.min_button,
                self.title_bar.max_button,
                self.title_bar.close_button,
            ):
                button.hide()
        shell.addWidget(self.title_bar)
        shell.addWidget(AccentStrip())
        root = QWidget()
        root.setObjectName("root")
        shell.addWidget(root, 1)
        frame = QHBoxLayout(root)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(225)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 28, 18, 19)
        side.setSpacing(9)
        side.addWidget(_label("✦  MAX", "brand"))
        side.addWidget(_label("PERSONAL INTELLIGENCE", "eyebrow"))
        side.addSpacing(32)
        side.addWidget(_label("WORKSPACE", "eyebrow"))
        self.nav: list[QPushButton] = []
        for index, name in enumerate(("Overview", "Tasks", "Trust", "System")):
            button = _button(f"{('◈', '▤', '⬡', '⌘')[index]}   {name}", "nav")
            button.setProperty("active", "true" if index == 0 else "false")
            button.clicked.connect(lambda _checked=False, n=index: self.switch(n))
            side.addWidget(button)
            self.nav.append(button)
        side.addStretch(1)
        side.addWidget(_label("✦  OWNER-CONTROLLED LOCAL BUILD", "eyebrow"))
        side.addWidget(
            _label(
                "In-process Core. Allowlisted local tools gated by MAX Trust. "
                "Opt-in Push-to-Talk only.",
                "sidebarNote",
            )
        )
        side.addSpacing(12)
        side.addWidget(_label("MAX CORE  •  DESKTOP BUILD", "sidebarNote"))
        frame.addWidget(sidebar)

        right = QWidget()
        body = QVBoxLayout(right)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        top = QFrame()
        top.setObjectName("header")
        header = QHBoxLayout(top)
        header.setContentsMargins(28, 15, 28, 15)
        self.crumb = _label("MAX  /  Overview")
        header.addWidget(self.crumb)
        header.addStretch(1)
        self.online = _label("●  STARTING CORE", "status", wrap=False)
        header.addWidget(self.online)
        self.focus_button = _button("Quiet mode: off")
        self.focus_button.clicked.connect(self.toggle_quiet)
        header.addWidget(self.focus_button)
        body.addWidget(top)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._scroll(self.build_overview()))
        self.stack.addWidget(self._scroll(self.build_tasks()))
        self.stack.addWidget(self._scroll(self.build_trust()))
        self.stack.addWidget(self._scroll(self.build_system()))
        body.addWidget(self.stack, 1)

        footer, footer_layout = _panel()
        footer.setObjectName("commandPanel")
        footer_layout.setContentsMargins(14, 10, 14, 10)
        form = QHBoxLayout()
        form.addWidget(_label("⌘", "sectionTitle"))
        self.command = QPlainTextEdit()
        self.command.setPlaceholderText(
            "Ask MAX anything, or type /help, /status, /tasks, /trust, /tools, /voice, /task ..."
        )
        self.command.setFixedHeight(43)
        self.command.setTabChangesFocus(True)
        self.command.installEventFilter(self)
        self.chat_input = self.command
        form.addWidget(self.command, 1)
        self.send_button = _button("Send ↵", "primary")
        self.send_button.clicked.connect(self.send_chat)
        form.addWidget(self.send_button)
        footer_layout.addLayout(form)
        body.addWidget(footer)
        self.notice = _label(self._default_notice_text(), "sidebarNote")
        self.notice.setContentsMargins(20, 3, 15, 12)
        body.addWidget(self.notice)
        frame.addWidget(right, 1)

        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.command.setFocus)
        for i in range(4):
            QShortcut(
                QKeySequence(f"Alt+{i + 1}"), self, activated=lambda number=i: self.switch(number)
            )
        self._refresh_provider_and_voice_labels()
        self._sync_orb()
        self._sync_island()
        QTimer.singleShot(50, self.refresh)

    def _default_notice_text(self) -> str:
        status = self._provider_status()
        if status.configured and status.provider:
            return f"Connected to {status.provider} ({status.model_id}). Type /help for local commands."
        return "Local Core ready. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env for cloud chat, or type /help."

    def _provider_status(self) -> Any:
        if hasattr(self._chat_service, "provider_status"):
            return self._chat_service.provider_status()
        return self.core.provider_status()

    def _refresh_provider_and_voice_labels(self) -> None:
        status = self._provider_status()
        self.provider_badge.setText(_format_provider_badge(status))
        if getattr(status, "configured", False):
            self.stat_model.setText(str(status.provider or "CLOUD").upper())
            self.stat_model_sub.setText(str(status.model_id or "Configured"))
        else:
            self.stat_model.setText("NO KEY")
            self.stat_model_sub.setText("Set key in .env or config.json")

        caps = self._voice.capabilities()
        self.voice_status_label.setText(_format_voice_status(caps))
        self.speak_toggle.setText("Speak: on" if self._speak_enabled else "Speak: off")

    def _scroll(self, page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _base_page(self, eyebrow: str, title: str, description: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(16)
        layout.addWidget(_label(eyebrow, "eyebrow"))
        layout.addWidget(_label(title, "heading"))
        layout.addWidget(_label(description, "subtle"))
        return page, layout

    def build_overview(self) -> QWidget:
        page, layout = self._base_page(
            "YOUR PERSONAL COMMAND CENTRE",
            "Good to have you here.",
            "Live assistant chat, persistent local tasks, Trust-gated tools, and opt-in voice.",
        )
        hero = QFrame()
        hero.setObjectName("hero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(26, 18, 22, 18)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(10)
        hero_text.addWidget(_label("✦  MAX PRESENCE  /  COMPANION", "eyebrow"))
        hero_text.addWidget(_label("Ready to organise and assist.", "heroTitle"))
        self.provider_badge = _label("Provider: checking…", "status")
        hero_text.addWidget(self.provider_badge)
        self.voice_status_label = _label("Voice: checking…", "subtle")
        hero_text.addWidget(self.voice_status_label)

        actions = QHBoxLayout()
        open_tasks = _button("Tasks ↗", "secondary")
        open_tasks.clicked.connect(lambda: self.switch(1))
        actions.addWidget(open_tasks)
        open_trust = _button("Trust Inbox ⬡", "secondary")
        open_trust.clicked.connect(lambda: self.switch(2))
        actions.addWidget(open_trust)
        check_status = _button("System ⌘", "secondary")
        check_status.clicked.connect(lambda: self.switch(3))
        actions.addWidget(check_status)
        actions.addStretch(1)
        hero_text.addLayout(actions)

        voice_bar = QHBoxLayout()
        self.ptt_button = _button("Push to Talk", "primary")
        self.ptt_button.clicked.connect(self.toggle_push_to_talk)
        voice_bar.addWidget(self.ptt_button)
        self.speak_toggle = _button("Speak: off", "secondary")
        self.speak_toggle.clicked.connect(self.toggle_speak_responses)
        voice_bar.addWidget(self.speak_toggle)
        self.stop_voice_button = _button("Stop", "secondary")
        self.stop_voice_button.clicked.connect(self.interrupt_voice)
        voice_bar.addWidget(self.stop_voice_button)
        voice_bar.addStretch(1)
        hero_text.addLayout(voice_bar)

        hero_text.addStretch(1)
        hero_layout.addLayout(hero_text, 2)
        self.orb = Orb()
        hero_layout.addWidget(self.orb, 1)
        layout.addWidget(hero)

        stats = QHBoxLayout()
        self.stat_core, _ = self._stat_with_sub(
            stats, "CORE STATUS", "STARTING", "Private in-process"
        )
        self.stat_tasks, self.stat_tasks_sub = self._stat_with_sub(
            stats, "TRACKED TASKS", "—", "Checking storage"
        )
        self.stat_trust, self.stat_trust_sub = self._stat_with_sub(
            stats, "PENDING TRUST", "—", "Owner approval gate"
        )
        self.stat_model, self.stat_model_sub = self._stat_with_sub(
            stats, "CLOUD CHAT", "CHECKING", "Gateway + privacy guard"
        )
        layout.addLayout(stats)

        chat_row = QHBoxLayout()
        chat_box, chat_layout = _panel()
        chat_header = QHBoxLayout()
        chat_header.addWidget(_label("ASSISTANT CHAT", "eyebrow"))
        chat_header.addStretch(1)
        clear_chat = _button("Clear", "secondary")
        clear_chat.clicked.connect(self.clear_chat)
        chat_header.addWidget(clear_chat)
        chat_layout.addLayout(chat_header)
        self.chat_transcript = QPlainTextEdit()
        self.chat_transcript.setReadOnly(True)
        self.chat_transcript.setMinimumHeight(210)
        self.chat_transcript.setPlaceholderText(
            "Start a conversation below, or click Push to Talk."
        )
        chat_layout.addWidget(self.chat_transcript, 1)
        chat_row.addWidget(chat_box, 2)

        tools_box, tools_layout = _panel()
        tools_layout.addWidget(_label("TOOL OUTCOMES", "eyebrow"))
        self.tool_outcomes_list = QListWidget()
        self.tool_outcomes_list.setMinimumHeight(210)
        self.tool_outcomes_list.addItem("No tool executions in this session yet.")
        tools_layout.addWidget(self.tool_outcomes_list, 1)
        chat_row.addWidget(tools_box, 1)
        layout.addLayout(chat_row)

        sections = QHBoxLayout()
        tasks_box, task_layout = _panel()
        task_layout.addWidget(_label("RECENT TASKS", "eyebrow"))
        self.recent_tasks = _label("Checking Core…", "subtle")
        task_layout.addWidget(self.recent_tasks)
        task_layout.addStretch(1)
        sections.addWidget(tasks_box, 1)
        trust_box, trust_layout = _panel()
        trust_layout.addWidget(_label("MAX TRUST", "eyebrow"))
        self.recent_trust = _label("Checking Core…", "subtle")
        trust_layout.addWidget(self.recent_trust)
        trust_layout.addWidget(
            _label("Model proposals never bypass owner approval in MAX Trust.", "subtle")
        )
        trust_layout.addStretch(1)
        sections.addWidget(trust_box, 1)
        layout.addLayout(sections)
        layout.addStretch(1)
        return page

    def _stat_with_sub(
        self, layout: QHBoxLayout, name: str, value: str, sub: str
    ) -> tuple[QLabel, QLabel]:
        panel = QFrame()
        panel.setObjectName("stat")
        box = QVBoxLayout(panel)
        box.setContentsMargins(16, 14, 16, 14)
        box.addWidget(_label(name, "eyebrow"))
        count = _label(value, "statValue")
        box.addWidget(count)
        sub_label = _label(sub, "subtle")
        box.addWidget(sub_label)
        layout.addWidget(panel, 1)
        return count, sub_label

    def build_tasks(self) -> QWidget:
        page, layout = self._base_page(
            "WORKSPACE  /  TRACKING",
            "Your task journal.",
            "Queue a tracked objective in your local Task Journal. Nothing runs silently.",
        )
        composer, compose_layout = _panel()
        compose_layout.addWidget(_label("NEW TRACKED TASK", "eyebrow"))
        self.objective = QPlainTextEdit()
        self.objective.setPlaceholderText(
            "Describe work to track in the journal (1–500 characters)."
        )
        self.objective.setMaximumBlockCount(8)
        self.objective.setFixedHeight(88)
        compose_layout.addWidget(self.objective)
        entry_row = QHBoxLayout()
        entry_row.addWidget(_label("Up to 500 characters  •  Explicit submit only", "subtle"))
        entry_row.addStretch(1)
        self.queue_button = _button("Queue task ↗", "primary")
        self.queue_button.setEnabled(False)
        self.queue_button.clicked.connect(self.queue_task)
        entry_row.addWidget(self.queue_button)
        compose_layout.addLayout(entry_row)
        layout.addWidget(composer)

        row = QHBoxLayout()
        panel, box = _panel()
        box.addWidget(_label("RECENT TASKS", "eyebrow"))
        self.task_list = QListWidget()
        self.task_list.setMinimumHeight(220)
        self.task_list.itemClicked.connect(self.select_task)
        box.addWidget(self.task_list, 1)
        row.addWidget(panel, 1)
        panel2, box2 = _panel()
        box2.addWidget(_label("SELECTED TASK TIMELINE", "eyebrow"))
        box2.addWidget(_label("Signed event history from your local Task Journal.", "subtle"))
        self.events_list = QListWidget()
        self.events_list.setMinimumHeight(220)
        box2.addWidget(self.events_list, 1)
        row.addWidget(panel2, 1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def build_trust(self) -> QWidget:
        page, layout = self._base_page(
            "OWNER CONTROL  /  TRUST",
            "Every action accountable.",
            "Approve or deny pending Trust requests. Model proposals never self-approve.",
        )
        box = QFrame()
        box.setObjectName("warning")
        warning = QVBoxLayout(box)
        warning.setContentsMargins(20, 15, 20, 15)
        warning.addWidget(_label("⬡  OWNER APPROVAL GATE", "eyebrow"))
        warning.addWidget(
            _label(
                "Gated tools such as task.create and notes.save wait here until you explicitly "
                "Approve or Deny them.",
                "subtle",
            )
        )
        layout.addWidget(box)
        panel, inside = _panel()
        inside.addWidget(_label("PENDING TRUST REQUESTS", "eyebrow"))
        self.trust_list = QListWidget()
        self.trust_list.setMinimumHeight(250)
        inside.addWidget(self.trust_list)
        action_row = QHBoxLayout()
        self.approve_button = _button("Approve selected", "primary")
        self.approve_button.setEnabled(False)
        self.approve_button.clicked.connect(lambda: self.decide_selected_approval(True))
        self.trust_approve_button = self.approve_button
        action_row.addWidget(self.approve_button)
        self.deny_button = _button("Deny selected", "secondary")
        self.deny_button.setEnabled(False)
        self.deny_button.clicked.connect(lambda: self.decide_selected_approval(False))
        self.trust_deny_button = self.deny_button
        action_row.addWidget(self.deny_button)
        action_row.addStretch(1)
        inside.addLayout(action_row)
        layout.addWidget(panel)
        layout.addStretch(1)
        return page

    def build_system(self) -> QWidget:
        page, layout = self._base_page(
            "LOCAL OPERATIONS  /  STATUS",
            "Know what really works.",
            "Live status separated cleanly from future plans.",
        )
        panel, content = _panel()
        self.system_core = _label("Core: starting…", "sectionTitle")
        content.addWidget(self.system_core)
        self.system_storage = _label("Storage: checking…", "subtle")
        content.addWidget(self.system_storage)
        refresh = _button("Refresh Core state ↻", "primary")
        refresh.clicked.connect(self.refresh)
        content.addWidget(refresh)
        layout.addWidget(panel)

        capabilities, available = _panel()
        available.addWidget(_label("CAPABILITY STATUS", "eyebrow"))
        self.capability_labels: dict[str, QLabel] = {}
        for key, initial in (
            ("ui", "◇  Native desktop window  —  ACTIVE"),
            ("storage", "◇  Task journal & Trust persistence  —  checking…"),
            ("chat", "◇  Cloud LLM chat  —  checking…"),
            (
                "tools",
                f"◇  Allowlisted local tools ({len(self._tool_registry.tool_definitions())})  —  ACTIVE",
            ),
            ("voice", "◇  Voice (Push-to-Talk & TTS)  —  checking…"),
            (
                "unimplemented_os",
                "◇  Unrestricted shell and OS hotkeys  —  not enabled",
            ),
        ):
            lbl = _label(initial, "subtle")
            available.addWidget(lbl)
            self.capability_labels[key] = lbl
        layout.addWidget(capabilities)

        panel, inside = _panel()
        inside.addWidget(_label("MCP CONNECTIONS  /  OWNER REVIEW", "eyebrow"))
        inside.addWidget(
            _label(
                "HTTPS servers only. Credentials are read from environment variables; "
                "discovery never enables a tool. Every approved tool call still needs "
                "one explicit Trust Inbox approval.",
                "subtle",
            )
        )
        self.mcp_list = QListWidget()
        self.mcp_list.setMinimumHeight(160)
        inside.addWidget(self.mcp_list)
        actions = QHBoxLayout()
        for title, handler in (
            ("Add server", self.mcp_add_server),
            ("Discover", self.mcp_discover_selected),
            ("Enable tool", lambda: self.mcp_review_selected(True)),
            ("Disable tool", lambda: self.mcp_review_selected(False)),
        ):
            button = _button(title, "secondary")
            button.clicked.connect(handler)
            actions.addWidget(button)
        inside.addLayout(actions)
        layout.addWidget(panel)
        layout.addStretch(1)
        self._update_capability_labels()
        self._refresh_mcp_list()
        return page

    def _refresh_mcp_list(self) -> None:
        if not hasattr(self, "mcp_list"):
            return
        self.mcp_list.clear()
        try:
            connections = self.core.mcp_connections()
        except Exception:
            self.mcp_list.addItem("MCP configuration unavailable; check mcp-servers.json.")
            return
        if not connections:
            self.mcp_list.addItem("No MCP servers configured. Add a trusted HTTPS endpoint.")
        for connection in connections:
            ident = connection["id"]
            authenticated = connection["authenticated"]
            label = "credential ready" if authenticated else "missing environment credential"
            header = QListWidgetItem(f"{ident}  •  {label}")
            header.setData(Qt.ItemDataRole.UserRole, (ident, None))
            self.mcp_list.addItem(header)
            for tool in connection["tools"]:
                enabled = "ENABLED" if tool["enabled"] else "NOT APPROVED"
                hint = "read-only hint" if tool["read_only_hint"] else "write-capable"
                entry = QListWidgetItem(f"    {tool['name']}  •  {enabled}  •  {hint}")
                entry.setData(Qt.ItemDataRole.UserRole, (ident, tool["name"]))
                self.mcp_list.addItem(entry)

    def _mcp_selection(self) -> tuple[str, str | None] | None:
        current = self.mcp_list.currentItem()
        data = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        return data if isinstance(data, tuple) and len(data) == 2 else None

    def mcp_add_server(self) -> None:
        ident, ok = QInputDialog.getText(self, "MCP connection", "Connection ID (e.g. github):")
        if not ok:
            return
        endpoint, ok = QInputDialog.getText(
            self, "MCP connection", "Trusted HTTPS endpoint:"
        )
        if not ok:
            return
        env_name, ok = QInputDialog.getText(
            self, "MCP credential", "Token environment variable (blank if public):"
        )
        if not ok:
            return
        self._run(
            "mcp_add",
            lambda: self.core.mcp_add_connection(
                ident.strip(), endpoint.strip(), env_name.strip()
            ),
        )

    def mcp_discover_selected(self) -> None:
        selection = self._mcp_selection()
        if selection is None:
            self.notice.setText("Select an MCP server first.")
            return
        ident, _ = selection
        question = (
            f"Connect to {ident} and discover its tool schemas? "
            "This resets all previous approvals for this server."
        )
        if QMessageBox.question(self, "MCP discovery", question) != QMessageBox.StandardButton.Yes:
            return
        self._run("mcp_discover", lambda: self.core.mcp_discover(ident))

    def mcp_review_selected(self, enable: bool) -> None:
        selection = self._mcp_selection()
        if selection is None or selection[1] is None:
            self.notice.setText("Select a discovered tool first.")
            return
        ident, name = selection
        assert name is not None
        if enable:
            details = next(
                (
                    tool
                    for connection in self.core.mcp_connections()
                    if connection["id"] == ident
                    for tool in connection["tools"]
                    if tool["name"] == name
                ),
                None,
            )
            if details is None:
                return
            question = (
                f"Enable {ident}.{name}?\n"
                f"Schema SHA-256: {details['sha256']}\n"
                f"Arguments: {', '.join(details['allowed_arguments']) or '(discover first)'}\n"
                "Remote tools are untrusted and every invocation requires a fresh owner approval."
            )
            if QMessageBox.question(self, "Review MCP tool", question) != QMessageBox.StandardButton.Yes:
                return
        self._run(
            "mcp_review",
            lambda: self.core.mcp_review(ident, name, enable=enable),
        )

    def _update_capability_labels(self) -> None:
        if not hasattr(self, "capability_labels"):
            return
        storage_text = (
            "ACTIVE (local persistence ready)"
            if self._store_ready
            else "inactive (database not configured)"
        )
        self.capability_labels["storage"].setText(
            f"◇  Task journal & Trust Inbox  —  {storage_text}"
        )
        status = self._provider_status()
        if getattr(status, "configured", False):
            chat_text = f"ACTIVE ({status.provider} / {status.model_id})"
        else:
            chat_text = "ready (API key not configured in .env / config.json)"
        self.capability_labels["chat"].setText(f"◇  Cloud LLM chat  —  {chat_text}")
        caps = self._voice.capabilities()
        self.capability_labels["voice"].setText(
            f"◇  Voice  —  {_format_voice_status(caps).removeprefix('Voice: ')}"
        )

    def switch(self, index: int, *, record: bool = True) -> None:
        if not 0 <= index < self.stack.count():
            return
        if record and index != self._history[self._history_index]:
            del self._history[self._history_index + 1 :]
            self._history.append(index)
            self._history = self._history[-50:]
            self._history_index = len(self._history) - 1
        self.title_bar.set_history(
            self._history_index > 0, self._history_index < len(self._history) - 1
        )
        self.stack.setCurrentIndex(index)
        self.crumb.setText("MAX  /  " + ("Overview", "Tasks", "Trust", "System")[index])
        for n, button in enumerate(self.nav):
            button.setProperty("active", "true" if n == index else "false")
            button.style().unpolish(button)
            button.style().polish(button)

    def go_back(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self.switch(self._history[self._history_index], record=False)

    def go_forward(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.switch(self._history[self._history_index], record=False)

    def show_help(self) -> None:
        self.notice.setText(
            "Commands: /status, /tasks, /trust, /tools, /voice, /task <objective>. "
            "Alt+Left/Right navigate, Ctrl+K focuses the command bar."
        )

    def _sync_island(self) -> None:
        if any(work.action == "model" for work in self._running.values()):
            self.title_bar.island.set_state(IslandState.THINKING)
        elif self._quiet:
            self.title_bar.island.set_state(IslandState.QUIET)
        else:
            self.title_bar.island.set_state(IslandState.IDLE)

    def toggle_quiet(self) -> None:
        self._quiet = not self._quiet
        if self._quiet:
            self._voice.revoke_microphone()
            self.ptt_button.setText("Push to Talk")
            self.ptt_button.setEnabled(True)
        self.orb.set_quiet(self._quiet)
        self._sync_island()
        self.focus_button.setText("Quiet mode: on" if self._quiet else "Quiet mode: off")
        self._sync_orb()
        self.notice.setText(
            "Quiet mode limits animation in this window and pauses microphone capture."
            if self._quiet
            else self._default_notice_text()
        )

    def eventFilter(self, watched: QObject, event: Any) -> bool:
        if watched is self.command and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.send_chat()
                    return True
        return super().eventFilter(watched, event)

    def run_command(self) -> None:
        self.send_chat()

    def clear_chat(self) -> None:
        if hasattr(self.core, "clear_chat_history"):
            self.core.clear_chat_history()
        elif hasattr(self._chat_service, "clear_history"):
            self._chat_service.clear_history()
        self.chat_transcript.clear()
        self.tool_outcomes_list.clear()
        self.tool_outcomes_list.addItem("No tool executions in this session yet.")
        self.notice.setText("Cleared conversation history.")

    def send_chat(self) -> None:
        raw = self.chat_input.toPlainText().strip()
        self.chat_input.clear()
        if not raw:
            return
        lower = raw.lower()
        if lower == "/help":
            self.notice.setText(
                "Commands: /help, /status, /tasks, /trust, /tools, /voice, /task <objective>. "
                "Any other text chats with MAX."
            )
            return
        if lower == "/status":
            self.switch(3)
            self.refresh()
            return
        if lower == "/tasks":
            self.switch(1)
            return
        if lower == "/trust":
            self.switch(2)
            return
        if lower == "/tools":
            definitions = self.core.tool_definitions()
            names = ", ".join(td.name for td in definitions)
            self._append_transcript(f"[Tools] Available allowlisted tools: {names}")
            self.notice.setText(f"Allowlisted tools ({len(definitions)}): {names}")
            return
        if lower == "/voice":
            status_line = _format_voice_status(self._voice.capabilities())
            self._append_transcript(f"[{status_line}]")
            self.notice.setText(status_line)
            return
        if lower == "/task" or lower.startswith("/task "):
            objective = raw[5:].strip()
            if not 1 <= len(objective) <= 500:
                self.notice.setText("Task descriptions must have 1–500 characters.")
                return
            self.objective.setPlainText(objective)
            self.switch(1)
            self.objective.setFocus()
            self.notice.setText("Review the text and choose Queue task. Nothing runs yet.")
            return
        self._submit_chat_message(raw)

    def _append_transcript(self, line: str) -> None:
        existing = self.chat_transcript.toPlainText()
        if existing:
            self.chat_transcript.appendPlainText(line)
        else:
            self.chat_transcript.setPlainText(line)

    def _record_tool_outcomes(self, summaries: tuple[ToolExecutionSummary, ...]) -> None:
        if not summaries:
            return
        if (
            self.tool_outcomes_list.count() == 1
            and self.tool_outcomes_list.item(0).text() == "No tool executions in this session yet."
        ):
            self.tool_outcomes_list.clear()
        for summary in summaries:
            preview = _format_tool_output_preview(summary.output)
            text = f"[{summary.status.upper()}] {summary.tool_name} — {preview}"
            self.tool_outcomes_list.addItem(text)
            self._append_transcript(f"[Tool {summary.tool_name} -> {summary.status}] {preview}")

    @property
    def _chat_busy(self) -> bool:
        return self._in_flight_chats > 0

    def _submit_chat_message(self, user_text: str) -> None:
        cleaned = user_text.strip()
        if not cleaned or self._closing:
            return
        if _contains_secret_value(cleaned):
            self._append_transcript(_REDACTED_USER_INPUT_LINE)
        else:
            self._append_transcript(f"You: {cleaned}")
        self._in_flight_chats += 1
        self._sync_orb()
        self.notice.setText("Sending message to MAX…")

        def _invoke() -> ChatTurnResult:
            if hasattr(self.core, "send_chat_message"):
                return self.core.send_chat_message(cleaned)
            return self._chat_service.send_message(cleaned, tool_executor=self._tool_executor)

        self._run("chat", _invoke)

    def _handle_chat_result(self, result: ChatTurnResult) -> None:
        self._record_tool_outcomes(result.tool_executions)
        if result.ok:
            self._append_transcript(f"MAX: {result.reply}")
            awaiting = any(t.status == "awaiting_approval" for t in result.tool_executions)
            if awaiting:
                self.notice.setText("Action paused awaiting your approval in the Trust Inbox tab.")
            else:
                self.notice.setText(self._default_notice_text())
            if self._speak_enabled and not self._quiet and result.reply.strip():
                if self._voice.speak_text(result.reply):
                    self._speak_poll_timer.start()
        else:
            code = result.error_code or "provider_error"
            self._append_transcript(f"MAX ({code}): {result.reply}")
            self.notice.setText(result.reply)
        self._refresh_provider_and_voice_labels()
        self._sync_orb()
        if result.tool_executions:
            self.refresh()

    def _sync_orb(self) -> None:
        try:
            view = self._voice.orb_view(quiet_mode=self._quiet)
        except Exception:
            view = OrbView(OrbState.IDLE, OrbVisibility.PASSIVE)
        if not self._quiet and view.visibility != OrbVisibility.HIDDEN:
            if self._chat_busy:
                view = OrbView(OrbState.THINKING, OrbVisibility.ACTIVE)
            elif self._pending_approval_count > 0 and view.state == OrbState.IDLE:
                view = OrbView(OrbState.AWAITING_APPROVAL, OrbVisibility.ACTIVE)
        self.orb.set_orb_view(view)

    def _poll_speaking_state(self) -> None:
        if self._closing:
            self._speak_poll_timer.stop()
            return
        if not self._voice.is_speaking:
            self._speak_poll_timer.stop()
        self._sync_orb()

    def toggle_push_to_talk(self) -> None:
        if self._voice.is_recording:
            self.ptt_button.setText("Transcribing…")
            self.ptt_button.setEnabled(False)
            self.notice.setText("Transcribing speech…")
            self._run("stt_transcribe", self._voice.stop_push_to_talk_and_transcribe)
            return

        if self._quiet:
            self.notice.setText("Turn off Quiet mode before using Push to Talk.")
            return
        if (
            self._voice.stt.available
            and getattr(self._voice.stt, "requires_remote_upload", False)
            and not self._voice.remote_stt_consent
        ):
            decision = QMessageBox.question(
                self,
                "Send microphone audio to the speech provider?",
                "MAX uses an external speech-transcription service for this turn. "
                "If you continue, microphone audio recorded this turn will be uploaded over "
                "HTTPS to the configured provider for transcription. "
                "Choose No to keep the recording on your device.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if decision != QMessageBox.StandardButton.Yes:
                self.notice.setText("Remote speech upload denied. No recording started.")
                return
            self._voice.grant_remote_stt_consent()
        started = self._voice.start_push_to_talk(direct_user_gesture=True)
        if not started:
            reason = (
                self._voice.last_error or self._voice.capabilities().reason or "voice_unavailable"
            )
            self.notice.setText(f"Push to Talk unavailable: {reason}")
            self._sync_orb()
            return
        self.ptt_button.setText("Stop & Send")
        self.notice.setText("Listening… click Stop & Send when finished speaking.")
        self._sync_orb()

    def toggle_speak_responses(self) -> None:
        caps = self._voice.capabilities()
        if not self._speak_enabled and not caps.tts_available:
            reason = caps.reason or "tts_unavailable"
            self.notice.setText(f"Speech output unavailable: {reason}")
            return
        self._speak_enabled = not self._speak_enabled
        self.speak_toggle.setText("Speak: on" if self._speak_enabled else "Speak: off")
        self.notice.setText(
            "Spoken replies enabled." if self._speak_enabled else "Spoken replies muted."
        )

    def interrupt_voice(self) -> None:
        self._speak_poll_timer.stop()
        if self._voice.is_recording:
            self._voice.revoke_microphone()
            self.ptt_button.setText("Push to Talk")
            self.ptt_button.setEnabled(True)
            self.notice.setText("Microphone capture cancelled.")
        else:
            self._voice.interrupt_speech()
            self.notice.setText("Speech output stopped.")
        self._sync_orb()

    def _run(self, action: str, fn: Callable[[], Any]) -> None:
        if self._closing:
            return
        self._sequence += 1
        sequence = self._sequence
        self._latest[action] = sequence
        work = WorkItem(sequence, action, fn)
        work.signals.done.connect(self._done)
        work.signals.failed.connect(self._failed)
        self._running[sequence] = work
        self._pool.start(work)

    def refresh(self) -> None:
        self.online.setText("●  CHECKING LOCAL CORE")
        self._run("snapshot", self.core.snapshot)

    def queue_task(self) -> None:
        if not self._store_ready:
            self.notice.setText(
                "Local storage is not ready. Configure local SQLite or PostgreSQL before creating tasks."
            )
            return
        objective = self.objective.toPlainText().strip()
        if not 1 <= len(objective) <= 500:
            self.notice.setText("Enter a description of 1–500 characters.")
            return
        self.queue_button.setEnabled(False)
        self.notice.setText("Recording task in the local Core journal…")
        self._run("create", lambda: self.core.create_task(objective))

    def select_task(self, item: QListWidgetItem) -> None:
        identifier = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(identifier, str):
            return
        self.events_list.clear()
        self.events_list.addItem("Loading read-only journal events…")
        self._run("timeline", lambda: self.core.timeline(identifier))

    def decide_selected_approval(self, approve: bool) -> None:
        if not self._store_ready:
            self.notice.setText("Trust storage is not ready.")
            return
        current = self.trust_list.currentItem()
        if current is None and self.trust_list.count() > 0:
            current = self.trust_list.item(0)
        approval_id = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        if not isinstance(approval_id, str):
            self.notice.setText("Select a pending Trust request first.")
            return
        self.approve_button.setEnabled(False)
        self.deny_button.setEnabled(False)
        self.notice.setText("Recording Trust decision…")

        def _decide() -> ApprovalResolution:
            if hasattr(self.core, "decide_approval_with_continuation"):
                return self.core.decide_approval_with_continuation(approval_id, approve)
            decided = self.core.decide_approval(approval_id, approve)
            return ApprovalResolution(approval=decided)

        self._run("decide_approval", _decide)

    def _done(self, sequence: int, action: str, value: Any) -> None:
        self._running.pop(sequence, None)
        if self._closing:
            return
        if action in {"snapshot", "timeline"} and self._latest.get(action) != sequence:
            return
        if action == "snapshot":
            self._show_snapshot(value)
        elif action == "create":
            self.objective.clear()
            self.queue_button.setEnabled(self._store_ready)
            self.notice.setText("Task recorded in your local Task Journal.")
            self.refresh()
        elif action == "timeline":
            self.events_list.clear()
            for event in value:
                kind = str(event.get("kind", "event"))[:90]
                revision = str(event.get("task_revision", "?"))
                self.events_list.addItem(f"{kind}    •    Revision {revision}")
            if not value:
                self.events_list.addItem("No recorded events.")
        elif action == "chat":
            self._in_flight_chats = max(0, self._in_flight_chats - 1)
            self._handle_chat_result(value)
        elif action == "decide_approval":
            resolution = (
                value
                if isinstance(value, ApprovalResolution)
                else ApprovalResolution(approval=value)
            )
            state = str(resolution.approval.get("state", "updated"))
            self.notice.setText(f"Trust request {state}.")
            if resolution.chat_result is not None:
                self._handle_chat_result(resolution.chat_result)
            elif resolution.tool_summary is not None:
                self._record_tool_outcomes((resolution.tool_summary,))
            self.refresh()
        elif action in {"mcp_add", "mcp_discover", "mcp_review"}:
            self._refresh_mcp_list()
            self.notice.setText("MCP connection settings updated. Review enabled tools before use.")
        elif action == "stt_transcribe":
            self.ptt_button.setText("Push to Talk")
            self.ptt_button.setEnabled(True)
            if isinstance(value, str) and value.strip():
                # Route speech transcript strictly as untrusted user chat input.
                self._submit_chat_message(value.strip())
            else:
                reason = self._voice.last_error or "no_speech_captured"
                self.notice.setText(f"Speech transcription did not produce text ({reason}).")
                self._sync_orb()

    def _failed(self, sequence: int, action: str, code: str) -> None:
        self._running.pop(sequence, None)
        if self._closing:
            return
        if action in {"snapshot", "timeline"} and self._latest.get(action) != sequence:
            return
        messages = {
            "task_store_not_ready": "Local database is not ready.",
            "invalid_task_description": "Provide a 1–500 character description.",
            "invalid_task_identifier": "Invalid task identifier.",
            "invalid_approval_identifier": "Invalid approval identifier.",
            "local_postgresql_required": "Only a local PostgreSQL database is permitted.",
            "local_core_request_rejected": "Request was rejected by MAX Trust or Core.",
        }
        message = messages.get(code, "Local Core operation unavailable.")
        if action.startswith("mcp_"):
            message = f"MCP action failed ({code}). Check endpoint, credentials and reviewed schema."
            self._refresh_mcp_list()
        self.notice.setText(message)
        if action == "snapshot":
            self.online.setText("●  CORE NOT READY")
            self.system_core.setText("Core: unavailable")
            self.system_storage.setText("Verify your local database configuration.")
        elif action == "create":
            self.queue_button.setEnabled(self._store_ready)
        elif action == "timeline":
            self.events_list.clear()
            self.events_list.addItem(message)
        elif action == "chat":
            self._in_flight_chats = max(0, self._in_flight_chats - 1)
            self._append_transcript(f"MAX (error): {message}")
            self._sync_orb()
        elif action == "decide_approval":
            self.refresh()
        elif action == "stt_transcribe":
            self.ptt_button.setText("Push to Talk")
            self.ptt_button.setEnabled(True)
            self._sync_orb()

    def _show_snapshot(self, snapshot: DesktopSnapshot) -> None:
        self.online.setText("●  LOCAL CORE ACTIVE")
        self.system_core.setText("Core: " + snapshot.core_status)
        self.system_storage.setText("Storage: " + snapshot.storage_status)
        self._store_ready = snapshot.storage_status in {
            "Local SQLite ready",
            "Local PostgreSQL ready",
        }
        self.queue_button.setEnabled(self._store_ready)
        self.stat_core.setText("ONLINE")
        self.stat_tasks.setText(str(len(snapshot.tasks)) if self._store_ready else "—")
        self.stat_trust.setText(str(len(snapshot.approvals)) if self._store_ready else "—")
        self.stat_tasks_sub.setText(
            snapshot.storage_status if self._store_ready else "Database not configured"
        )
        self.stat_trust_sub.setText(
            "Owner approval gate" if self._store_ready else "Requires local storage"
        )
        self._pending_approval_count = len(snapshot.approvals) if self._store_ready else 0
        self.task_list.clear()
        if not self._store_ready:
            self.recent_tasks.setText(
                "No database configured. Explore the interface without creating tasks."
            )
            self.recent_trust.setText(
                "Trust storage unavailable until local storage is configured."
            )
            self.task_list.addItem("Task journal disabled: configure local SQLite or PostgreSQL.")
            self.trust_list.clear()
            self.trust_list.addItem("No Trust database connected.")
            self.approve_button.setEnabled(False)
            self.deny_button.setEnabled(False)
            self._update_capability_labels()
            self._sync_orb()
            return
        tasks = snapshot.tasks[:30]
        self.recent_tasks.setText(
            "\n".join(f"• {str(task.get('objective', 'Task'))[:70]}" for task in tasks[:3])
            if tasks
            else "No recorded tasks yet."
        )
        for task in tasks:
            objective = str(task.get("objective", "Task"))[:130]
            state = str(task.get("state", "unknown"))[:30]
            item = QListWidgetItem(f"{objective}\n{state.upper()}")
            item.setData(Qt.ItemDataRole.UserRole, task.get("id"))
            self.task_list.addItem(item)
        if not tasks:
            self.task_list.addItem("No tracked tasks yet.")

        self.trust_list.clear()
        self.recent_trust.setText(
            f"{len(snapshot.approvals)} pending request(s) awaiting owner decision."
        )
        for approval in snapshot.approvals[:50]:
            tool = str(approval.get("tool_name", "Unknown tool"))[:80]
            state = str(approval.get("state", "pending"))[:30]
            item = QListWidgetItem(f"{tool}    •    {state.upper()}")
            item.setData(Qt.ItemDataRole.UserRole, approval.get("id"))
            self.trust_list.addItem(item)
        has_approvals = bool(snapshot.approvals)
        if has_approvals:
            self.trust_list.setCurrentRow(0)
        else:
            self.trust_list.addItem("No pending Trust requests.")
        self.approve_button.setEnabled(has_approvals)
        self.deny_button.setEnabled(has_approvals)
        self._update_capability_labels()
        self._sync_orb()

    def closeEvent(self, event: Any) -> None:
        self._closing = True
        self._speak_poll_timer.stop()
        if self._recorder_proxy is not None:
            self._recorder_proxy.mark_closing()
        try:
            self._voice.revoke_microphone()
        except Exception:
            pass
        self.orb.set_quiet(True)
        self._pool.waitForDone()
        self.core.close()
        super().closeEvent(event)


def launch(*, smoke_test: bool = False) -> int:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("MAX Personal Assistant")
    app.setWindowIcon(_icon())
    try:
        core = NativeDesktopCore.from_env()
    except NativeDesktopError as exc:
        print(f"MAX Desktop configuration error: {exc.code}", file=sys.stderr)
        return 2
    window = MaxWindow(core)
    window.show()
    if smoke_test:
        app.processEvents()
        healthy = window.isVisible() and window.stack.count() == 4
        window.close()
        return 0 if healthy else 1
    return app.exec()
