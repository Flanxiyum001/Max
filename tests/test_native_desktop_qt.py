"""Graphical desktop integration tests (offscreen Qt) for MAX."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.local_config import DesktopConfig, load_desktop_config
from max_core.models import ApprovalGrant
from max_core.native_desktop.backend import NativeDesktopCore
from max_core.persona import VoicePreference
from max_core.presence.notifications import FocusState
from max_core.presence.orb import OrbState, OrbVisibility
from max_core.voice.controller import VoiceController
from max_core.voice.desktop_stt import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE, DesktopSpeechToText
from max_core.voice.desktop_tts import DesktopTextToSpeech

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _isolate_qt_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "isolated_desktop.sqlite3"
    data_dir = tmp_path / "app_data"
    ws_dir = tmp_path / "ws"
    notes_dir = tmp_path / "notes"
    data_dir.mkdir(parents=True, exist_ok=True)
    ws_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(data_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MAX_LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("MAX_WORKSPACE_ROOT", str(ws_dir))
    monkeypatch.setenv("MAX_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("MAX_CONFIG_PATH", str(tmp_path / "missing_config.json"))
    for key in (
        "MAX_DATABASE_URL",
        "MAX_DESKTOP_DB_URL",
        "MAX_LLM_PROVIDER",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return db_path


def _pump_until(app: Any, predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    assert predicate(), "Timed out waiting for Qt UI condition"


def _openai_mock_transport(
    responses: list[tuple[int, dict[str, Any]]],
    recorded_requests: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    queue = list(responses)

    def _handler(request: httpx.Request) -> httpx.Response:
        if recorded_requests is not None:
            recorded_requests.append(json.loads(request.content.decode("utf-8")))
        if not queue:
            return httpx.Response(500, json={"error": {"message": "no queued mock response"}})
        status_code, payload = queue.pop(0)
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(_handler)


def _openai_ok(
    content: str = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return (
        200,
        {
            "id": "chatcmpl-qt-test",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": msg,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {"prompt_tokens": 110, "completion_tokens": 20, "total_tokens": 130},
        },
    )


def _make_cfg(tmp_path: Path, db_path: Path, *, api_key: str | None = None) -> DesktopConfig:
    env: dict[str, str] = {
        "MAX_LOCAL_DB_PATH": str(db_path),
        "MAX_WORKSPACE_ROOT": str(tmp_path / "ws"),
        "MAX_NOTES_DIR": str(tmp_path / "notes"),
    }
    if api_key is not None:
        env["MAX_LLM_PROVIDER"] = "openai"
        env["OPENAI_API_KEY"] = api_key
        env["MAX_OPENAI_MODEL"] = "gpt-4o-mini"
    return load_desktop_config(
        env=env,
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path / "app_data",
    )


def test_qt_native_window_builds_offscreen_with_isolated_local_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pytest.importorskip("PySide6")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    from max_core.native_desktop.qt_ui import launch

    assert launch(smoke_test=True) == 0
    assert db_path.is_file()


def test_qt_window_no_database_mode_degrades_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    _isolate_qt_env(monkeypatch, tmp_path)
    from max_core.native_desktop.qt_ui import MaxWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    core = NativeDesktopCore()
    window = MaxWindow(core)
    try:
        window.show()
        _pump_until(app, lambda: "checking" not in window.system_storage.text().lower())
        assert window.system_storage.text() == "Storage: Database not configured"
        assert window._store_ready is False
        assert not window.queue_button.isEnabled()
        assert not window.approve_button.isEnabled()
        assert not window.deny_button.isEnabled()
        window.queue_task()
        assert "not ready" in window.notice.text().lower()
    finally:
        window.close()
        app.processEvents()


def test_qt_window_live_chat_multi_turn_and_provider_badge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    from max_core.native_desktop.qt_ui import MaxWindow

    recorded: list[dict[str, Any]] = []
    transport = _openai_mock_transport(
        [
            _openai_ok("First live reply from MAX."),
            _openai_ok("Second live reply remembering turn one."),
        ],
        recorded,
    )
    cfg = _make_cfg(tmp_path, db_path, api_key="sk-test-qt-live-chat-12345")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    core = NativeDesktopCore.from_env(config=cfg)
    window = MaxWindow(core, transport=transport)
    try:
        window.show()
        _pump_until(app, lambda: window._store_ready)
        assert "openai" in window.provider_badge.text()
        assert "gpt-4o-mini" in window.provider_badge.text()

        window.chat_input.setPlainText("Hello MAX")
        window.send_chat()
        _pump_until(
            app, lambda: "First live reply from MAX." in window.chat_transcript.toPlainText()
        )

        window.chat_input.setPlainText("Follow up question")
        window.send_chat()
        _pump_until(
            app,
            lambda: (
                "Second live reply remembering turn one." in window.chat_transcript.toPlainText()
            ),
        )

        assert len(recorded) == 2
        assert len(recorded[1]["messages"]) > len(recorded[0]["messages"])
    finally:
        window.close()
        app.processEvents()


def test_qt_window_chat_sanitized_errors_never_leak_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    from max_core.native_desktop.qt_ui import MaxWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    # 1. Missing API key
    no_key_cfg = _make_cfg(tmp_path, db_path, api_key=None)
    core_no_key = NativeDesktopCore.from_env(config=no_key_cfg)
    win_no_key = MaxWindow(core_no_key)
    try:
        win_no_key.show()
        _pump_until(app, lambda: win_no_key._store_ready)
        win_no_key.chat_input.setPlainText("Are you configured?")
        win_no_key.send_chat()
        _pump_until(app, lambda: "missing_api_key" in win_no_key.chat_transcript.toPlainText())
    finally:
        win_no_key.close()
        app.processEvents()

    # 2. HTTP 401 + HTTP 429 + privacy_guard egress block (secret never leaked)
    secret_key = "sk-qt-super-secret-must-never-leak-987654"
    recorded: list[dict[str, Any]] = []
    transport = _openai_mock_transport(
        [
            (401, {"error": {"message": f"Invalid key {secret_key}"}}),
            (429, {"error": {"message": "Rate limit exceeded"}}),
        ],
        recorded,
    )
    cfg = _make_cfg(tmp_path, tmp_path / "errors.sqlite3", api_key=secret_key)
    core = NativeDesktopCore.from_env(config=cfg)
    window = MaxWindow(core, transport=transport)
    try:
        window.show()
        _pump_until(app, lambda: window._store_ready)

        window.chat_input.setPlainText("Test auth failure")
        window.send_chat()
        _pump_until(app, lambda: "auth_failed" in window.chat_transcript.toPlainText())
        assert secret_key not in window.chat_transcript.toPlainText()
        assert secret_key not in window.notice.text()

        window.chat_input.setPlainText("Test rate limit")
        window.send_chat()
        _pump_until(app, lambda: "rate_limited" in window.chat_transcript.toPlainText())

        assert len(recorded) == 2
        leaked_secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
        window.chat_input.setPlainText(f"Here is a secret {leaked_secret}")
        window.send_chat()
        _pump_until(app, lambda: "egress_blocked" in window.chat_transcript.toPlainText())
        assert len(recorded) == 2
    finally:
        window.close()
        app.processEvents()


def test_qt_window_task_journal_active_and_persists_across_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    from max_core.native_desktop.qt_ui import MaxWindow

    cfg = _make_cfg(tmp_path, db_path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    core1 = NativeDesktopCore.from_env(config=cfg)
    win1 = MaxWindow(core1)
    try:
        win1.show()
        _pump_until(app, lambda: win1._store_ready)
        assert win1.system_storage.text() == "Storage: Local SQLite ready"
        assert win1.queue_button.isEnabled()

        win1.switch(1)
        win1.objective.setPlainText("Persist Qt journal item")
        win1.queue_task()
        _pump_until(app, lambda: win1.stat_tasks.text() == "1")
    finally:
        win1.close()
        app.processEvents()

    core2 = NativeDesktopCore.from_env(config=cfg)
    win2 = MaxWindow(core2)
    try:
        win2.show()
        _pump_until(app, lambda: win2.stat_tasks.text() == "1" and win2.task_list.count() == 1)
        assert "Persist Qt journal item" in win2.task_list.item(0).text()
        win2.select_task(win2.task_list.item(0))
        _pump_until(
            app,
            lambda: (
                win2.events_list.count() >= 1 and "task.queued" in win2.events_list.item(0).text()
            ),
        )
    finally:
        win2.close()
        app.processEvents()


def test_qt_window_trust_inbox_approve_deny_and_continuation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    notes_dir = tmp_path / "notes"
    from max_core.native_desktop.qt_ui import MaxWindow

    transport = _openai_mock_transport(
        [
            _openai_ok(
                tool_calls=[
                    {
                        "id": "call_save_note",
                        "type": "function",
                        "function": {
                            "name": "notes_save",
                            "arguments": json.dumps(
                                {"title": "qt-note", "content": "Approved via UI"}
                            ),
                        },
                    }
                ]
            ),
            _openai_ok("Note qt-note saved and confirmed."),
            _openai_ok(
                tool_calls=[
                    {
                        "id": "call_create_task",
                        "type": "function",
                        "function": {
                            "name": "task_create",
                            "arguments": json.dumps({"objective": "Should not be created"}),
                        },
                    }
                ]
            ),
        ]
    )
    cfg = _make_cfg(tmp_path, db_path, api_key="sk-test-qt-trust-12345")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    core = NativeDesktopCore.from_env(config=cfg)
    window = MaxWindow(core, transport=transport)
    try:
        window.show()
        _pump_until(app, lambda: window._store_ready)

        # 1. Propose notes.save -> Trust Inbox -> Approve -> executes & continues LLM turn.
        window.chat_input.setPlainText("Save note qt-note")
        window.send_chat()
        _pump_until(
            app,
            lambda: (
                window.approve_button.isEnabled()
                and window.trust_list.count() >= 1
                and "notes.save" in window.trust_list.item(0).text()
            ),
        )
        assert not (notes_dir / "qt-note.md").exists()

        window.switch(2)
        window.trust_list.setCurrentRow(0)
        window.approve_button.click()
        _pump_until(
            app,
            lambda: (
                "Note qt-note saved and confirmed." in window.chat_transcript.toPlainText()
                and not window.approve_button.isEnabled()
            ),
        )
        assert (notes_dir / "qt-note.md").read_text(encoding="utf-8") == "Approved via UI"

        # 2. Propose task.create -> Trust Inbox -> Deny -> denied without creating task.
        window.chat_input.setPlainText("Create task")
        window.send_chat()
        _pump_until(
            app,
            lambda: (
                window.deny_button.isEnabled() and "task.create" in window.trust_list.item(0).text()
            ),
        )
        window.trust_list.setCurrentRow(0)
        window.deny_button.click()
        _pump_until(
            app,
            lambda: (
                "was denied" in window.chat_transcript.toPlainText().lower()
                and not window.deny_button.isEnabled()
            ),
        )

        assert core.engine is not None
        with Session(core.engine) as session:
            states = {g.tool_name: g.state for g in session.scalars(select(ApprovalGrant)).all()}
        assert states["notes.save"] == "consumed"
        assert states["task.create"] == "denied"
        assert not any(t["objective"] == "Should not be created" for t in core.snapshot().tasks)
    finally:
        window.close()
        app.processEvents()


def test_qt_window_tool_outcomes_displayed_for_r0_and_denied_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    from max_core.native_desktop.qt_ui import MaxWindow

    transport = _openai_mock_transport(
        [
            _openai_ok(
                tool_calls=[
                    {
                        "id": "call_add",
                        "type": "function",
                        "function": {
                            "name": "dev_add",
                            "arguments": json.dumps({"a": 19, "b": 23}),
                        },
                    },
                    {
                        "id": "call_shell",
                        "type": "function",
                        "function": {"name": "shell.run", "arguments": json.dumps({"cmd": "dir"})},
                    },
                ]
            ),
            _openai_ok("Computed 42; shell.run was denied."),
        ]
    )
    cfg = _make_cfg(tmp_path, db_path, api_key="sk-test-qt-tools-12345")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    core = NativeDesktopCore.from_env(config=cfg)
    window = MaxWindow(core, transport=transport)
    try:
        window.show()
        _pump_until(app, lambda: window._store_ready)
        window.chat_input.setPlainText("Add 19 + 23 and try shell.run")
        window.send_chat()
        _pump_until(
            app,
            lambda: "Computed 42; shell.run was denied." in window.chat_transcript.toPlainText(),
        )
        outcomes = [
            window.tool_outcomes_list.item(i).text()
            for i in range(window.tool_outcomes_list.count())
        ]
        joined = "\n".join(outcomes)
        assert "[COMPLETED] dev.add" in joined
        assert "42" in joined
        assert "[DENIED] shell.run" in joined
    finally:
        window.close()
        app.processEvents()


class _FakeTtsDriver:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stop_calls = 0
        self._state = "ready"

    def engine(self) -> str:
        return "sapi"

    def availableVoices(self) -> list[Any]:
        return []

    def allVoices(self, _locale: Any) -> list[Any]:
        return []

    def setLocale(self, _locale: Any) -> None:
        pass

    def setVoice(self, _voice: Any) -> None:
        pass

    def say(self, text: str) -> None:
        self._state = "speaking"
        self.spoken.append(text)

    def stop(self, _hint: Any = None) -> None:
        self.stop_calls += 1
        self._state = "ready"

    def state(self) -> str:
        return self._state

    def errorReason(self) -> int:
        return 0


class _FakeAudioOutput:
    def isNull(self) -> bool:
        return False


class _ThreadCheckingFakeRecorder:
    def __init__(self, expected_thread: Any) -> None:
        self._expected_thread = expected_thread
        self.available = True
        self.headless = False
        self.reason = None
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS
        self.start_threads: list[Any] = []
        self.stop_threads: list[Any] = []
        self.discard_calls = 0

    def start(self) -> bool:
        from PySide6.QtCore import QThread

        self.start_threads.append(QThread.currentThread())
        return QThread.currentThread() == self._expected_thread

    def stop_and_drain(self) -> list[bytes]:
        from PySide6.QtCore import QThread

        self.stop_threads.append(QThread.currentThread())
        return [b"\x01\x02" * 160]

    def discard(self) -> None:
        self.discard_calls += 1


def test_qt_window_voice_controls_cyan_orb_and_headless_degradation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    from max_core.native_desktop.qt_ui import MaxWindow, _MainThreadRecorderProxy

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    # 1. Headless offscreen graceful degradation on default VoiceController.
    cfg_headless = _make_cfg(tmp_path, db_path)
    core_headless = NativeDesktopCore.from_env(config=cfg_headless)
    win_headless = MaxWindow(core_headless)
    try:
        win_headless.show()
        _pump_until(app, lambda: win_headless._store_ready)
        assert "headless" in win_headless.voice_status_label.text().lower()
        assert win_headless.orb.primary_ring_hex.startswith("#00") or (
            win_headless.orb.primary_ring_hex == "#22d3ee"
        )
        win_headless.ptt_button.click()
        assert "unavailable" in win_headless.notice.text().lower()
        win_headless.speak_toggle.click()
        assert "unavailable" in win_headless.notice.text().lower()
        win_headless.stop_voice_button.click()
    finally:
        win_headless.close()
        app.processEvents()

    # 2. _MainThreadRecorderProxy thread-affinity + STT -> chat + TTS -> barge-in + quiet mode.
    main_thread = app.thread()
    raw_recorder = _ThreadCheckingFakeRecorder(main_thread)
    proxy = _MainThreadRecorderProxy(raw_recorder)  # type: ignore[arg-type]

    stt_threads: list[Any] = []

    def _fake_transcribe(_wav_bytes: bytes) -> str:
        stt_threads.append(QtCore.QThread.currentThread())
        return "/task Should never execute as slash command"

    driver = _FakeTtsDriver()
    tts = DesktopTextToSpeech(
        preference=VoicePreference(presentation="feminine", locale="en-GB"),
        headless=False,
        engine_factory=lambda _name: driver,
        available_engines_fn=lambda: ["sapi"],
        audio_outputs_fn=lambda: [_FakeAudioOutput()],
    )
    stt = DesktopSpeechToText(transcribe_fn=_fake_transcribe)
    vc = VoiceController(
        config=_make_cfg(tmp_path, tmp_path / "voice.sqlite3", api_key="sk-test-voice-12345"),
        headless=False,
        tts=tts,
        stt=stt,
        recorder=proxy,  # type: ignore[arg-type]
        focus_probe=lambda: FocusState.ACCEPTS_NOTIFICATIONS,
    )
    transport = _openai_mock_transport([_openai_ok("Voice reply delivered safely.")])
    cfg = _make_cfg(tmp_path, tmp_path / "voice.sqlite3", api_key="sk-test-voice-12345")
    core = NativeDesktopCore.from_env(config=cfg)
    window = MaxWindow(core, transport=transport, voice_controller=vc)
    try:
        proxy.setParent(window)
        window.show()
        _pump_until(app, lambda: window._store_ready)

        window.speak_toggle.click()
        assert window._speak_enabled is True

        window.ptt_button.click()
        assert window.orb.orb_state == OrbState.LISTENING
        assert window.orb.orb_visibility == OrbVisibility.ACTIVE
        assert window.ptt_button.text() == "Stop & Send"

        # Stop & transcribe runs STT on QThreadPool while stop_and_drain marshals to main thread.
        window.ptt_button.click()
        _pump_until(
            app,
            lambda: "Voice reply delivered safely." in window.chat_transcript.toPlainText(),
        )
        # STT transcript routed strictly as untrusted user text into chat (never /task shortcut).
        assert "You: /task Should never execute as slash command" in (
            window.chat_transcript.toPlainText()
        )
        assert window.stack.currentIndex() == 0
        assert raw_recorder.start_threads == [main_thread]
        assert raw_recorder.stop_threads == [main_thread]
        assert len(stt_threads) == 1 and stt_threads[0] != main_thread
        assert driver.spoken == ["Voice reply delivered safely."]

        # Barge-in / Stop button halts TTS.
        window.stop_voice_button.click()
        assert driver.stop_calls >= 1

        # Quiet mode stops orb timer and forces PASSIVE.
        window.toggle_quiet()
        assert window.orb.quiet is True
        assert not window.orb._timer.isActive()
        assert window.orb.orb_visibility == OrbVisibility.PASSIVE
    finally:
        window.close()
        app.processEvents()


def test_qt_window_egress_blocked_redacts_secrets_in_ui_and_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    ws_dir = tmp_path / "ws"
    (ws_dir / "leak.txt").write_text("token: Bearer secret-token-999\n", encoding="utf-8")
    from max_core.native_desktop.qt_ui import MaxWindow

    recorded: list[dict[str, Any]] = []
    transport = _openai_mock_transport(
        [
            _openai_ok(
                tool_calls=[
                    {
                        "id": "call_read_leak",
                        "type": "function",
                        "function": {
                            "name": "workspace_read_file",
                            "arguments": json.dumps({"path": "leak.txt"}),
                        },
                    }
                ]
            ),
            _openai_ok("Should never be reached after egress block."),
        ],
        recorded,
    )
    cfg = _make_cfg(tmp_path, db_path, api_key="sk-test-qt-redaction-12345")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    core = NativeDesktopCore.from_env(config=cfg)
    window = MaxWindow(core, transport=transport)
    try:
        window.show()
        _pump_until(app, lambda: window._store_ready)

        # (a) User submits high-confidence credential directly.
        window.chat_input.setPlainText("Please send Bearer secret-token-999")
        window.send_chat()
        _pump_until(app, lambda: "egress_blocked" in window.chat_transcript.toPlainText())
        assert len(recorded) == 0

        # (b) workspace.read_file reads a file containing a high-confidence credential.
        window.chat_input.setPlainText("Read leak.txt")
        window.send_chat()
        _pump_until(
            app,
            lambda: (
                "Tool output blocked" in window.chat_transcript.toPlainText()
                and window._in_flight_chats == 0
            ),
        )
        assert len(recorded) == 1

        transcript = window.chat_transcript.toPlainText()
        outcomes = "\n".join(
            window.tool_outcomes_list.item(i).text()
            for i in range(window.tool_outcomes_list.count())
        )
        notice_text = window.notice.text()
        badge_text = window.provider_badge.text()

        for surface in (transcript, outcomes, notice_text, badge_text):
            assert "Bearer secret-token-999" not in surface
            assert "secret-token-999" not in surface

        assert "You: [REDACTED — sensitive credential omitted]" in transcript
        assert "[REDACTED — contains high-confidence credential]" in outcomes
        assert "[REDACTED — contains high-confidence credential]" in transcript
        assert "[COMPLETED] workspace.read_file" in outcomes
    finally:
        window.close()
        app.processEvents()

    assert b"secret-token-999" not in db_path.read_bytes()
    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        for table in tables:
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()  # noqa: S608
            assert "secret-token-999" not in repr(rows)


def test_qt_window_overlapping_chats_keep_orb_thinking_until_all_finish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    db_path = _isolate_qt_env(monkeypatch, tmp_path)
    from max_core.native_desktop.qt_ui import MaxWindow

    gate1 = threading.Event()
    gate2 = threading.Event()
    entered1 = threading.Event()
    entered2 = threading.Event()
    call_count = 0

    def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            entered1.set()
            gate1.wait(timeout=3.0)
            _, payload = _openai_ok("Reply one.")
        else:
            entered2.set()
            gate2.wait(timeout=3.0)
            _, payload = _openai_ok("Reply two.")
        return httpx.Response(200, json=payload)

    cfg = _make_cfg(tmp_path, db_path, api_key="sk-test-qt-overlap-12345")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    core = NativeDesktopCore.from_env(config=cfg)
    window = MaxWindow(core, transport=httpx.MockTransport(_handler))
    try:
        window.show()
        _pump_until(app, lambda: window._store_ready)

        window.chat_input.setPlainText("First prompt")
        window.send_chat()
        assert entered1.wait(timeout=3.0)
        assert window._in_flight_chats == 1
        assert window._chat_busy is True
        assert window.orb.orb_state == OrbState.THINKING

        window.chat_input.setPlainText("Second prompt")
        window.send_chat()
        assert window._in_flight_chats == 2
        assert window._chat_busy is True
        assert window.orb.orb_state == OrbState.THINKING

        gate1.set()
        _pump_until(app, lambda: "Reply one." in window.chat_transcript.toPlainText())
        assert entered2.wait(timeout=3.0)
        assert window._in_flight_chats == 1
        assert window._chat_busy is True
        assert window.orb.orb_state == OrbState.THINKING

        gate2.set()
        _pump_until(app, lambda: "Reply two." in window.chat_transcript.toPlainText())
        assert window._in_flight_chats == 0
        assert window._chat_busy is False
        assert window.orb.orb_state == OrbState.IDLE
    finally:
        gate1.set()
        gate2.set()
        window.close()
        app.processEvents()
