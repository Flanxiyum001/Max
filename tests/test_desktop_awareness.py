from pathlib import Path

import pytest

from max_core import assistant_tools, desktop_awareness
from max_core.assistant_tools import assistant_registry
from max_core.tools import ToolError, ToolRisk


def test_desktop_tools_local_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_tools.sys, "platform", "win32")
    for cloud, host_tools in ((True, True), (False, False), (True, False)):
        registry = assistant_registry(
            data_dir=tmp_path, cloud=cloud, cloud_personal_tools=True, host_tools=host_tools
        )
        assert registry.get("desktop.list_windows") is None
        assert registry.get("system.gpu") is None
    local = assistant_registry(data_dir=tmp_path)
    for name in ("desktop.active_window", "desktop.list_windows", "desktop.focus_window"):
        tool = local.get(name)
        assert tool is not None and tool.risk == ToolRisk.EFFECTFUL
    gpu = local.get("system.gpu")
    assert gpu is not None and gpu.risk == ToolRisk.READ_ONLY


def test_focus_rejects_changed_title(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        def IsWindow(self, hwnd: int) -> bool:
            return True

        def IsWindowVisible(self, hwnd: int) -> bool:
            return True

        def GetWindowTextW(self, hwnd: int, buffer, size: int) -> int:
            buffer.value = "Changed"
            return 7

        def SetForegroundWindow(self, hwnd: int) -> bool:
            pytest.fail("Must not focus stale selection")

    monkeypatch.setattr(desktop_awareness, "_user32", Fake)
    with pytest.raises(ToolError, match="window_changed"):
        desktop_awareness.focus_window({"handle": 42, "title": "Original"})


def test_focus_verifies_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        def IsWindow(self, hwnd: int) -> bool:
            return True

        def IsWindowVisible(self, hwnd: int) -> bool:
            return True

        def GetWindowTextW(self, hwnd: int, buffer, size: int) -> int:
            buffer.value = "Editor"
            return 6

        def SetForegroundWindow(self, hwnd: int) -> bool:
            return True

        def GetForegroundWindow(self) -> int:
            return 99

    monkeypatch.setattr(desktop_awareness, "_user32", Fake)
    with pytest.raises(ToolError, match="window_focus_denied"):
        desktop_awareness.focus_window({"handle": 42, "title": "Editor"})
