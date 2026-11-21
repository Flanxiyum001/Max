"""Offscreen checks for the orb interface (skipped without PySide6)."""

from __future__ import annotations

import pytest


@pytest.fixture
def orb(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from max_core.native_desktop.backend import NativeDesktopCore
    from max_core.native_desktop.orb_ui import OrbWindow

    app = QApplication.instance() or QApplication([])
    window = OrbWindow(NativeDesktopCore.from_env())
    window.show()
    yield app, window
    window.close()
    app.processEvents()


def test_orb_smoke_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from max_core.native_desktop.orb_ui import launch_orb

    assert launch_orb(smoke_test=True) == 0


def test_orb_is_the_whole_ui(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    from PySide6.QtCore import Qt

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.orb.state == "idle"
    assert window.input.isHidden() and window.answer.text() == ""


def test_typing_reveals_input_and_escape_clears(orb) -> None:  # type: ignore[no-untyped-def]
    app, window = orb
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.keyClicks(window, "h")
    assert window.input.isVisible() and window.input.text() == "h"
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert window.input.text() == "" and window.input.isHidden()


def test_unconfigured_model_fails_honestly(orb, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    app, window = orb
    for key in ("MAX_TRIAL_DESTINATION", "MAX_TRIAL_MODEL", "MAX_TRIAL_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    window.input.setText("hello")
    window.send()
    assert window.orb.state == "thinking"
    window._pool.waitForDone()
    for _ in range(50):
        app.processEvents()
    assert window.orb.state == "error"
    assert "No model connected" in window.answer.text()


def test_previous_answer_cannot_hide_new_answer(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    window._show_answer("first response")
    assert window._hide_timer.isActive()
    window._show_answer(None)
    assert not window._hide_timer.isActive()
    window._show_answer("second response")
    assert window.answer.text() == "second response"
    assert window._hide_timer.isActive()


def test_close_is_deferred_during_active_local_call(orb) -> None:  # type: ignore[no-untyped-def]
    app, window = orb
    window._busy = True
    assert not window.close()  # no blocking call to the worker thread
    assert window.isVisible()
    assert window._close_requested
    window._busy = False
    window._failed("unavailable")
    app.processEvents()
    assert not window.isVisible()
    assert window.core._closed


def test_idle_window_close_releases_local_core(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    assert not window.core._closed
    assert window.close()
    assert window.core._closed
    assert window.core.engine is None


def test_new_reply_restarts_idle_timer(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    window._failed("unavailable")
    assert window._state_timer.isActive() and window.orb.state == "error"
    window._state_timer.stop()
    window._idle_after_reply()
    assert window.orb.state == "idle"


def test_model_tool_proposal_is_never_reported_as_executed(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    from max_core.native_desktop.backend import LocalModelReply

    window._busy = True
    window._done(LocalModelReply("review_required", "I modified the source code", ("shell.run",)))
    assert window.orb.state == "error"
    assert window.answer.text() == (
        "The model proposed tool actions. Nothing was executed; review required."
    )
    assert "modified the source code" not in window.answer.text()


class _FakeSession:
    """Stands in for AssistantSession: asks approval for one tool, then answers."""

    def __init__(self) -> None:
        self.decisions: list[bool] = []
        self.closed = False

    def ask(self, text, *, approve=None, on_step=None):  # type: ignore[no-untyped-def]
        from max_core.tool_loop import LoopResult

        self.decisions.append(approve("apps.open", {"target": "https://example.org"}))
        return LoopResult(answer="opened" if self.decisions[-1] else "not opened")

    def due_reminders(self):  # type: ignore[no-untyped-def]
        return [{"text": "stretch"}]

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _orb_with(monkeypatch: pytest.MonkeyPatch, session: _FakeSession):  # type: ignore[no-untyped-def]
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from max_core.native_desktop.backend import NativeDesktopCore
    from max_core.native_desktop.orb_ui import OrbWindow

    app = QApplication.instance() or QApplication([])
    window = OrbWindow(NativeDesktopCore.from_env(), session_factory=lambda: session)
    window.show()
    return app, window


@pytest.mark.parametrize(("key", "allowed"), [("Return", True), ("Escape", False)])
def test_orb_asks_owner_before_effectful_tool(
    monkeypatch: pytest.MonkeyPatch, key: str, allowed: bool
) -> None:
    session = _FakeSession()
    app, window = _orb_with(monkeypatch, session)
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.input.setText("open example")
    window.send()
    for _ in range(200):
        app.processEvents()
        if window._pending_approval is not None:
            break
        QTest.qWait(5)
    assert "MAX wants to run apps.open" in window.answer.text()
    QTest.keyClick(window, getattr(Qt.Key, f"Key_{key}"))
    window._pool.waitForDone()
    for _ in range(50):
        app.processEvents()
    assert session.decisions == [allowed]
    assert window.answer.text() == ("opened" if allowed else "not opened")
    window.close()
    assert session.closed


def test_orb_announces_due_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    app, window = _orb_with(monkeypatch, _FakeSession())
    window._check_reminders()
    assert window.answer.text() == "Reminder: stretch"
    window.close()
