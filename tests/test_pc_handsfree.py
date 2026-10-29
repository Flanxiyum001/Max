"""Hands-free wake/sleep is opt-in and contains no passive startup recording."""
from __future__ import annotations

import time
from collections import deque

from max.config import Config
from max.pc.control import PCController
from max.pc.web_app import PCBridge


def test_handsfree_is_off_by_default_and_only_started_by_click(tmp_path):
    captured = []
    words = deque(["random conversation", "hi max", "what time is it", "bye max"])
    def recorder(seconds):
        captured.append(seconds)
        return b"FAKE-WAV"
    def stt(_wav):
        if words:
            return words.popleft()
        raise RuntimeError("mic disconnected")
    bridge = PCBridge(
        PCController(tmp_path), config=Config(repo_root=tmp_path, data_dir=tmp_path),
        recorder=recorder, stt=stt, speaker=lambda _: False,
    )
    bridge.set_voice(False)
    assert bridge.state()["handsfree"] is False
    assert captured == []
    bridge.set_handsfree(True)
    deadline = time.monotonic() + 2
    events = []
    while time.monotonic() < deadline:
        events.extend(bridge.poll_events())
        if any(e["type"] == "microphone_off" for e in events):
            break
        time.sleep(.02)
    bridge.set_handsfree(False)
    heard = [e["heard"] for e in events if e["type"] == "speech"]
    assert "random conversation" not in heard
    assert heard == ["hi max", "what time is it", "bye max"]
    assert len(captured) >= 4
    assert all(x == 3.0 for x in captured)
    assert bridge.controller.awake is False
    assert any(e["type"] == "error" for e in events)


def test_failing_microphone_disables_handsfree_and_surfaces_error(tmp_path):
    def fail(seconds):
        raise OSError("no mic")
    bridge = PCBridge(
        PCController(tmp_path), config=Config(repo_root=tmp_path, data_dir=tmp_path),
        recorder=fail, speaker=lambda _: False,
    )
    bridge.set_handsfree(True)
    deadline = time.monotonic() + 2
    events = []
    while time.monotonic() < deadline:
        events.extend(bridge.poll_events())
        if any(e["type"] == "microphone_off" for e in events):
            break
        time.sleep(.02)
    assert not bridge.state()["handsfree"]
    assert "microphone" in bridge.state()["voice_error"].lower()
    assert any(e["type"] == "error" for e in events)


def test_handfree_stop_works_without_model_or_internet(tmp_path):
    def recorder(seconds):
        time.sleep(.04)
        return b"WAV"
    bridge = PCBridge(
        PCController(tmp_path), config=Config(repo_root=tmp_path, data_dir=tmp_path),
        recorder=recorder, stt=lambda wav: "",
    )
    bridge.set_handsfree(True)
    bridge.set_handsfree(False)
    bridge._listening_thread.join(timeout=1)
    assert not bridge.state()["handsfree"]
