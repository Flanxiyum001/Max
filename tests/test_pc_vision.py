"""First Jarvis 'eyes' milestone: explicitly consented, read-only native PC vision.

No display hardware, provider network, credential or disk writes in these tests.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from max.config import Config
from max.pc.control import PCController
from max.pc.vision import VisionUnavailable, WindowSnapshot, capture_foreground_window
from max.pc.web_app import PCBridge


class FakeProvider:
    def __init__(self, text="An Adobe Premiere timeline is visible."):
        self.calls = []
        self.text = text

    def complete(self, messages, tools=None):
        self.calls.append((messages, tools))
        return SimpleNamespace(text=self.text)


def bridge_with_vision(tmp_path, capture):
    events = []
    provider = FakeProvider()
    bridge = PCBridge(
        PCController(tmp_path),
        config=Config(repo_root=tmp_path, data_dir=tmp_path, provider="mock"),
        speaker=lambda _: False,
        vision_capture=capture,
        vision_wait=lambda seconds: events.append(seconds),
    )
    bridge.set_voice(False)
    return bridge, provider, events


def enable(bridge, provider):
    assert bridge.submit("hi max")["action"] == "wake"
    bridge._active_provider = provider
    bridge._model_verified = True
    bridge.model_name = "vision-model"


def fake_capture(name):
    return WindowSnapshot(name, b"\xff\xd8JPEG TEST DATA", 800, 600)


def test_vision_requires_wake_verified_model_and_supported_app(tmp_path):
    captures = []
    bridge, provider, _ = bridge_with_vision(
        tmp_path, lambda name: captures.append(name) or fake_capture(name)
    )
    assert not bridge.prepare_observation("premiere")["ok"]
    bridge.submit("hi max")
    assert not bridge.prepare_observation("premiere")["ok"]
    bridge._active_provider = provider
    assert not bridge.prepare_observation("premiere")["ok"]
    bridge._model_verified = True
    assert not bridge.prepare_observation("chrome")["ok"]
    assert captures == [] and not provider.calls


def test_one_shot_window_vision_sends_image_only_after_explicit_token(tmp_path):
    captures = []
    bridge, provider, waits = bridge_with_vision(
        tmp_path, lambda name: captures.append(name) or fake_capture(name)
    )
    enable(bridge, provider)
    prepared = bridge.prepare_observation("premiere")
    assert prepared["ok"] and prepared["application"] == "premiere"
    assert captures == [] and not provider.calls
    failed = bridge.observe_screen("not-the-token")
    assert not failed["ok"]
    assert captures == [] and not provider.calls

    prepared = bridge.prepare_observation("premiere")
    result = bridge.observe_screen(prepared["token"])
    assert result["ok"] and "timeline" in result["text"]
    assert waits == [3.0]
    assert captures == ["premiere"]
    assert len(provider.calls) == 1
    messages, tools = provider.calls[0]
    assert tools is None
    assert len(messages) == 1
    assert "untrusted data" in messages[0]["content"][0]["text"]
    image = messages[0]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(image.split(",", 1)[1]) == fake_capture("premiere").jpeg
    assert image not in result["text"]
    assert "approval" not in result
    assert not bridge.observe_screen(prepared["token"])["ok"]
    assert len(provider.calls) == 1


def test_cancel_sleep_expiry_and_error_do_not_upload(tmp_path):
    captures = []
    bridge, provider, _ = bridge_with_vision(
        tmp_path, lambda app: captures.append(app) or fake_capture(app)
    )
    enable(bridge, provider)
    p = bridge.prepare_observation("notepad")
    assert bridge.cancel_observation()["ok"]
    assert not bridge.observe_screen(p["token"])["ok"]
    p = bridge.prepare_observation("notepad")
    bridge._vision_pending = (p["token"], "notepad", 0.0)
    assert "expired" in bridge.observe_screen(p["token"])["text"]
    p = bridge.prepare_observation("notepad")
    assert bridge.submit("bye max")["action"] == "sleep"
    assert not bridge.observe_screen(p["token"])["ok"]
    assert captures == [] and provider.calls == []


def test_model_error_is_redacted_and_never_acts_on_visual_instructions(tmp_path):
    def capture(app):
        return fake_capture(app)
    bridge, provider, _ = bridge_with_vision(tmp_path, capture)
    enable(bridge, provider)
    def failure(*args, **kwargs):
        raise RuntimeError("sensitive provider response including credentials")
    provider.complete = failure
    prepared = bridge.prepare_observation("premiere")
    reply = bridge.observe_screen(prepared["token"])
    assert not reply["ok"]
    assert "sensitive" not in reply["text"]
    assert "RuntimeError" in reply["text"]
    # Natural chat requests are never a hidden trigger for image capture.
    bridge.submit("What is on my screen?")


class FakeImage:
    size = (1600, 1000)
    width = 1600
    height = 1000

    def crop(self, box):
        self.size = (box[2] - box[0], box[3] - box[1])
        self.width, self.height = self.size
        return self

    def thumbnail(self, size):
        self.width, self.height = min(self.width, size[0]), min(self.height, size[1])

    def convert(self, mode):
        assert mode == "RGB"
        return self

    def save(self, output, *, format, quality, optimize):
        assert format == "JPEG" and quality <= 70 and optimize
        output.write(b"\xff\xd8" + b"x" * 128)


class FakeGui:
    def __init__(self, title="Adobe Premiere Pro 2026", program="Adobe Premiere Pro.exe"):
        self.window = SimpleNamespace(
            title=title, _hWnd=10, left=10, top=15,
            width=800, height=600, isMinimized=False,
        )
        self.calls = 0
        self.program = program

    def getActiveWindow(self):
        return self.window

    def size(self):
        return (1600, 1000)

    def screenshot(self, region=None):
        self.calls += 1
        assert region == (self.window.left, self.window.top, self.window.width, self.window.height)
        image = FakeImage()
        image.size = (region[2], region[3])
        image.width, image.height = image.size
        return image


def test_foreground_capture_never_accepts_spoofed_app_title_or_wrong_program():
    gui = FakeGui(title="Adobe Premiere Pro 2026", program="msedge.exe")
    with pytest.raises(VisionUnavailable, match="different app"):
        capture_foreground_window("premiere", gui=gui, executable=gui.program)
    assert gui.calls == 0


def test_foreground_capture_bounded_ephemeral_jpeg_without_disk(tmp_path):
    gui = FakeGui()
    captured = capture_foreground_window("premiere", gui=gui, executable=gui.program)
    assert captured.application == "premiere"
    assert captured.jpeg.startswith(b"\xff\xd8")
    assert captured.width <= 1280 and captured.height <= 900
    assert gui.calls == 1
    assert list(tmp_path.iterdir()) == []


def test_capture_fails_when_foreground_window_switches_during_screenshot():
    gui = FakeGui()
    original = gui.screenshot
    def capture_then_switch():
        out = original()
        gui.window = SimpleNamespace(
            title="Notepad", _hWnd=20, left=0, top=0,
            width=600, height=400, isMinimized=False,
        )
        return out
    gui.screenshot = capture_then_switch
    with pytest.raises(VisionUnavailable, match="active app changed"):
        capture_foreground_window("premiere", gui=gui, executable="Adobe Premiere Pro.exe")


def test_no_full_screen_fallback_for_secondary_monitor():
    gui = FakeGui()
    gui.window.left = -100
    with pytest.raises(VisionUnavailable, match="primary monitor"):
        capture_foreground_window("premiere", gui=gui, executable=gui.program)
    assert gui.calls == 0
