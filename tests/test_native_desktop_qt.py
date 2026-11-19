"""Optional graphical smoke validation when PySide6 is installed."""

from __future__ import annotations

import pytest


def test_qt_native_window_builds_offscreen_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from max_core.native_desktop.qt_ui import launch

    assert launch(smoke_test=True) == 0
