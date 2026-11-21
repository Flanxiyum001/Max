"""Offscreen checks for the orb interface (skipped without PySide6)."""

from __future__ import annotations

import pytest


@pytest.fixture
def orb(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from max_core.native_desktop.backend import NativeDesktopCore
    from max_core.native_desktop.orb_ui import OrbWindow

    app = QApplication.instance() or QApplication([])
    window = OrbWindow(NativeDesktopCore.from_env())
    window.show()
    yield app, window
    window.close()
    app.processEvents()


def test_orb_smoke_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from max_core.native_desktop.orb_ui import launch_orb

    assert launch_orb(smoke_test=True) == 0


def test_orb_is_the_whole_ui(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    from PySide6.QtCore import Qt

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.orb.state == "idle"
    assert window.input.isHidden() and window.answer.text() == ""


def test_typing_reveals_input_and_escape_clears(orb) -> None:  # type: ignore[no-untyped-def]
    app, window = orb
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.keyClicks(window, "h")
    assert window.input.isVisible() and window.input.text() == "h"
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert window.input.text() == "" and window.input.isHidden()


def test_unconfigured_model_fails_honestly(orb, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    app, window = orb
    for key in ("MAX_TRIAL_DESTINATION", "MAX_TRIAL_MODEL", "MAX_TRIAL_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    window.input.setText("hello")
    window.send()
    assert window.orb.state == "thinking"
    window._pool.waitForDone()
    for _ in range(50):
        app.processEvents()
    assert window.orb.state == "error"
    assert "No model connected" in window.answer.text()


def test_previous_answer_cannot_hide_new_answer(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    window._show_answer("first response")
    assert window._hide_timer.isActive()
    window._show_answer(None)
    assert not window._hide_timer.isActive()
    window._show_answer("second response")
    assert window.answer.text() == "second response"
    assert window._hide_timer.isActive()


def test_close_is_deferred_during_active_local_call(orb) -> None:  # type: ignore[no-untyped-def]
    app, window = orb
    window._busy = True
    assert not window.close()  # no blocking call to the worker thread
    assert window.isVisible()
    assert window._close_requested
    window._busy = False
    window._failed("unavailable")
    app.processEvents()
    assert not window.isVisible()
    assert window.core._closed


def test_idle_window_close_releases_local_core(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    assert not window.core._closed
    assert window.close()
    assert window.core._closed
    assert window.core.engine is None


def test_new_reply_restarts_idle_timer(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    window._failed("unavailable")
    assert window._state_timer.isActive() and window.orb.state == "error"
    window._state_timer.stop()
    window._idle_after_reply()
    assert window.orb.state == "idle"


def test_model_tool_proposal_is_never_reported_as_executed(orb) -> None:  # type: ignore[no-untyped-def]
    _, window = orb
    from max_core.native_desktop.backend import LocalModelReply

    window._busy = True
    window._done(LocalModelReply("review_required", "I modified the source code", ("shell.run",)))
    assert window.orb.state == "error"
    assert window.answer.text() == (
        "The model proposed tool actions. Nothing was executed; review required."
    )
    assert "modified the source code" not in window.answer.text()


class _FakeSession:
    """Stands in for AssistantSession: asks approval for one tool, then answers."""

    def __init__(self) -> None:
        self.decisions: list[bool] = []
        self.closed = False

    def ask(self, text, *, approve=None, on_step=None):  # type: ignore[no-untyped-def]
        from max_core.tool_loop import LoopResult

        self.decisions.append(approve("apps.open", {"target": "https://example.org"}))
        return LoopResult(answer="opened" if self.decisions[-1] else "not opened")

    def due_reminders(self):  # type: ignore[no-untyped-def]
        return [{"text": "stretch"}]

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _orb_with(monkeypatch: pytest.MonkeyPatch, session: _FakeSession):  # type: ignore[no-untyped-def]
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from max_core.native_desktop.backend import NativeDesktopCore
    from max_core.native_desktop.orb_ui import OrbWindow

    app = QApplication.instance() or QApplication([])
    window = OrbWindow(NativeDesktopCore.from_env(), session_factory=lambda: session)
    window.show()
    return app, window


@pytest.mark.parametrize(("key", "allowed"), [("Return", True), ("Escape", False)])
def test_orb_asks_owner_before_effectful_tool(
    monkeypatch: pytest.MonkeyPatch, key: str, allowed: bool
) -> None:
    session = _FakeSession()
    app, window = _orb_with(monkeypatch, session)
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.input.setText("open example")
    window.send()
    for _ in range(200):
        app.processEvents()
        if window._pending_approval is not None:
            break
        QTest.qWait(5)
    assert "MAX wants to run apps.open" in window.answer.text()
    QTest.keyClick(window, getattr(Qt.Key, f"Key_{key}"))
    window._pool.waitForDone()
    for _ in range(50):
        app.processEvents()
    assert session.decisions == [allowed]
    assert window.answer.text() == ("opened" if allowed else "not opened")
    window.close()
    assert session.closed


def test_orb_announces_due_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    app, window = _orb_with(monkeypatch, _FakeSession())
    window._check_reminders()
    assert window.answer.text() == "Reminder: stretch"
    window.close()


class _FakeSTT:
    def __init__(self, remote: bool) -> None:
        self.available = True
        self.requires_remote_upload = remote
        self.endpoint = "https://stt.example.test/v1/audio/transcriptions"


class _FakeVoice:
    """Mimics VoiceController's surface used by the orb."""

    def __init__(self, transcript: str | None = "what time is it", remote: bool = False) -> None:
        self.stt = _FakeSTT(remote)
        self.is_recording = False
        self.is_speaking = False
        self.remote_stt_consent = False
        self.last_error: str | None = None
        self.transcript = transcript
        self.spoken: list[str] = []
        self.interrupted = 0

    def grant_remote_stt_consent(self) -> None:
        self.remote_stt_consent = True

    def start_push_to_talk(self, *, direct_user_gesture: bool = True) -> bool:
        if self.stt.requires_remote_upload and not self.remote_stt_consent:
            self.last_error = "remote_stt_consent_required"
            return False
        self.is_recording = True
        return True

    def stop_push_to_talk_and_transcribe(self) -> str | None:
        self.is_recording = False
        self.remote_stt_consent = False
        if self.transcript is None:
            self.last_error = "empty_audio"
        return self.transcript

    def speak_text(self, text: str) -> bool:
        self.spoken.append(text)
        self.is_speaking = True
        return True

    def interrupt_speech(self) -> None:
        self.interrupted += 1
        self.is_speaking = False

    def revoke_microphone(self) -> None:
        self.is_recording = False


class _EchoSession(_FakeSession):
    def ask(self, text, *, approve=None, on_step=None):  # type: ignore[no-untyped-def]
        from max_core.tool_loop import LoopResult

        self.asked = text
        return LoopResult(answer=f"You asked: {text}")


def _voice_orb(monkeypatch, voice, session=None):  # type: ignore[no-untyped-def]
    pytest.importorskip("PySide6")
    monkeypatch.delenv("MAX_DATABASE_URL", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from max_core.native_desktop.backend import NativeDesktopCore
    from max_core.native_desktop.orb_ui import OrbWindow

    app = QApplication.instance() or QApplication([])
    session = session or _EchoSession()
    window = OrbWindow(
        NativeDesktopCore.from_env(),
        session_factory=lambda: session,
        voice_factory=lambda: voice,
    )
    window.show()
    return app, window, session


def _settle(app, window) -> None:  # type: ignore[no-untyped-def]
    window._pool.waitForDone()
    for _ in range(50):
        app.processEvents()


def test_space_listens_then_answers_aloud(monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _FakeVoice()
    app, window, session = _voice_orb(monkeypatch, voice)
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.keyClick(window, Qt.Key.Key_Space)
    assert voice.is_recording and window.orb.state == "listening"
    assert window.input.isHidden()  # space did not start typing
    QTest.keyClick(window, Qt.Key.Key_Space)
    _settle(app, window)
    assert session.asked == "what time is it"
    assert window.answer.text() == "You asked: what time is it"
    assert voice.spoken == ["You asked: what time is it"]
    assert window.orb.state == "speaking"
    QTest.keyClick(window, Qt.Key.Key_Escape)  # Esc stops speech
    assert voice.interrupted == 1 and window.orb.state == "idle"
    window.close()


def test_typed_turn_is_not_spoken(monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _FakeVoice()
    app, window, _ = _voice_orb(monkeypatch, voice)
    window.input.setText("hello")
    window.send()
    _settle(app, window)
    assert window.answer.text() == "You asked: hello" and voice.spoken == []
    window.close()


@pytest.mark.parametrize(("key", "records"), [("Return", True), ("Escape", False)])
def test_remote_transcription_needs_consent_each_time(
    monkeypatch: pytest.MonkeyPatch, key: str, records: bool
) -> None:
    voice = _FakeVoice(remote=True)
    app, window, _ = _voice_orb(monkeypatch, voice)
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.keyClick(window, Qt.Key.Key_Space)
    assert not voice.is_recording
    assert "stt.example.test" in window.answer.text()
    QTest.keyClick(window, getattr(Qt.Key, f"Key_{key}"))
    assert voice.is_recording is records
    if records:
        QTest.keyClick(window, Qt.Key.Key_Space)
        _settle(app, window)
        assert not voice.remote_stt_consent  # consent used up by that one recording
        QTest.keyClick(window, Qt.Key.Key_Space)
        assert not voice.is_recording and "stt.example.test" in window.answer.text()
        QTest.keyClick(window, Qt.Key.Key_Escape)
    window.close()


def test_silence_reports_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _FakeVoice(transcript=None)
    app, window, session = _voice_orb(monkeypatch, voice)
    window.toggle_listening()
    window.toggle_listening()
    _settle(app, window)
    assert window.orb.state == "error" and window.answer.text() == "I didn't hear anything."
    assert not hasattr(session, "asked")
    window.close()


def test_voice_unavailable_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken():  # type: ignore[no-untyped-def]
        raise RuntimeError("no audio stack")

    app, window, _ = _voice_orb(monkeypatch, None)
    window._voice_factory = broken
    window.toggle_listening()
    assert window.orb.state == "error" and "Voice problem" in window.answer.text()
    window.close()
