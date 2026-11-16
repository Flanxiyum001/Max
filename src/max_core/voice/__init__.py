"""Replaceable, opt-in voice session primitives; no microphone starts on import."""

from max_core.voice.barge_in import BargeInGate
from max_core.voice.pipeline import (
    AudioSink,
    SpeechToText,
    TextGateway,
    TextToSpeech,
    VoiceError,
    VoiceTurnPipeline,
    VoiceTurnResult,
    bounded_microphone,
    sentence_chunks,
)

__all__ = [
    "AudioSink",
    "BargeInGate",
    "SpeechToText",
    "TextGateway",
    "TextToSpeech",
    "VoiceError",
    "VoiceTurnPipeline",
    "VoiceTurnResult",
    "bounded_microphone",
    "sentence_chunks",
]
