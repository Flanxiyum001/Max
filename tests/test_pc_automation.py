"""Windows app workflows must require a fresh, explicit local click."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from max.config import Config
from max.pc.automation import ApprovalQueue, WindowsAction, parse_action, WindowsAdapter
from max.pc.control import PCController
from max.pc.web_app import PCBridge


def test_intents_are_strict_and_bounded():
    assert parse_action("Type into notepad: My TODO list") == WindowsAction(
        "type", "notepad", "My TODO list"
    )
    assert parse_action("press ctrl+shift+s in vscode") == WindowsAction(
        "shortcut", "vscode", "ctrl+shift+s"
    )
    assert parse_action("switch to Chrome") == WindowsAction("focus", "chrome")
    assert parse_action("type into notepad: A\nB") is None
    assert parse_action("type into unknown: hello") is None
    assert parse_action("press alt+f4 in chrome") is None
    assert parse_action("type into notepad: " + "x" * 501) is None
    assert parse_action("rm -rf /") is None


class FakeAdapter:
    def __init__(self):
        self.executed = []
    def execute(self, plan):
        self.executed.append(plan)
        return f"Completed {plan.operation} in {plan.application}."


def test_one_shot_and_expired_approval():
    adapter = FakeAdapter()
    now = [100.0]
    queue = ApprovalQueue(adapter=adapter, now=lambda: now[0], ttl_seconds=30)
    p = queue.plan(WindowsAction("focus", "notepad"))
    ok, _ = queue.approve("not-the-token", awake=True)
    assert not ok and adapter.executed == []  # invalid ID consumes the pending plan
    assert not queue.approve(p["id"], awake=True)[0]
    p = queue.plan(WindowsAction("shortcut", "vscode", "ctrl+s"))
    assert queue.approve(p["id"], awake=False)[0] is False
    p = queue.plan(WindowsAction("focus", "notepad"))
    now[0] += 31
    assert queue.approve(p["id"], awake=True)[0] is False
    p = queue.plan(WindowsAction("focus", "notepad"))
    assert queue.approve(p["id"], awake=True)[0]
    assert len(adapter.executed) == 1
    assert not queue.approve(p["id"], awake=True)[0]


def test_windows_adapter_targets_approved_visible_window():
    title = SimpleNamespace(title="notes - Notepad", _hWnd=12,
                            isMinimized=False, activate=lambda: None)
    gui = SimpleNamespace(getAllWindows=lambda: [title],
                          getActiveWindow=lambda: title)
    typed = []
    keys = []
    auto = SimpleNamespace(
        FAILSAFE=False,
        write=lambda text, interval: typed.append((text, interval)),
        hotkey=lambda *parts: keys.append(parts),
    )
    adapter = WindowsAdapter(pyauto=auto, windows=gui)
    assert "Completed type" in adapter.execute(WindowsAction("type", "notepad", "Hello"))
    assert typed == [("Hello", .008)]
    assert auto.FAILSAFE is True
    assert "Completed shortcut" in adapter.execute(WindowsAction("shortcut", "notepad", "ctrl+s"))
    assert keys == [("ctrl", "s")]


def test_bridge_never_executes_without_local_approval(tmp_path):
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path, provider="mock")
    bridge = PCBridge(PCController(tmp_path), config=cfg, speaker=lambda x: False)
    adapter = FakeAdapter()
    bridge._approvals = ApprovalQueue(adapter=adapter)
    assert bridge.submit("type into notepad: private text")["action"] == "sleeping"
    bridge.submit("hi max")
    p = bridge.submit("type into notepad: a safe note")
    assert p["action"] == "approval"
    assert p["approval"]["application"] == "notepad"
    assert "a safe note" in p["approval"]["preview"]
    assert adapter.executed == []
    assert bridge.approve_action(p["approval"]["id"])["ok"]
    assert len(adapter.executed) == 1
    assert not bridge.approve_action(p["approval"]["id"])["ok"]


def test_sleep_and_cancel_revoke_pending_actions(tmp_path):
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path, provider="mock")
    bridge = PCBridge(PCController(tmp_path), config=cfg)
    bridge._approvals = ApprovalQueue(adapter=FakeAdapter())
    bridge.submit("hi max")
    p = bridge.submit("press ctrl+s in vscode")["approval"]
    bridge.submit("bye max")
    assert bridge.approve_action(p["id"])["ok"] is False
    bridge.submit("hi max")
    p = bridge.submit("switch to notepad")["approval"]
    bridge.deny_action()
    assert not bridge.approve_action(p["id"])["ok"]



def test_focus_steal_blocks_keyboard_injection():
    target = SimpleNamespace(title="Notepad", _hWnd=10, isMinimized=False,
                             activate=lambda: None)
    other = SimpleNamespace(title="Password manager", _hWnd=20)
    windows = SimpleNamespace(getAllWindows=lambda: [target],
                              getActiveWindow=lambda: other)
    typed = []
    auto = SimpleNamespace(FAILSAFE=False,
                           write=lambda *a, **k: typed.append(a),
                           hotkey=lambda *a: typed.append(a))
    adapter = WindowsAdapter(pyauto=auto, windows=windows)
    result = adapter.execute(WindowsAction("type", "notepad", "Hello"))
    assert "Nothing was typed" in result
    assert typed == []
