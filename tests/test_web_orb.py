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


# --- speech-to-text ------------------------------------------------------------------


def test_stt_config_defaults_to_chat_provider() -> None:
    from max_core.web.app import stt_config

    cfg = stt_config(
        {"MAX_TRIAL_ENDPOINT": "https://llm.example.test/v1/chat/completions",
         "MAX_TRIAL_API_KEY": "freellmapi-k"}
    )  # fmt: skip
    assert cfg is not None
    assert cfg.url == "https://llm.example.test/v1/audio/transcriptions"
    assert cfg.api_key == "freellmapi-k" and cfg.model == "whisper-large-v3"
    override = stt_config(
        {"MAX_STT_ENDPOINT": "https://stt.example.test/v1/audio/transcriptions",
         "MAX_STT_API_KEY": "s", "MAX_STT_MODEL": "m", "MAX_TRIAL_API_KEY": "x"}
    )  # fmt: skip
    assert override is not None and (override.api_key, override.model) == ("s", "m")
    assert stt_config({}) is None
    assert stt_config({"MAX_STT_ENDPOINT": "http://evil.example/v1/audio/transcriptions"}) is None
    assert stt_config({"MAX_STT": "off", "MAX_STT_ENDPOINT": "https://a.test/x"}) is None


def test_transcribe_endpoint(tmp_path: Path) -> None:
    heard: list[tuple[int, str]] = []

    async def fake_stt(audio: bytes, content_type: str, cfg: object) -> str:
        heard.append((len(audio), content_type))
        return "  what's the weather in Pune  "

    app = create_app(
        {"MAX_WEB_PASSCODE": PASS, "MAX_WEB_INSECURE_COOKIE": "yes",
         "MAX_STT_ENDPOINT": "https://stt.example.test/v1/audio/transcriptions"},
        session_factory=FakeSession, tts=fake_tts, transcriber=fake_stt,
    )  # fmt: skip
    client = TestClient(app)
    body = b"\x1aE\xdf\xa3" + b"0" * 4000
    assert client.post("/api/transcribe", content=body,
                       headers={"content-type": "audio/webm"}).status_code == 401  # fmt: skip
    login(client)
    assert client.get("/api/state").json()["stt_ready"] is True
    ok = client.post("/api/transcribe", content=body, headers={"content-type": "audio/webm"})
    assert ok.json() == {"text": "what's the weather in Pune"}
    assert heard == [(len(body), "audio/webm")]
    assert client.post("/api/transcribe", content=b"x" * 10,
                       headers={"content-type": "audio/webm"}).status_code == 400  # fmt: skip
    assert client.post("/api/transcribe", content=body,
                       headers={"content-type": "text/plain"}).status_code == 415  # fmt: skip


def test_transcribe_real_http_shape() -> None:
    """The multipart request matches the OpenAI /audio/transcriptions contract."""
    import asyncio

    import httpx

    from max_core.web import app as web

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "Hey MAX, remind me."})

    real = httpx.AsyncClient

    def patched(*a, **kw):  # type: ignore[no-untyped-def]
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    cfg = web.SttConfig(
        "https://stt.example.test/v1/audio/transcriptions", "k", "whisper-large-v3", "en"
    )
    httpx.AsyncClient = patched  # type: ignore[misc]
    try:
        text = asyncio.run(web.transcribe(b"audio-bytes", "audio/webm;codecs=opus", cfg))
    finally:
        httpx.AsyncClient = real  # type: ignore[misc]
    assert text == "Hey MAX, remind me."
    assert seen["auth"] == "Bearer k"
    body = seen["body"]
    assert isinstance(body, bytes)
    for part in (b'name="model"', b"whisper-large-v3", b'name="language"', b'filename="speech.webm"',
                 b"audio-bytes", b"MAX"):  # fmt: skip
        assert part in body


def test_stt_unconfigured(web) -> None:  # type: ignore[no-untyped-def]
    client, _ = web
    login(client)
    assert client.get("/api/state").json()["stt_ready"] is False
    assert client.post("/api/transcribe", content=b"0" * 3000,
                       headers={"content-type": "audio/webm"}).status_code == 503  # fmt: skip


# --- FreeLLMAPI "auto" ----------------------------------------------------------------


def test_router_auto_model_accepts_routed_model() -> None:
    from max_core.gateway.normalize import model_matches

    assert model_matches("auto", "groq/llama-3.3-70b-versatile")
    assert model_matches("auto:fast", "gemini-2.5-flash")
    assert not model_matches("gpt-4o", "gpt-4o-mini")
    assert not model_matches("auto", "")
