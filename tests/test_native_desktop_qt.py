"""Optional graphical smoke validation when PySide6 is installed."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_qt_native_window_builds_offscreen_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from max_core.native_desktop.qt_ui import launch

    assert launch(smoke_test=True) == 0


def test_qt_native_window_enables_tasks_with_local_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: default SQLite must not be treated as missing PostgreSQL."""
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from max_core.native_desktop.backend import NativeDesktopCore
    from max_core.native_desktop.qt_ui import MaxWindow

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    core = NativeDesktopCore(
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'window.sqlite3').as_posix()}"
    )
    window = MaxWindow(core)
    try:
        first = core.snapshot()
        assert first.storage_status == "Local SQLite ready"
        window._show_snapshot(first)
        assert window.queue_button.isEnabled()
        task = core.create_task("SQLite task visible in native Qt")
        window._show_snapshot(core.snapshot())
        assert window.stat_tasks.text() == "1"
        assert window.task_list.count() == 1
        assert task["id"] == window.task_list.item(0).data(Qt.ItemDataRole.UserRole)
    finally:
        window.close()


def _window(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from max_core.native_desktop.backend import NativeDesktopCore
    from max_core.native_desktop.qt_ui import MaxWindow

    app = QApplication.instance() or QApplication([])
    return app, MaxWindow(NativeDesktopCore.from_env())


def test_title_bar_island_is_idle_and_never_claims_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, window = _window(monkeypatch)
    from PySide6.QtCore import Qt

    from max_core.native_desktop.title_bar import IslandState

    island = window.title_bar.island
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert island.state == IslandState.IDLE and not island.wave.active
    window.toggle_quiet()
    assert island.state == IslandState.QUIET
    window.toggle_quiet()
    assert island.state == IslandState.IDLE
    island.set_state(IslandState.LISTENING)  # only an audio source may do this
    assert island.label.text() == "LISTENING" and island.wave.active
    window.close()
    app.processEvents()


def test_title_bar_back_forward_history(monkeypatch: pytest.MonkeyPatch) -> None:
    app, window = _window(monkeypatch)
    bar = window.title_bar
    assert not bar.back_button.isEnabled() and not bar.forward_button.isEnabled()
    window.switch(1)
    window.switch(3)
    window.go_back()
    assert window.stack.currentIndex() == 1 and bar.forward_button.isEnabled()
    window.go_back()
    assert window.stack.currentIndex() == 0 and not bar.back_button.isEnabled()
    window.go_forward()
    assert window.stack.currentIndex() == 1
    window.switch(2)  # new branch drops forward history
    assert not bar.forward_button.isEnabled()
    bar.inbox.emit()
    assert window.stack.currentIndex() == 2
    window.close()
    app.processEvents()


def test_system_frame_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_NATIVE_SYSTEM_FRAME", "yes")
    app, window = _window(monkeypatch)
    from PySide6.QtCore import Qt

    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.title_bar.close_button.isHidden()
    window.close()
    app.processEvents()
