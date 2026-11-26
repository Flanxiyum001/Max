from __future__ import annotations

from pathlib import Path

import pytest

from max_core.comms import CommsStore, comms_tools
from max_core.comms.voice_call import (
    END_MARK,
    CallManager,
    CallSettings,
    VoiceCall,
    WhatsAppDesktopDialer,
)
from max_core.tools import ToolError, ToolRisk


class FakeAudio:
    def __init__(self, heard: list[bytes | None]) -> None:
        self.heard, self.played = heard, []

    def listen(self, max_seconds: float, silence_seconds: float) -> bytes | None:
        return self.heard.pop(0) if self.heard else None

    def play(self, wav: bytes) -> None:
        self.played.append(wav.decode())


class FakeDialer:
    def __init__(self) -> None:
        self.events: list[str] = []

    def dial(self, phone: str) -> None:
        self.events.append(f"dial {phone}")

    def hang_up(self) -> None:
        self.events.append("hang_up")


def _call(
    heard: list[bytes | None], replies: list[str]
) -> tuple[VoiceCall, FakeAudio, FakeDialer, list]:
    audio, dialer, prompts = FakeAudio(heard), FakeDialer(), []

    def reply(messages):  # type: ignore[no-untyped-def]
        prompts.append(list(messages))
        return replies.pop(0)

    call = VoiceCall(
        dialer=dialer,
        audio=audio,
        transcribe=lambda pcm: pcm.decode(),
        speak=lambda text: text.encode(),
        reply=reply,
        settings=CallSettings(owner_name="Pourush"),
    )
    return call, audio, dialer, prompts


def test_call_mom_says_late_has_conversation_and_hangs_up() -> None:
    call, audio, dialer, prompts = _call(
        [b"Hello?", b"Oh okay, is he driving?"],
        [
            "Hi, I'm MAX, an AI assistant calling for Pourush. He'll be 20-30 minutes late.",
            f"Yes, he's on his way. I'll let him know you asked. Bye! {END_MARK}",
            "Mom was told he is 20-30 minutes late and asked if he is driving.",
        ],
    )
    record = call.run("Mom", "+919876543210", "Tell her I'll be late by 20-30 minutes")
    assert record.state == "ended"
    assert dialer.events == ["dial +919876543210", "hang_up"]
    assert audio.played[0].startswith("Hi, I'm MAX, an AI assistant")
    assert END_MARK not in audio.played[1]
    system = prompts[0][0][1]
    assert "late by 20-30 minutes" in system and "AI assistant" in system
    assert [w for w, _ in record.transcript] == ["them", "max", "them", "max"]
    assert "20-30 minutes" in record.summary


def test_no_answer_hangs_up_without_speaking() -> None:
    call, audio, dialer, _ = _call([None], [])
    record = call.run("Mom", "+919876543210", "late")
    assert record.state == "no_answer" and audio.played == []
    assert dialer.events[-1] == "hang_up" and "did not answer" in record.summary


def test_silence_ends_call() -> None:
    call, audio, _, _ = _call([b"Hello?"], ["Hi, it's MAX.", "Are you there?", "summary"])
    record = call.run("Mom", "+91", "late")
    assert record.state == "ended" and len(audio.played) == 2


def test_dialer_opens_whatsapp_chat_then_presses_call_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    opened, pressed = [], []
    dialer = WhatsAppDesktopDialer("ctrl+shift+c", "", open_uri=opened.append, press=pressed.append)
    dialer.dial("+91 98765-43210")
    dialer.hang_up()
    assert opened == ["whatsapp://send?phone=919876543210"] and pressed == ["ctrl+shift+c"]
    with pytest.raises(ToolError):
        dialer.dial("12")


def test_tool_starts_background_call_and_stores_summary(tmp_path: Path) -> None:
    store = CommsStore(tmp_path / "c.sqlite3")
    store.add_contact("Mom", "+919876543210")
    call, *_ = _call([b"Hello?"], [f"Hi! He is late. Bye {END_MARK}", "Told Mom he is late."])
    manager = CallManager(
        lambda: call,
        lambda r: store.receive("call", r.contact, f"Call summary: {r.summary}", None),
    )
    tools = {t.name: t for t in comms_tools(store, {}, calls=manager)}
    assert tools["comms.whatsapp_call"].risk == ToolRisk.EFFECTFUL
    with pytest.raises(ToolError, match="unknown_contact"):
        tools["comms.whatsapp_call"].handler({"to": "Dad", "message": "x"})
    assert "Calling Mom" in tools["comms.whatsapp_call"].handler(
        {"to": "Mom", "message": "I'll be 20-30 minutes late"}
    )
    manager.wait(5)
    assert "ended" in tools["comms.call_status"].handler({})
    assert "Told Mom he is late." in tools["comms.inbox"].handler({})
