"""Voice controls use injected sound/device fakes in CI, no microphone needed."""
import wave
import io

import pytest

from max.pc.voice import mic_wav, say, transcribe


def test_microphone_capture_is_well_formed_wav():
    data = mic_wav(seconds=0.1, sample_rate=16000, recorder=lambda frames, _rate: b"\x00\x00"*frames)
    with wave.open(io.BytesIO(data), "rb") as f:
        assert f.getnchannels() == 1
        assert f.getframerate() == 16000
        assert f.getnframes() == 1600
    with pytest.raises(ValueError):
        mic_wav(seconds=15, recorder=lambda *_: b"")


def test_tts_prefers_male_voice_when_it_exists():
    class Voice:
        id = "male_id"
        name = "Microsoft David"
        gender = "Male"
    class Engine:
        def __init__(self): self.props = {}; self.words = []
        def getProperty(self, name): return [Voice()]
        def setProperty(self, name, value): self.props[name] = value
        def say(self, text): self.words.append(text)
        def runAndWait(self): pass
        def stop(self): pass
    engine = Engine()
    assert say("Good morning boss", engine_factory=lambda: engine)
    assert engine.props["voice"] == "male_id"
    assert engine.words == ["Good morning boss"]


def test_stt_uses_service_and_does_not_need_network():
    class Service:
        def transcribe_bytes(self, payload):
            assert payload == b"fake"
            return type("Result", (), {"text": "hi max"})()
    assert transcribe(b"fake", service=Service()) == "hi max"
