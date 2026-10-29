"""Voice setup requires consent; failed speech produces actionable diagnostics."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from max.config import Config
from max.pc.control import PCController
from max.pc.voice import (
    install_speech_model, speech_failure, speech_model_status
)
from max.pc.web_app import PCBridge
from max.speech import SttError


def test_whisper_install_is_explicit_and_verifies_cached_weights(tmp_path):
    calls = []
    def download(name, **kwargs):
        calls.append((name, kwargs))
        (tmp_path / "model.bin").write_bytes(b"fake-weights")
        return str(tmp_path)
    assert calls == []  # nothing downloads on import/startup
    message = install_speech_model("tiny", downloader=download)
    assert calls == [("tiny", {"local_files_only": False})]
    assert "cached locally" in message


def test_failed_or_unapproved_model_install_is_not_a_success(tmp_path):
    def missing_file(name, **kwargs):
        return str(tmp_path)
    with pytest.raises(RuntimeError, match="incomplete"):
        install_speech_model("tiny", downloader=missing_file)
    def should_not_download(*args, **kwargs):
        raise AssertionError("called for a forbidden model")
    with pytest.raises(ValueError, match="Unsupported"):
        install_speech_model("../../dangerous", downloader=should_not_download)


def test_stt_error_has_helpful_text_without_internal_trace():
    exc = SttError("failed to load whisper model 'tiny': LocalEntryNotFoundError.")
    text = speech_failure(exc)
    assert "Whisper" in text
    assert "SPEECH SETUP" in text
    assert "LocalEntryNotFoundError" not in text
    assert "Windows Settings" in speech_failure(OSError("Private device path"))


def test_voice_bridge_shows_status_and_installs_only_on_button(tmp_path):
    state = {"cached": False, "n": 0}
    def inspect():
        return {"model": "tiny", "backend_installed": True,
                "cached": state["cached"]}
    def install():
        state["n"] += 1
        state["cached"] = True
        return "Whisper tiny cached locally."
    bridge = PCBridge(
        PCController(tmp_path),
        config=Config(repo_root=tmp_path, data_dir=tmp_path),
        speech_installer=install, speech_inspector=inspect,
        speaker=lambda _: None,
    )
    assert not bridge.state()["speech"]["cached"]
    assert state["n"] == 0  # boot never downloads
    result = bridge.install_voice_model()
    assert result["ok"] and result["state"]["speech"]["cached"]
    assert state["n"] == 1
    assert bridge.state()["model"] == "NO MODEL CONNECTED"  # voice != AI


def test_voice_record_preserves_actionable_stt_failure(tmp_path):
    def fail(_wav):
        raise SttError("failed to load whisper model 'tiny': LocalEntryNotFoundError")
    bridge = PCBridge(
        PCController(tmp_path),
        config=Config(repo_root=tmp_path, data_dir=tmp_path),
        recorder=lambda seconds: b"test",
        stt=fail, speech_inspector=lambda: {"model": "tiny", "backend_installed": True, "cached": False},
        speaker=lambda _: None,
    )
    result = bridge.record()
    assert not result["ok"] and "SPEECH SETUP" in result["text"]


def test_installer_refuses_while_handsfree_active(tmp_path):
    calls = []
    bridge = PCBridge(
        PCController(tmp_path), config=Config(repo_root=tmp_path, data_dir=tmp_path),
        speech_installer=lambda: calls.append(True),
        speech_inspector=lambda: {"model": "tiny", "backend_installed": True, "cached": False},
    )
    bridge._listening_thread = SimpleNamespace(is_alive=lambda: True)
    result = bridge.install_voice_model()
    assert not result["ok"] and "HANDS-FREE OFF" in result["text"]
    assert calls == []


def test_speech_setup_controls_are_part_of_packaged_ui():
    html = (Path(__file__).parents[1] / "src/max/pc/ui/index.html").read_text(encoding="utf-8")
    for element_id in ["speech-button", "speech-panel", "speech-status", "install-speech"]:
        assert 'id="' + element_id + '"' in html
    assert "install_voice_model" in html
    assert "window.confirm" in html
