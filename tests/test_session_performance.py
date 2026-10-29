"""Regression tests for bounded disk reads while retaining durable transcripts."""

from pathlib import Path

from max.core.session import SessionStore


def test_history_returns_newest_turns_in_order_with_unicode(tmp_path):
    sessions = SessionStore(tmp_path)
    for index in range(1000):
        sessions.append("session", "user", f"hello-{index}-😀")
        sessions.append("session", "assistant", f"response-{index}-é")
    history = sessions.history("session", limit=5)
    assert [m["content"] for m in history] == [
        "response-997-é", "hello-998-😀", "response-998-é",
        "hello-999-😀", "response-999-é",
    ]
    assert len(sessions.load("session")) == 2000


def test_history_skips_invalid_and_torn_lines(tmp_path):
    sessions = SessionStore(tmp_path)
    sessions.append("s", "user", "one")
    sessions.append("s", "system", "excluded")
    sessions.append("s", "assistant", "two")
    with sessions.path("s").open("ab") as handle:
        handle.write(b"not-json\n{broken")
    assert sessions.history("s", limit=10) == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
    assert sessions.history("s", limit=0) == []


def test_history_does_not_need_to_load_full_transcript(tmp_path, monkeypatch):
    sessions = SessionStore(tmp_path)
    for index in range(4000):
        sessions.append("big", "user", f"message-{index}")
    def forbid_full_load(_):
        raise AssertionError("history must not read the entire transcript")
    monkeypatch.setattr(sessions, "load", forbid_full_load)
    assert sessions.history("big", limit=2) == [
        {"role": "user", "content": "message-3998"},
        {"role": "user", "content": "message-3999"},
    ]


def test_history_handles_one_line_without_trailing_newline(tmp_path):
    sessions = SessionStore(tmp_path)
    sessions.path("solo").write_text(
        '{"role":"user","content":"single"}', encoding="utf-8"
    )
    assert sessions.history("solo") == [{"role": "user", "content": "single"}]
