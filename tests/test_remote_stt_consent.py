"""Remote speech transcription needs explicit owner consent before recording."""

from __future__ import annotations

from max_core.voice.controller import VoiceController
from max_core.voice.desktop_stt import DesktopSpeechToText


class RecorderStub:
    available = True
    headless = False
    reason = None
    sample_rate = 16000
    channels = 1

    def __init__(self) -> None:
        self.started = 0

    def start(self) -> bool:
        self.started += 1
        return True

    def discard(self) -> None:
        pass

    def stop_and_drain(self) -> list[bytes]:
        return []


def test_remote_stt_fails_closed_without_owner_consent() -> None:
    recorder = RecorderStub()
    stt = DesktopSpeechToText(
        api_key="synthetic-test-key",
        endpoint="https://api.openai.com/v1/audio/transcriptions",
    )
    assert stt.requires_remote_upload
    controller = VoiceController(stt=stt, recorder=recorder, headless=True)
    assert not controller.remote_stt_consent
    assert controller.start_push_to_talk() is False
    assert controller.last_error == "remote_stt_consent_required"
    assert recorder.started == 0
    controller.grant_remote_stt_consent()
    assert controller.remote_stt_consent
    assert controller.start_push_to_talk() is True
    assert recorder.started == 1
    controller.revoke_microphone()
    controller.revoke_remote_stt_consent()
    assert controller.remote_stt_consent is False


def test_local_injected_transcription_does_not_require_remote_upload() -> None:
    stt = DesktopSpeechToText(transcribe_fn=lambda payload: "local text")
    assert not stt.requires_remote_upload
