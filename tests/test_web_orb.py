from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from max_core.tool_loop import LoopResult
from max_core.web.app import create_app

PASS = "correct horse battery"


class FakeSession:
    def __init__(self) -> None:
        self.registry: list = []  # type: ignore[type-arg]
        self.asked: list[str] = []
        self.decision: bool | None = None
        self.due = [{"text": "stretch"}]

    def ask(self, text, *, approve=None, on_step=None):  # type: ignore[no-untyped-def]
        self.asked.append(text)
        if text == "remind me":
            self.decision = approve("reminders.add", {"text": "call mum", "due": "in 5 minutes"})
            return LoopResult(answer="added" if self.decision else "not added")
        return LoopResult(answer=f"echo: {text}")

    def reset(self) -> None:
        self.asked.clear()

    def due_reminders(self):  # type: ignore[no-untyped-def]
        due, self.due = self.due, []
        return due


async def fake_tts(text: str, voice: str, rate: str, pitch: str) -> bytes:
    return b"ID3" + voice.encode()


@pytest.fixture
def web() -> tuple[TestClient, FakeSession]:
    session = FakeSession()
    app = create_app(
        {"MAX_WEB_PASSCODE": PASS, "MAX_WEB_INSECURE_COOKIE": "yes"},
        session_factory=lambda: session,
        tts=fake_tts,
    )
    return TestClient(app), session


def login(client: TestClient) -> None:
    assert client.post("/api/login", json={"passcode": PASS}).status_code == 200


def wait(client: TestClient, job: str, until: set[str]) -> dict:  # type: ignore[type-arg]
    for _ in range(200):
        data = client.get(f"/api/job/{job}").json()
        if data["status"] in until:
            return data
        time.sleep(0.01)
    raise AssertionError(data)


def test_refuses_to_start_without_strong_passcode() -> None:
    with pytest.raises(RuntimeError):
        create_app({"MAX_WEB_PASSCODE": "short"})


def test_page_is_public_but_api_needs_login(web) -> None:  # type: ignore[no-untyped-def]
    client, _ = web
    page = client.get("/")
    assert page.status_code == 200 and "orb.js" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/static/orb.js").status_code == 200
    assert client.get("/static/../app.py").status_code == 404
    assert client.get("/api/state").json() == {"authed": False}
    for method, path, body in [
        ("post", "/api/ask", {"text": "hi"}),
        ("get", "/api/reminders/due", None),
        ("post", "/api/speak", {"text": "hi"}),
        ("post", "/api/reset", None),
    ]:
        assert getattr(client, method)(path, **({"json": body} if body else {})).status_code == 401


def test_wrong_passcode_and_rate_limit(web) -> None:  # type: ignore[no-untyped-def]
    client, _ = web
    codes = [client.post("/api/login", json={"passcode": "nope"}).status_code for _ in range(9)]
    assert codes[:8] == [403] * 8 and codes[8] == 429


def test_forged_cookie_rejected(web) -> None:  # type: ignore[no-untyped-def]
    client, _ = web
    client.cookies.set("max_web", f"{int(time.time()) + 999}.deadbeef")
    assert client.get("/api/state").json() == {"authed": False}


def test_conversation_round_trip(web) -> None:  # type: ignore[no-untyped-def]
    client, session = web
    login(client)
    assert client.get("/api/state").json()["model_ready"] is True
    job = client.post("/api/ask", json={"text": "hello"}).json()["job"]
    assert wait(client, job, {"done"})["answer"] == "echo: hello"


@pytest.mark.parametrize("allow", [True, False])
def test_effectful_tool_waits_for_owner(web, allow: bool) -> None:  # type: ignore[no-untyped-def]
    client, session = web
    login(client)
    job = client.post("/api/ask", json={"text": "remind me"}).json()["job"]
    pending = wait(client, job, {"approval"})
    assert pending["approval"]["tool"] == "reminders.add"
    assert client.post("/api/ask", json={"text": "other"}).status_code == 409  # one at a time
    client.post(f"/api/job/{job}/approve", json={"allow": allow})
    assert wait(client, job, {"done"})["answer"] == ("added" if allow else "not added")
    assert session.decision is allow
    assert client.post(f"/api/job/{job}/approve", json={"allow": True}).status_code == 409


def test_reminders_and_speech(web) -> None:  # type: ignore[no-untyped-def]
    client, _ = web
    login(client)
    assert client.get("/api/reminders/due").json() == {"due": ["stretch"]}
    assert client.get("/api/reminders/due").json() == {"due": []}
    audio = client.post("/api/speak", json={"text": "Hello"})
    assert audio.headers["content-type"] == "audio/mpeg"
    assert audio.content == b"ID3en-IE-EmilyNeural"


def test_unconfigured_model_is_reported(tmp_path: Path) -> None:
    app = create_app(
        {"MAX_WEB_PASSCODE": PASS, "MAX_WEB_INSECURE_COOKIE": "yes", "MAX_DATA_DIR": str(tmp_path)}
    )
    client = TestClient(app)
    login(client)
    state = client.get("/api/state").json()
    assert state["model_ready"] is False and state["error"] == "model_not_configured"
    assert client.post("/api/ask", json={"text": "hi"}).status_code == 503


def test_hosted_session_has_no_host_tools(tmp_path: Path) -> None:
    from max_core.web.app import _default_session

    env = {
        "MAX_TRIAL_DESTINATION": "cloud",
        "MAX_TRIAL_CLOUD_ALLOWED": "yes",
        "MAX_TRIAL_ENDPOINT": "https://api.example.test/v1/chat/completions",
        "MAX_TRIAL_MODEL": "free-model",
        "MAX_TRIAL_CONTEXT_TOKENS": "8192",
        "MAX_TRIAL_API_KEY": "k",
        "MAX_TRIAL_MAX_COST_USD": "0",
        "MAX_TRIAL_INPUT_USD_PER_MILLION": "0",
        "MAX_TRIAL_OUTPUT_USD_PER_MILLION": "0",
        "MAX_ENABLE_SHELL": "yes",
        "MAX_CLOUD_PERSONAL_TOOLS": "yes",
        "MAX_WORKSPACE_DIR": str(tmp_path),
        "MAX_DATA_DIR": str(tmp_path),
    }
    session = _default_session(env)
    try:
        names = {t.name for t in session.registry}
        assert not names & {"shell.run", "apps.open", "system.status", "workspace.read"}
        assert {"memory.remember", "reminders.add", "web.search", "weather.current"} <= names
    finally:
        session.close()
