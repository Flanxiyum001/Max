"""Natural speech → PC capability routing; no screen button or mock autonomous agent."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from max.config import Config
from max.pc.control import PCController
from max.pc.intents import (
    observation_target, is_visual_confirmation, is_voice_action_confirmation,
)
from max.pc.web_app import PCBridge
from max.pc.vision import WindowSnapshot


def make(tmp_path, *, base_url="http://127.0.0.1:11434/v1"):
    screenshots = []
    calls = []

    class FakeProvider:
        def complete(self, messages, tools=None):
            calls.append((messages, tools))
            return SimpleNamespace(text="The Premiere timeline shows a sequence.")

    provider = FakeProvider()
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path,
                 provider="mock", base_url=base_url)
    bridge = PCBridge(
        PCController(tmp_path), config=cfg,
        speaker=lambda _: None,
        vision_capture=lambda app: (
            screenshots.append(app)
            or WindowSnapshot(
                "premiere" if app == "active" else app, b"\xff\xd8local screenshot", 640, 480
            )
        ),
    )
    bridge.set_voice(False)
    bridge._active_provider = provider
    bridge.model_name = "vision-test"
    bridge._model_verified = False
    bridge.submit("hi max")
    return bridge, screenshots, calls


@pytest.mark.parametrize("phrase,target", [
    ("Max, look at Premiere and tell me what's wrong", "premiere"),
    ("Hey Max, what's happening in Premiere Pro?", "premiere"),
    ("Max, check Notepad", "notepad"),
    ("Max, describe the window in VS Code", "vscode"),
    ("Max, what's on my screen?", "active"),
    ("Max, look at this window", "active"),
    ("Max, why is my Premiere timeline stuck?", "premiere"),
])
def test_natural_desktop_observation_intents(phrase, target):
    assert observation_target(phrase) == target


@pytest.mark.parametrize("phrase", [
    "open Premiere",
    "Max, what is Adobe Premiere Pro?",
    "search the web for Premiere tutorials",
    "Max, don't look at Premiere",
    "Max, please install Premiere",
])
def test_unrelated_or_negative_speech_never_grabs_screen(phrase):
    assert observation_target(phrase) is None


def test_local_microphone_request_performs_real_model_image_request_no_button(tmp_path):
    bridge, shots, calls = make(tmp_path)
    response = bridge.submit(
        "Max, look at Premiere and tell me what's wrong", source="microphone"
    )
    assert response["ok"] and response["action"] == "vision"
    assert "sequence" in response["text"]
    assert shots == ["premiere"]
    assert len(calls) == 1
    messages, tools = calls[0]
    assert tools is None
    assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # No preflight TEST AI click or LOOK button is required.


def test_untrusted_remote_never_triggers_capture_or_model_call(tmp_path):
    bridge, shots, calls = make(tmp_path)
    result = bridge.submit_remote("Max, look at Premiere")
    assert not result["ok"] and result["action"] == "vision"
    assert shots == [] and calls == []


def test_cloud_needs_separate_local_spoken_authorization(tmp_path):
    bridge, shots, calls = make(tmp_path, base_url="https://llm.example.org/v1")
    prompt = bridge.submit("Max, what's on my screen?", source="microphone")
    assert prompt["action"] == "vision_confirmation"
    assert not shots and not calls
    # Neither a typed reply nor a remotely supplied confirmation can upload.
    assert bridge.submit("Max confirm screen share")["action"] != "vision"
    assert bridge.submit_remote("Max confirm screen share")["action"] != "vision"
    assert not shots and not calls
    confirm = bridge.submit("Max confirm screen share", source="microphone")
    assert confirm["ok"] and confirm["action"] == "vision"
    assert shots == ["active"] and len(calls) == 1
    assert not bridge.submit("Max confirm screen share", source="microphone")["ok"]


def test_cloud_cancellation_and_wake_state_clear_pending(tmp_path):
    bridge, shots, calls = make(tmp_path, base_url="https://llm.example.org/v1")
    bridge.submit("Max, inspect Notepad", source="microphone")
    assert bridge.submit("Max cancel", source="microphone")["action"] == "cancel"
    assert not bridge.submit("Max confirm screen share", source="microphone")["ok"]
    bridge.submit("Max, inspect Notepad", source="microphone")
    bridge.submit("bye max", source="microphone")
    bridge.submit("hi max", source="microphone")
    assert not bridge.submit("Max confirm screen share", source="microphone")["ok"]
    assert shots == [] and calls == []


def test_voice_direct_low_risk_action_without_button(tmp_path):
    bridge, _, _ = make(tmp_path)
    actions = []
    bridge._approvals.adapter = SimpleNamespace(
        execute=lambda action: actions.append(action) or "Sent Space to Premiere"
    )
    reply = bridge.submit("Max, play the timeline in Premiere", source="microphone")
    assert reply["ok"] and reply["action"] == "desktop"
    assert actions and actions[0].value == "space"


def test_edit_requires_distinct_spoken_confirmation_and_consumes_token_once(tmp_path):
    bridge, _, _ = make(tmp_path)
    actions = []
    bridge._approvals.adapter = SimpleNamespace(
        execute=lambda action: actions.append(action) or "Sent Ctrl+K"
    )
    pending = bridge.submit("Max, cut at playhead in Premiere", source="microphone")
    assert pending["action"] == "voice_confirmation" and not actions
    assert is_voice_action_confirmation("Max confirm action")
    assert not is_visual_confirmation("Max confirm action")
    # Text UI and phone cannot grant speech approval.
    bridge.submit("Max confirm action")
    bridge.submit_remote("Max confirm action")
    assert not actions
    done = bridge.submit("Max confirm action", source="microphone")
    assert done["ok"] and done["action"] == "desktop"
    assert len(actions) == 1 and actions[0].value == "ctrl+k"
    assert not bridge.submit("Max confirm action", source="microphone")["ok"]
    assert len(actions) == 1


def test_mutation_cancelled_by_sleep(tmp_path):
    bridge, _, _ = make(tmp_path)
    actions = []
    bridge._approvals.adapter = SimpleNamespace(
        execute=lambda action: actions.append(action) or "Sent Ctrl+K"
    )
    bridge.submit("Max, cut at playhead in Premiere", source="microphone")
    bridge.submit("bye max", source="microphone")
    bridge.submit("hi max", source="microphone")
    outcome = bridge.submit("Max confirm action", source="microphone")
    assert not outcome["ok"]
    assert actions == []


def test_html_has_no_look_button():
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1] / "src" / "max" / "pc" / "ui"
    for name in ("companion.html", "index.html"):
        data = (folder / name).read_text(encoding="utf-8")
        assert 'id="look"' not in data
        assert "el('look')" not in data
        assert "$('look')" not in data
