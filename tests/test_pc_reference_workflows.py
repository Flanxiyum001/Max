"""OpenJarvis-inspired local-first command workflow regressions.

Desktop actions require an explicit on-device confirmation, even for natural
speech commands addressed to Max. No provider call can authorize execution.
"""
from __future__ import annotations

from max.config import Config
from max.pc.automation import ApprovalQueue, WindowsAction, parse_action
from max.pc.control import PCController
from max.pc.web_app import PCBridge


class Recorder:
    def __init__(self):
        self.actions = []

    def execute(self, action):
        self.actions.append(action)
        return f"Completed {action.operation} in {action.application}."


def make_bridge(tmp_path):
    bridge = PCBridge(
        PCController(tmp_path),
        config=Config(repo_root=tmp_path, data_dir=tmp_path, provider="mock"),
        speaker=lambda _: None,
    )
    recorder = Recorder()
    bridge._approvals = ApprovalQueue(adapter=recorder)
    return bridge, recorder


def test_spoken_name_and_premiere_aliases():
    assert parse_action("Max, switch to Adobe Premiere Pro") == WindowsAction(
        "focus", "premiere"
    )
    assert parse_action("hey Max, please play the timeline in Premiere Pro") == WindowsAction(
        "shortcut", "premiere", "space"
    )
    assert parse_action("MAX, type into Notepad: This is my note!") == WindowsAction(
        "type", "notepad", "This is my note!"
    )
    assert parse_action("max, press ctrl+m in Adobe Premiere Pro.") == WindowsAction(
        "shortcut", "premiere", "ctrl+m"
    )


def test_real_premiere_shortcut_plans_are_explicit_and_bounded():
    intents = {
        "Max, go to beginning of timeline in premiere": "home",
        "go to end of timeline in premiere": "end",
        "please add a marker in Premiere": "m",
        "mark in point in Premiere": "i",
        "mark out point in Premiere": "o",
        "cut at playhead in Premiere": "ctrl+k",
        "export video in Premiere": "ctrl+m",
        "pause playback in premiere": "space",
    }
    for message, shortcut in intents.items():
        assert parse_action(message) == WindowsAction("shortcut", "premiere", shortcut)
    assert parse_action("delete footage in premiere") is None
    assert parse_action("press ctrl+k in chrome") is None
    assert parse_action("press home in notepad") is None
    assert parse_action("press alt+f4 in premiere") is None
    assert parse_action("type into premiere: a\nb") is None


def test_addressed_action_never_executes_before_pc_approval(tmp_path):
    bridge, recorder = make_bridge(tmp_path)
    assert bridge.submit("Max, cut at playhead in Premiere")["action"] == "sleeping"
    assert not recorder.actions
    bridge.submit("hi max")
    request = bridge.submit("Max, cut at playhead in Premiere")
    assert request["action"] == "approval"
    assert request["approval"]["application"] == "premiere"
    assert "CTRL+K" in request["approval"]["preview"]
    assert not recorder.actions
    assert bridge.approve_action(request["approval"]["id"])["ok"]
    assert recorder.actions == [WindowsAction("shortcut", "premiere", "ctrl+k")]
    assert not bridge.approve_action(request["approval"]["id"])["ok"]


def test_speech_sleep_cancels_pending_edit(tmp_path):
    bridge, recorder = make_bridge(tmp_path)
    bridge.submit("hi max")
    pending = bridge.submit("Max, add a marker in premiere")["approval"]
    bridge.submit("bye max")
    assert not bridge.approve_action(pending["id"])["ok"]
    assert recorder.actions == []
