"""MAX web orb: a passcode-protected, single-owner web version of the orb.

Run locally:   MAX_WEB_PASSCODE=... python -m max_core.web
Hosted (Render): see render.yaml and docs/M30_WEB_ORB.md.

Only the owner (whoever knows MAX_WEB_PASSCODE) can use it. Tools that act on
the host machine (shell, apps, PC status, workspace files) are never enabled
on the web server.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

STATIC = Path(__file__).parent / "static"
COOKIE = "max_web"
SESSION_SECONDS = 7 * 24 * 3600
DEFAULT_VOICE = "en-IE-EmilyNeural"


class LoginBody(BaseModel):
    passcode: str = Field(max_length=200)


class AskBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: bool = False


class ApproveBody(BaseModel):
    allow: bool


class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@dataclass
class Job:
    id: str
    status: str = "running"  # running | approval | done | failed
    answer: str = ""
    error: str | None = None
    approval: dict[str, Any] | None = None
    steps: list[str] = field(default_factory=list)
    _event: threading.Event = field(default_factory=threading.Event)
    _allowed: bool = False
    created: float = field(default_factory=time.time)


class RateLimiter:
    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


async def synthesize(text: str, voice: str, rate: str, pitch: str) -> bytes:
    """Neural speech via the edge-tts library (Microsoft Edge read-aloud voices)."""
    import edge_tts  # type: ignore[import-not-found,unused-ignore]  # optional: .[web]

    audio = bytearray()
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            audio.extend(chunk["data"])
            if len(audio) > 8_000_000:
                break
    return bytes(audio)


@dataclass(frozen=True)
class SttConfig:
    url: str
    api_key: str | None
    model: str
    language: str | None


def stt_config(env: dict[str, str]) -> SttConfig | None:
    """Speech-to-text via any OpenAI-compatible /v1/audio/transcriptions endpoint.

    Defaults to the chat provider (e.g. FreeLLMAPI) and its key, so one key covers both.
    """
    if env.get("MAX_STT", "").lower() in {"off", "no", "0"}:
        return None
    url = env.get("MAX_STT_ENDPOINT", "").strip()
    if not url:
        chat = env.get("MAX_TRIAL_ENDPOINT", "").strip()
        if not chat.endswith("/chat/completions"):
            return None
        url = chat[: -len("/chat/completions")] + "/audio/transcriptions"
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        return None
    return SttConfig(
        url=url,
        api_key=env.get("MAX_STT_API_KEY") or env.get("MAX_TRIAL_API_KEY") or None,
        model=env.get("MAX_STT_MODEL", "whisper-large-v3"),
        language=env.get("MAX_STT_LANGUAGE") or None,
    )


_STT_PROMPT = "MAX, the owner's personal assistant. Hey MAX."


async def transcribe(audio: bytes, content_type: str, cfg: SttConfig) -> str:
    import httpx

    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/wav": "wav",
           "audio/mpeg": "mp3"}.get(content_type.split(";")[0].strip(), "webm")  # fmt: skip
    data = {"model": cfg.model, "response_format": "json", "temperature": "0",
            "prompt": _STT_PROMPT}  # fmt: skip
    if cfg.language:
        data["language"] = cfg.language
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.post(
            cfg.url,
            data=data,
            files={"file": (f"speech.{ext}", audio, content_type.split(";")[0])},
            headers=headers,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"stt_http_{response.status_code}")
    text = response.json().get("text", "")
    return text.strip() if isinstance(text, str) else ""


def create_app(
    env: dict[str, str] | None = None,
    *,
    session_factory: Any = None,
    tts: Any = None,
    transcriber: Any = None,
) -> FastAPI:
    env = dict(os.environ if env is None else env)
    passcode = env.get("MAX_WEB_PASSCODE", "")
    if len(passcode) < 8:
        raise RuntimeError("Set MAX_WEB_PASSCODE (at least 8 characters) before starting.")
    secret = (env.get("MAX_WEB_SECRET") or secrets.token_hex(32)).encode()
    secure_cookie = env.get("MAX_WEB_INSECURE_COOKIE", "") != "yes"
    voice = env.get("MAX_WEB_VOICE", DEFAULT_VOICE)
    rate = env.get("MAX_WEB_VOICE_RATE", "+6%")
    pitch = env.get("MAX_WEB_VOICE_PITCH", "+2Hz")
    speak = tts or synthesize
    stt = stt_config(env)
    hear = transcriber or transcribe

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    login_limit = RateLimiter(8, 600)
    stt_limit = RateLimiter(30, 60)
    ask_limit = RateLimiter(30, 60)
    tts_limit = RateLimiter(40, 60)
    jobs: dict[str, Job] = {}
    state: dict[str, Any] = {"session": None, "error": None}
    state_lock = threading.Lock()

    def make_token() -> str:
        expires = str(int(time.time()) + SESSION_SECONDS)
        sig = hmac.new(secret, expires.encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{sig}"

    def authed(request: Request) -> bool:
        token = request.cookies.get(COOKIE, "")
        expires, _, sig = token.partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        good = hmac.new(secret, expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good)

    def require(request: Request) -> None:
        if not authed(request):
            raise HTTPException(401, "login_required")

    def session() -> Any:
        with state_lock:
            if state["session"] is None and state["error"] is None:
                try:
                    if session_factory is not None:
                        state["session"] = session_factory()
                    else:
                        state["session"] = _default_session(env)
                except Exception:
                    state["error"] = "model_not_configured"
            return state["session"]

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; media-src 'self' blob:; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    def static(name: str) -> FileResponse:
        if name not in {"orb.js", "orb.css"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request) -> JSONResponse:
        if not login_limit.allow(_client_ip(request)):
            raise HTTPException(429, "too_many_attempts")
        if not hmac.compare_digest(body.passcode.encode(), passcode.encode()):
            raise HTTPException(403, "wrong_passcode")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            make_token(),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
        )
        return response

    @app.post("/api/logout")
    def logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/state")
    def get_state(request: Request) -> dict[str, Any]:
        if not authed(request):
            return {"authed": False}
        sess = session()
        return {
            "authed": True,
            "model_ready": sess is not None,
            "error": state["error"],
            "tools": sorted(t.name for t in sess.registry) if sess is not None else [],
            "stt_ready": stt is not None,
        }

    @app.post("/api/ask")
    def ask(body: AskBody, request: Request) -> dict[str, str]:
        require(request)
        if not ask_limit.allow(_client_ip(request)):
            raise HTTPException(429, "slow_down")
        sess = session()
        if sess is None:
            raise HTTPException(503, state["error"] or "model_not_configured")
        if any(j.status in {"running", "approval"} for j in jobs.values()):
            raise HTTPException(409, "busy")
        for old in [k for k, j in jobs.items() if time.time() - j.created > 600]:
            jobs.pop(old, None)
        job = Job(uuid.uuid4().hex)
        jobs[job.id] = job

        def approve(tool: str, arguments: dict[str, Any]) -> bool:
            job._event.clear()
            job.approval = {
                "tool": tool,
                "arguments": {k: str(v)[:300] for k, v in arguments.items()},
            }
            job.status = "approval"
            answered = job._event.wait(120)
            job.approval = None
            job.status = "running"
            return answered and job._allowed

        def run() -> None:
            try:
                result = sess.ask(
                    body.text,
                    approve=approve,
                    on_step=lambda step: job.steps.append(f"{step.tool_name}:{step.status}"),
                )
                if result.stopped_reason == "model_failed":
                    job.error, job.status = "model_unavailable", "failed"
                else:
                    job.answer, job.status = result.answer.strip() or "…", "done"
            except Exception:
                job.error, job.status = "model_unavailable", "failed"

        threading.Thread(target=run, daemon=True).start()
        return {"job": job.id}

    @app.get("/api/job/{job_id}")
    def get_job(job_id: str, request: Request) -> dict[str, Any]:
        require(request)
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404)
        return {
            "status": job.status,
            "answer": job.answer,
            "error": job.error,
            "approval": job.approval,
            "steps": job.steps[-6:],
        }

    @app.post("/api/job/{job_id}/approve")
    def approve_job(job_id: str, body: ApproveBody, request: Request) -> dict[str, bool]:
        require(request)
        job = jobs.get(job_id)
        if job is None or job.status != "approval":
            raise HTTPException(409, "no_pending_approval")
        job._allowed = body.allow
        job._event.set()
        return {"ok": True}

    @app.post("/api/reset")
    def reset(request: Request) -> dict[str, bool]:
        require(request)
        sess = session()
        if sess is not None:
            sess.reset()
        return {"ok": True}

    @app.get("/api/reminders/due")
    def due(request: Request) -> dict[str, list[str]]:
        require(request)
        sess = session()
        items = sess.due_reminders() if sess is not None else []
        return {"due": [item["text"] for item in items]}

    @app.post("/api/transcribe")
    async def transcribe_audio(request: Request) -> dict[str, str]:
        require(request)
        if stt is None:
            raise HTTPException(503, "stt_not_configured")
        if not stt_limit.allow(_client_ip(request)):
            raise HTTPException(429, "slow_down")
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("audio/"):
            raise HTTPException(415, "audio_only")
        audio = await request.body()
        if len(audio) > 10_000_000:
            raise HTTPException(413, "audio_too_large")
        if len(audio) < 200:
            raise HTTPException(400, "audio_too_short")
        try:
            text = await hear(audio, content_type, stt)
        except Exception:
            raise HTTPException(502, "stt_unavailable") from None
        return {"text": str(text).strip()[:4000]}

    @app.post("/api/speak")
    async def speak_text(body: SpeakBody, request: Request) -> Response:
        require(request)
        if not tts_limit.allow(_client_ip(request)):
            raise HTTPException(429, "slow_down")
        try:
            audio = await speak(body.text, voice, rate, pitch)
        except Exception:
            raise HTTPException(503, "tts_unavailable") from None
        if not audio:
            raise HTTPException(503, "tts_unavailable")
        return Response(audio, media_type="audio/mpeg")

    return app


def _default_session(env: dict[str, str]) -> Any:
    from max_core.assistant_session import AssistantSession, SessionOptions
    from max_core.gateway_trial import TrialSettings

    options = SessionOptions.from_env(env)
    options = SessionOptions(
        data_dir=options.data_dir,
        workspace=None,
        cloud_personal_tools=options.cloud_personal_tools,
        enable_shell=False,
        enable_web=options.enable_web,
        host_tools=False,
    )
    return AssistantSession(TrialSettings.from_env(env), options)


def main() -> int:
    import uvicorn

    try:
        app = create_app()
    except RuntimeError as exc:
        print(f"MAX web: {exc}")
        return 2
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - hosted service
        port=int(os.environ.get("PORT", "10000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
        server_header=False,
    )
    return 0
