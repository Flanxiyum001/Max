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


def test_unconfigured_model_fails_honestly(orb) -> None:  # type: ignore[no-untyped-def]
    app, window = orb
    for key in ("MAX_TRIAL_DESTINATION", "MAX_TRIAL_MODEL", "MAX_TRIAL_ENDPOINT"):
        import os

        os.environ.pop(key, None)
    window.input.setText("hello")
    window.send()
    assert window.orb.state == "thinking"
    window._pool.waitForDone()
    for _ in range(50):
        app.processEvents()
    assert window.orb.state == "error"
    assert "No model connected" in window.answer.text()
