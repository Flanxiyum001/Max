# mypy: ignore-errors
"""Original MAX Qt Widgets desktop application.

PySide6 is optional. This module is manually smoke-tested in a dedicated Qt CI
job and never imported by the regular headless MAX Core/server process.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
    QShortcut,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from max_core.native_desktop.backend import (
    DesktopSnapshot,
    NativeDesktopCore,
    NativeDesktopError,
)

STYLE = """
QWidget { color: #eae8f3; font-family: "Segoe UI", "Inter", sans-serif; font-size: 12px; }
QWidget#root { background: #090a12; }
QFrame#sidebar { background: #10101a; border-right: 1px solid #292437; }
QFrame#header { background: #0c0d16; border-bottom: 1px solid #282538; }
QFrame#hero { background: #221a35; border: 1px solid #5f437d; border-radius: 20px; }
QFrame#panel, QFrame#stat, QFrame#commandPanel {
    background: #151521; border: 1px solid #302b3e; border-radius: 14px;
}
QFrame#warning { background: #292116; border: 1px solid #78603c; border-radius: 12px; }
QLabel#brand { color: #f7eeff; font-size: 21px; font-weight: 850; letter-spacing: 4px; }
QLabel#eyebrow { color: #c19afa; font-size: 10px; font-weight: 700; }
QLabel#heading { color: #f7f2ff; font-size: 30px; font-weight: 700; }
QLabel#heroTitle { color: #f7efff; font-size: 25px; font-weight: 700; }
QLabel#subtle { color: #a49aae; font-size: 12px; }
QLabel#statValue { color: #f3e9ff; font-size: 22px; font-weight: 700; }
QLabel#sectionTitle { font-size: 17px; font-weight: 700; }
QLabel#status { color: #ae91da; font-size: 11px; }
QLabel#sidebarNote { color: #9891a8; font-size: 11px; }
QPushButton { border-radius: 10px; padding: 11px 17px; font-weight: 650; }
QPushButton#nav { text-align: left; border: 1px solid transparent;
                  background: transparent; color: #a69eb4; }
QPushButton#nav:hover { background: #282039; color: #f3eaff; }
QPushButton#nav[active="true"] { background: #382b53; color: #f0daff;
                                 border: 1px solid #715291; }
QPushButton#primary { background: #b89af1; color: #1b1126; border: none; }
QPushButton#primary:hover { background: #d0b4ff; }
QPushButton#secondary { background: #29243a; border: 1px solid #4a3c60; }
QPushButton#secondary:hover { background: #39304b; }
QPushButton:disabled { color: #716a80; background: #272332; }
QPlainTextEdit, QListWidget { background: #0e0f1b; border: 1px solid #3a3248;
                               border-radius: 10px; padding: 10px; selection-background-color: #594373; }
QPlainTextEdit:focus, QListWidget:focus { border: 1px solid #af84eb; }
QListWidget::item { padding: 11px; border-bottom: 1px solid #292337; }
QListWidget::item:selected { background: #403159; color: #fff; border-radius: 8px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: #12111c; width: 9px; }
QScrollBar::handle:vertical { background: #544260; border-radius: 4px; min-height: 20px; }
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
    layout.setContentsMargins(23, 20, 23, 20)
    layout.setSpacing(12)
    return panel, layout


def _icon() -> QIcon:
    image = QPixmap(64, 64)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#986fdf"))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 16, 16)
    painter.setPen(QColor("#ffffff"))
    font = QFont("Segoe UI", 29)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "M")
    painter.end()
    return QIcon(image)


class Orb(QWidget):
    """Decorative only. No microphone, tool or thinking-state claim."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(225, 235)
        self.phase = 0.0
        self.quiet = False
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

    def _tick(self) -> None:
        self.phase = (self.phase + 0.7) % 360
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = self.width() / 2
        cy = self.height() / 2 - 5
        radius = min(self.width(), self.height()) * 0.31
        painter.setPen(Qt.PenStyle.NoPen)
        glow = QRadialGradient(QPointF(cx, cy), radius * 1.9)
        glow.setColorAt(0, QColor(158, 102, 241, 110 if not self.quiet else 25))
        glow.setColorAt(1, QColor(70, 32, 111, 0))
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(cx, cy), radius * 1.9, radius * 1.9)

        for tilt, shade in ((-32, "#ae86ff"), (35, "#7777f2")):
            painter.save()
            painter.translate(cx, cy)
            painter.rotate(tilt + (self.phase * (0.15 if tilt < 0 else -0.11)))
            pen = QPen(QColor(shade))
            pen.setWidthF(1.9)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(-radius * 1.56, -radius * 0.58,
                                       radius * 3.12, radius * 1.16))
            painter.restore()

        sphere = QRadialGradient(QPointF(cx - radius * 0.5, cy - radius * 0.5),
                                 radius * 1.95)
        sphere.setColorAt(0, QColor("#f5e7ff"))
        sphere.setColorAt(0.20, QColor("#bf9dff"))
        sphere.setColorAt(0.47, QColor("#7455c2"))
        sphere.setColorAt(0.83, QColor("#251839"))
        sphere.setColorAt(1, QColor("#100e1c"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(sphere)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.setPen(QColor("#b9a0d9"))
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(QRectF(0, self.height() - 28, self.width(), 22),
                         Qt.AlignmentFlag.AlignCenter, "MAX / STANDBY")
        painter.end()


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
    """Always user-visible, manual-only MAX desktop command centre."""

    def __init__(self, core: NativeDesktopCore) -> None:
        super().__init__()
        self.core = core
        self.setWindowTitle("MAX — Personal Desktop Companion")
        self.setWindowIcon(_icon())
        self.resize(1240, 790)
        self.setMinimumSize(860, 580)
        self.setStyleSheet(STYLE)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._running: dict[int, WorkItem] = {}
        self._sequence = 0
        self._latest: dict[str, int] = {}
        self._closing = False
        self._store_ready = False
        self._quiet = False
        self._task_ids: list[str] = []

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
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
        side.addSpacing(37)
        side.addWidget(_label("WORKSPACE", "eyebrow"))
        self.nav: list[QPushButton] = []
        for index, name in enumerate(("Overview", "Tasks", "Trust", "System")):
            button = _button(f"{('◈', '▤', '⬡', '⌘')[index]}   {name}", "nav")
            button.setProperty("active", "true" if index == 0 else "false")
            button.clicked.connect(lambda _checked=False, n=index: self.switch(n))
            side.addWidget(button)
            self.nav.append(button)
        side.addStretch(1)
        side.addWidget(_label("✦  PRIVATE LOCAL PREVIEW", "eyebrow"))
        side.addWidget(_label(
            "No remote listener. No computer-control permissions. "
            "No model or voice provider activated.", "sidebarNote"))
        side.addSpacing(15)
        side.addWidget(_label("MAX CORE  •  NATIVE DEV BUILD", "sidebarNote"))
        frame.addWidget(sidebar)

        right = QWidget()
        body = QVBoxLayout(right)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        top = QFrame()
        top.setObjectName("header")
        header = QHBoxLayout(top)
        header.setContentsMargins(31, 17, 31, 17)
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
        self.command.setPlaceholderText("Try /help, /status, /tasks, /trust or /task ...")
        self.command.setFixedHeight(43)
        self.command.setTabChangesFocus(True)
        self.command.installEventFilter(self)
        form.addWidget(self.command, 1)
        send = _button("Run ↵", "primary")
        send.clicked.connect(self.run_command)
        form.addWidget(send)
        footer_layout.addLayout(form)
        body.addWidget(footer)
        self.notice = _label("Local command deck only. AI chat and microphone are not connected.",
                             "sidebarNote")
        self.notice.setContentsMargins(20, 3, 15, 12)
        body.addWidget(self.notice)
        frame.addWidget(right, 1)

        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.command.setFocus)
        for i in range(4):
            QShortcut(QKeySequence(f"Alt+{i + 1}"), self,
                      activated=lambda number=i: self.switch(number))
        QTimer.singleShot(50, self.refresh)

    def _scroll(self, page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _base_page(self, eyebrow: str, title: str, description: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 27, 32, 32)
        layout.setSpacing(18)
        layout.addWidget(_label(eyebrow, "eyebrow"))
        layout.addWidget(_label(title, "heading"))
        layout.addWidget(_label(description, "subtle"))
        return page, layout

    def build_overview(self) -> QWidget:
        page, layout = self._base_page(
            "YOUR PERSONAL COMMAND CENTRE", "Good to have you here.",
            "Your assistant's real state. No pretend task execution or fictional abilities."
        )
        hero = QFrame()
        hero.setObjectName("hero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(30, 20, 23, 20)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(11)
        hero_text.addWidget(_label("✦  MAX PRESENCE  /  LOCAL MODE", "eyebrow"))
        hero_text.addWidget(_label("Ready to organise.\nNot pretending to automate.", "heroTitle"))
        hero_text.addWidget(_label(
            "This desktop companion is a native application. "
            "You can inspect task records and Trust requests; AI and computer-control "
            "features are still gated.", "subtle"))
        actions = QHBoxLayout()
        open_tasks = _button("Open task queue ↗", "primary")
        open_tasks.clicked.connect(lambda: self.switch(1))
        actions.addWidget(open_tasks)
        check_status = _button("System status")
        check_status.clicked.connect(lambda: self.switch(3))
        actions.addWidget(check_status)
        actions.addStretch(1)
        hero_text.addLayout(actions)
        hero_text.addStretch(1)
        hero_layout.addLayout(hero_text, 2)
        self.orb = Orb()
        hero_layout.addWidget(self.orb, 1)
        layout.addWidget(hero)

        stats = QHBoxLayout()
        self.stat_core = self._stat(stats, "CORE STATUS", "STARTING", "Private in-process")
        self.stat_tasks = self._stat(stats, "TRACKED TASKS", "—", "Requires PostgreSQL")
        self.stat_trust = self._stat(stats, "PENDING TRUST", "—", "Read-only inbox")
        self._stat(stats, "MODEL EXECUTION", "NOT WIRED", "Not an AI chat client")
        layout.addLayout(stats)

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
        trust_layout.addWidget(_label(
            "Model suggestions are never treated as owner approval.", "subtle"))
        trust_layout.addStretch(1)
        sections.addWidget(trust_box, 1)
        layout.addLayout(sections)
        layout.addStretch(1)
        return page

    def _stat(self, layout: QHBoxLayout, name: str, value: str, sub: str) -> QLabel:
        panel = QFrame()
        panel.setObjectName("stat")
        box = QVBoxLayout(panel)
        box.setContentsMargins(16, 16, 16, 15)
        box.addWidget(_label(name, "eyebrow"))
        count = _label(value, "statValue")
        box.addWidget(count)
        box.addWidget(_label(sub, "subtle"))
        layout.addWidget(panel, 1)
        return count

    def build_tasks(self) -> QWidget:
        page, layout = self._base_page(
            "WORKSPACE  /  TRACKING", "Your task journal.",
            "Queue a tracked objective, not an AI command. Nothing runs automatically."
        )
        composer, compose_layout = _panel()
        compose_layout.addWidget(_label("NEW TRACKED TASK", "eyebrow"))
        self.objective = QPlainTextEdit()
        self.objective.setPlaceholderText(
            "Describe work to track. This does not execute a model or operate your PC."
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
        box2.addWidget(_label(
            "Metadata only. Events are not proof that a model or tool ran.", "subtle"))
        self.events_list = QListWidget()
        self.events_list.setMinimumHeight(220)
        box2.addWidget(self.events_list, 1)
        row.addWidget(panel2, 1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def build_trust(self) -> QWidget:
        page, layout = self._base_page(
            "OWNER CONTROL  /  TRUST", "Every action accountable.",
            "Review pending requests. This screen cannot approve actions."
        )
        box = QFrame()
        box.setObjectName("warning")
        warning = QVBoxLayout(box)
        warning.setContentsMargins(20, 15, 20, 15)
        warning.addWidget(_label("⬡  REVIEW ONLY — NEVER AN APPROVAL SURFACE", "eyebrow"))
        warning.addWidget(_label(
            "High-impact actions need independent, owner-verified authorization. "
            "The native preview does not send approval decisions.", "subtle"))
        layout.addWidget(box)
        panel, inside = _panel()
        inside.addWidget(_label("PENDING TRUST REQUESTS", "eyebrow"))
        self.trust_list = QListWidget()
        self.trust_list.setMinimumHeight(270)
        inside.addWidget(self.trust_list)
        layout.addWidget(panel)
        layout.addStretch(1)
        return page

    def build_system(self) -> QWidget:
        page, layout = self._base_page(
            "LOCAL OPERATIONS  /  STATUS", "Know what really works.",
            "Status is separated from promises, demos and future features."
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
        for item in (
            "Native desktop window  —  ACTIVE",
            "Task journal  —  requires migrated local PostgreSQL",
            "Trust inbox  —  read-only; no owner decisions",
            "Voice / microphone  —  not connected",
            "LLM conversations  —  not connected",
            "OS control and MCP effects  —  not enabled",
            "Global hotkeys and gaming overlay  —  not implemented",
        ):
            available.addWidget(_label("◇  " + item, "subtle"))
        layout.addWidget(capabilities)
        layout.addStretch(1)
        return page

    def switch(self, index: int) -> None:
        if not 0 <= index < self.stack.count():
            return
        self.stack.setCurrentIndex(index)
        self.crumb.setText("MAX  /  " + ("Overview", "Tasks", "Trust", "System")[index])
        for n, button in enumerate(self.nav):
            button.setProperty("active", "true" if n == index else "false")
            button.style().unpolish(button)
            button.style().polish(button)

    def toggle_quiet(self) -> None:
        self._quiet = not self._quiet
        self.orb.set_quiet(self._quiet)
        self.focus_button.setText("Quiet mode: on" if self._quiet else "Quiet mode: off")
        self.notice.setText(
            "Quiet mode limits animation in this window; it does not control OS notifications."
            if self._quiet else
            "Local command deck only. AI chat and microphone are not connected."
        )

    def eventFilter(self, watched: QObject, event: Any) -> bool:
        if watched is self.command and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.run_command()
                    return True
        return super().eventFilter(watched, event)

    def run_command(self) -> None:
        raw = self.command.toPlainText().strip()
        self.command.clear()
        lower = raw.lower()
        if lower == "/help":
            self.notice.setText(
                "Commands: /status, /tasks, /trust, /help, /task <objective>. No AI chat."
            )
        elif lower == "/status":
            self.switch(3)
            self.refresh()
        elif lower == "/tasks":
            self.switch(1)
        elif lower == "/trust":
            self.switch(2)
        elif lower.startswith("/task "):
            objective = raw[6:].strip()
            if not 1 <= len(objective) <= 500:
                self.notice.setText("Task descriptions must have 1–500 characters.")
                return
            self.objective.setPlainText(objective)
            self.switch(1)
            self.objective.setFocus()
            self.notice.setText("Review the text and choose Queue task. Nothing runs yet.")
        elif raw:
            self.notice.setText("No live AI chat yet. Type /help for local commands.")

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
                "Connect a migrated local PostgreSQL database before creating tasks."
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

    def _done(self, sequence: int, action: str, value: Any) -> None:
        self._running.pop(sequence, None)
        if self._closing or self._latest.get(action) != sequence:
            return
        if action == "snapshot":
            self._show_snapshot(value)
        elif action == "create":
            self.objective.clear()
            self.queue_button.setEnabled(self._store_ready)
            self.notice.setText(
                "Task recorded. No AI, shell command or computer action was executed."
            )
            self.refresh()
        elif action == "timeline":
            self.events_list.clear()
            for event in value:
                kind = str(event.get("kind", "event"))[:90]
                revision = str(event.get("task_revision", "?"))
                self.events_list.addItem(f"{kind}    •    Revision {revision}")
            if not value:
                self.events_list.addItem("No recorded events.")

    def _failed(self, sequence: int, action: str, code: str) -> None:
        self._running.pop(sequence, None)
        if self._closing or self._latest.get(action) != sequence:
            return
        messages = {
            "task_store_not_ready": "Local database is not ready.",
            "invalid_task_description": "Provide a 1–500 character description.",
            "invalid_task_identifier": "Invalid task identifier.",
            "local_postgresql_required": "Only a local PostgreSQL database is permitted.",
        }
        message = messages.get(code, "Local Core operation unavailable.")
        self.notice.setText(message)
        if action == "snapshot":
            self.online.setText("●  CORE NOT READY")
            self.system_core.setText("Core: unavailable")
            self.system_storage.setText("Verify your local development database.")
        if action == "create":
            self.queue_button.setEnabled(self._store_ready)
        if action == "timeline":
            self.events_list.clear()
            self.events_list.addItem(message)

    def _show_snapshot(self, snapshot: DesktopSnapshot) -> None:
        self.online.setText("●  LOCAL CORE ACTIVE")
        self.system_core.setText("Core: " + snapshot.core_status)
        self.system_storage.setText("Storage: " + snapshot.storage_status)
        self._store_ready = snapshot.storage_status == "Local PostgreSQL ready"
        self.queue_button.setEnabled(self._store_ready)
        self.stat_core.setText("ONLINE")
        self.stat_tasks.setText(
            str(len(snapshot.tasks)) if self._store_ready else "—"
        )
        self.stat_trust.setText(
            str(len(snapshot.approvals)) if self._store_ready else "—"
        )
        self.task_list.clear()
        if not self._store_ready:
            self.recent_tasks.setText(
                "No database configured. Explore the interface without creating tasks."
            )
            self.recent_trust.setText(
                "Trust storage unavailable until PostgreSQL is configured."
            )
            self.task_list.addItem("Task journal disabled: configure local PostgreSQL.")
            self.trust_list.clear()
            self.trust_list.addItem("No Trust database connected.")
            return
        tasks = snapshot.tasks[:30]
        self.recent_tasks.setText(
            "\n".join(
                f"• {str(task.get('objective', 'Task'))[:70]}"
                for task in tasks[:3]
            ) if tasks else "No recorded tasks yet."
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
            f"{len(snapshot.approvals)} pending request(s), read-only."
        )
        for approval in snapshot.approvals[:50]:
            tool = str(approval.get("tool_name", "Unknown tool"))[:80]
            state = str(approval.get("state", "pending"))[:30]
            self.trust_list.addItem(f"{tool}    •    {state.upper()}")
        if not snapshot.approvals:
            self.trust_list.addItem("No pending Trust requests.")

    def closeEvent(self, event: Any) -> None:
        self._closing = True
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
