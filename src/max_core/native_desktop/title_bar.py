# mypy: ignore-errors
"""MAX frameless title bar with a centred status island.

The island shows MAX's *real* state. Nothing in this module turns on a
microphone: ``LISTENING`` is only displayed when a caller with a genuine audio
source sets it. Until a voice pipeline is connected, the app uses IDLE,
THINKING (a real model call is running) and QUIET.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

BAR_HEIGHT = 44
ACCENT = QColor("#9b4dff")


class IslandState(StrEnum):
    IDLE = "IDLE"
    THINKING = "THINKING"
    LISTENING = "LISTENING"
    SPEAKING = "SPEAKING"
    QUIET = "QUIET"


_ANIMATED = {IslandState.THINKING, IslandState.LISTENING, IslandState.SPEAKING}


class Waveform(QWidget):
    """Purple bar waveform. Flat when idle; animated only for active states."""

    BARS = 11

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(60, 22)
        self.phase = 0.0
        self.active = False
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)

    def set_active(self, active: bool) -> None:
        self.active = active
        if active:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self.phase = (self.phase + 0.45) % (math.pi * 200)
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        width = 3.0
        gap = (self.width() - self.BARS * width) / (self.BARS - 1)
        mid = self.height() / 2
        for i in range(self.BARS):
            if self.active:
                wave = (
                    abs(math.sin(self.phase + i * 0.9)) * 0.7
                    + abs(math.sin(self.phase * 0.6 + i * 1.7)) * 0.3
                )
                h = 4 + wave * (self.height() - 4)
                alpha = 255
            else:
                h = 4 + (3 if i % 3 == 1 else 0)
                alpha = 150
            color = QColor(ACCENT)
            color.setAlpha(alpha)
            painter.setBrush(color.lighter(130))
            x = i * (width + gap)
            painter.drawRoundedRect(QRectF(x, mid - h / 2, width, h), 1.5, 1.5)
        painter.end()


class StatusIsland(QWidget):
    """Black notch hanging from the top edge: state label + waveform."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(QSize(470, BAR_HEIGHT - 4))
        self.state = IslandState.IDLE
        layout = QHBoxLayout(self)
        layout.setContentsMargins(26, 0, 22, 4)
        self.label = QLabel(self.state.value)
        self.label.setObjectName("islandLabel")
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.wave = Waveform()
        layout.addWidget(self.wave)

    def set_state(self, state: IslandState) -> None:
        self.state = IslandState(state)
        self.label.setText(self.state.value)
        self.wave.set_active(self.state in _ANIMATED)
        self.setToolTip(
            {
                IslandState.IDLE: "MAX is ready.",
                IslandState.THINKING: "A model request is running.",
                IslandState.LISTENING: "MAX is receiving audio.",
                IslandState.SPEAKING: "MAX is speaking.",
                IslandState.QUIET: "Quiet mode: animation paused.",
            }[self.state]
        )

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, r = float(self.width()), float(self.height()), 18.0
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(w, 0)
        path.lineTo(w, h - r)
        path.quadTo(w, h, w - r, h)
        path.lineTo(r, h)
        path.quadTo(0, h, 0, h - r)
        path.closeSubpath()
        painter.fillPath(path, QColor("#000000"))
        if self.state in _ANIMATED:
            pen = QPen(QColor(155, 77, 255, 90))
            pen.setWidthF(1.0)
            painter.strokePath(path, pen)
        painter.end()


class _IconButton(QPushButton):
    def __init__(self, glyph: str, tip: str, name: str = "titleIcon") -> None:
        super().__init__(glyph)
        self.setObjectName(name)
        self.setToolTip(tip)
        self.setFixedSize(QSize(40, 32))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


TITLE_STYLE = """
QWidget#titleBar QPushButton#titleIcon, QWidget#titleBar QPushButton#titleClose {
    background: transparent; border: none; border-radius: 6px; padding: 0;
    color: #d9d4e4; font-size: 16px; font-weight: 400;
}
QWidget#titleBar QPushButton#titleIcon:hover { background: #2a2433; }
QWidget#titleBar QPushButton#titleIcon:disabled { color: #4e4858; background: transparent; }
QWidget#titleBar QPushButton#titleClose:hover { background: #c42b3c; color: #ffffff; }
QLabel#islandLabel { color: #c9a8ff; font-size: 11px; font-weight: 700; letter-spacing: 2px;
                     font-family: "Cascadia Mono", "JetBrains Mono", "Consolas", monospace; }
"""


class IslandTitleBar(QWidget):
    back = Signal()
    forward = Signal()
    inbox = Signal()
    help = Signal()

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        self.setObjectName("titleBar")
        self.setFixedHeight(BAR_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 8, 0)
        layout.setSpacing(2)
        self.back_button = _IconButton("←", "Back (Alt+Left)")
        self.forward_button = _IconButton("→", "Forward (Alt+Right)")
        self.back_button.clicked.connect(self.back)
        self.forward_button.clicked.connect(self.forward)
        layout.addWidget(self.back_button)
        layout.addWidget(self.forward_button)
        layout.addStretch(1)
        self.island = StatusIsland()
        layout.addWidget(self.island, 0, Qt.AlignmentFlag.AlignTop)
        layout.addStretch(1)
        self.inbox_button = _IconButton("✉", "Trust inbox")
        self.help_button = _IconButton("?", "Help")
        self.inbox_button.clicked.connect(self.inbox)
        self.help_button.clicked.connect(self.help)
        layout.addWidget(self.inbox_button)
        layout.addWidget(self.help_button)
        layout.addSpacing(14)
        self.min_button = _IconButton("—", "Minimise")
        self.max_button = _IconButton("☐", "Maximise")
        self.close_button = _IconButton("✕", "Close", "titleClose")
        self.min_button.clicked.connect(window.showMinimized)
        self.max_button.clicked.connect(self.toggle_maximised)
        self.close_button.clicked.connect(window.close)
        for button in (self.min_button, self.max_button, self.close_button):
            layout.addWidget(button)
        self.setStyleSheet(TITLE_STYLE)
        self.set_history(False, False)

    def set_history(self, can_back: bool, can_forward: bool) -> None:
        self.back_button.setEnabled(can_back)
        self.forward_button.setEnabled(can_forward)

    def toggle_maximised(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
            self.max_button.setText("☐")
        else:
            self._window.showMaximized()
            self.max_button.setText("❐")

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        grad = QLinearGradient(QPointF(0, 0), QPointF(self.width(), 0))
        grad.setColorAt(0.0, QColor("#2a0306"))
        grad.setColorAt(0.28, QColor("#120203"))
        grad.setColorAt(0.5, QColor("#050505"))
        grad.setColorAt(1.0, QColor("#0b0b0d"))
        painter.fillRect(self.rect(), grad)
        painter.end()

    # Drag to move / double-click to maximise. startSystemMove works on Wayland too.
    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximised()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class AccentStrip(QWidget):
    """Thin purple glow line under the title bar."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(3)

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        grad = QLinearGradient(QPointF(0, 0), QPointF(self.width(), 0))
        grad.setColorAt(0.0, QColor("#7a2fe0"))
        grad.setColorAt(0.5, QColor("#a35bff"))
        grad.setColorAt(1.0, QColor("#7a2fe0"))
        painter.fillRect(self.rect(), grad)
        painter.end()


__all__ = ["AccentStrip", "IslandState", "IslandTitleBar", "StatusIsland"]
