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
