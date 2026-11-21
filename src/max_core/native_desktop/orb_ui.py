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

import math
import sys
from typing import Any

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
    QVBoxLayout,
    QWidget,
)

from max_core.native_desktop.backend import (
    LocalModelReply,
    NativeDesktopCore,
    NativeDesktopError,
)

IDLE, THINKING, SPEAKING, ERROR = "idle", "thinking", "speaking", "error"


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
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, state: str) -> None:
        self.state = state

    def _tick(self) -> None:
        target = {IDLE: 0.0, THINKING: 1.0, SPEAKING: 0.6, ERROR: 0.2}[self.state]
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


class _Ask(QRunnable):
    def __init__(self, core: NativeDesktopCore, prompt: str) -> None:
        super().__init__()
        self.core, self.prompt, self.signals = core, prompt, _Signals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.core.ask_local(self.prompt))
        except NativeDesktopError as exc:
            self.signals.failed.emit(exc.code)
        except Exception:
            self.signals.failed.emit("unavailable")


_ERRORS = {
    "local_model_not_configured": "No model connected yet. Set the MAX_TRIAL_* settings.",
    "local_model_only": "This window only talks to a local model.",
    "invalid_model_prompt": "Say that again, a bit shorter.",
}


class OrbWindow(QWidget):
    open_dashboard = Signal()

    def __init__(self, core: NativeDesktopCore) -> None:
        super().__init__()
        self.core = core
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

    # --- conversation -------------------------------------------------------
    def send(self) -> None:
        prompt = self.input.text().strip()
        if not prompt or self._busy or self._closing:
            return
        self.input.clear()
        self.setFocus()
        self._busy = True
        self.orb.set_state(THINKING)
        self._show_answer(None)
        job = _Ask(self.core, prompt)
        job.signals.done.connect(self._done)
        job.signals.failed.connect(self._failed)
        self._job = job
        self._pool.start(job)

    def _done(self, reply: LocalModelReply) -> None:
        self._busy = False
        if self._closing:
            return
        if reply.state == "failed":
            self._failed(reply.error_code or "unavailable")
            return
        self.orb.set_state(SPEAKING)
        QTimer.singleShot(2500, lambda: self.orb.set_state(IDLE) if not self._busy else None)
        self._show_answer(reply.text.strip() or "…")

    def _failed(self, code: str) -> None:
        self._busy = False
        if self._closing:
            return
        self.orb.set_state(ERROR)
        QTimer.singleShot(2500, lambda: self.orb.set_state(IDLE) if not self._busy else None)
        self._show_answer(_ERRORS.get(code, "I couldn't reach my model just now."))

    def _show_answer(self, text: str | None) -> None:
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
        menu.addSeparator()
        close = menu.addAction("Close MAX")
        chosen = menu.exec(event.globalPos())
        if chosen is dash:
            self.open_dashboard.emit()
        elif chosen is close:
            self.close()

    def closeEvent(self, event: Any) -> None:
        self._closing = True
        self._pool.waitForDone()
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
    code = app.exec()
    core.close()
    return code


__all__ = ["OrbView", "OrbWindow", "launch_orb"]
