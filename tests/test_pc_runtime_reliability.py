"""Reliability regressions against actual PCBridge lifecycle, no live hardware."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from max.config import Config
from max.pc.control import PCController
from max.pc.web_app import PCBridge


def make_bridge(tmp_path, **kwargs):
    bridge = PCBridge(
        PCController(tmp_path),
        config=Config(repo_root=tmp_path, data_dir=tmp_path, provider="mock"),
        speaker=lambda text: False,
        **kwargs,
    )
    bridge.set_voice(False)
    return bridge


def test_rapid_stop_restart_never_runs_two_microphone_workers(tmp_path):
    started = threading.Event()
    unblock = threading.Event()
    active = [0]
    maximum = [0]
    calls = []

    def capture(seconds):
        active[0] += 1
        maximum[0] = max(maximum[0], active[0])
        calls.append(seconds)
        started.set()
        try:
            assert unblock.wait(3), "test recorder remained blocked"
            return b"FAKE"
        finally:
            active[0] -= 1

    bridge = make_bridge(tmp_path, recorder=capture, stt=lambda audio: "")
    try:
        state = bridge.set_handsfree(True)
        assert state["handsfree"]
        assert started.wait(2)
        worker = bridge._listening_thread

        bridge.set_handsfree(False)
        rejected = bridge.set_handsfree(True)
        assert not rejected["handsfree"]
        assert "still stopping" in rejected["voice_error"]
        assert bridge._listening_thread is worker
        assert len(calls) == 1

        # Even if the user presses TALK while the old capture is stopping,
        # never start a second recorder.
        talk = bridge.record()
        assert not talk["ok"]
        assert "still active or stopping" in talk["text"]
        assert len(calls) == 1

        unblock.set()
        worker.join(timeout=2)
        assert not worker.is_alive()

        started.clear()
        unblock.clear()
        state = bridge.set_handsfree(True)
        assert state["handsfree"]
        assert started.wait(2)
        assert len(calls) == 2
        assert maximum == [1]
    finally:
        bridge.set_handsfree(False)
        unblock.set()
        if bridge._listening_thread:
            bridge._listening_thread.join(timeout=3)


def test_explicit_ai_probe_clears_stale_success_on_error(tmp_path):
    bridge = make_bridge(tmp_path)
    class Provider:
        fail = False

        def complete(self, messages, tools=None):
            if self.fail:
                raise RuntimeError("provider offline")
            return SimpleNamespace(text="OK")

    provider = Provider()
    bridge._active_provider = provider
    bridge.model_name = "example-model"
    assert bridge.test_ai_connection()["ok"]
    assert bridge.state()["model_verified"]
    provider.fail = True
    failed = bridge.test_ai_connection()
    assert not failed["ok"]
    assert not bridge.state()["model_verified"]


def test_failed_reprobe_with_no_provider_is_not_verified(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge._model_verified = True
    bridge._active_provider = None
    result = bridge.test_ai_connection()
    assert not result["ok"]
    assert bridge.state()["model_verified"] is False


def test_model_install_must_wait_for_stopping_capture(tmp_path):
    captured = threading.Event()
    release = threading.Event()
    installations = []

    def capture(seconds):
        captured.set()
        assert release.wait(3)
        return b"FAKE"

    bridge = make_bridge(
        tmp_path,
        recorder=capture,
        stt=lambda audio: "",
        speech_installer=lambda: installations.append(True) or "installed",
    )
    try:
        bridge.set_handsfree(True)
        assert captured.wait(2)
        bridge.set_handsfree(False)
        outcome = bridge.install_voice_model()
        assert not outcome["ok"]
        assert not installations
        release.set()
        bridge._listening_thread.join(timeout=2)
        assert not bridge._listening_thread.is_alive()
        assert bridge.install_voice_model()["ok"]
        assert installations == [True]
    finally:
        bridge.set_handsfree(False)
        release.set()
        if bridge._listening_thread:
            bridge._listening_thread.join(timeout=3)
