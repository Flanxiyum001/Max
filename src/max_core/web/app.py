"""MAX web orb: a passcode-protected, single-owner web version of the orb.

Run locally:   MAX_WEB_PASSCODE=... python -m max_core.web
Hosted (Render): see render.yaml and docs/M30_WEB_ORB.md.

Only the owner (whoever knows MAX_WEB_PASSCODE) can use it. Tools that act on
the host machine (shell, apps, PC status, workspace files) are never enabled
on the web server.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from max_core.web.github_connect import (
    GitHubConnectionError,
    WebGitHubConnector,
    github_connect_intent,
)

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


log = logging.getLogger("max_core.web")


@dataclass
class Job:
    id: str
    status: str = "running"  # running | approval | done | failed
    answer: str = ""
    error: str | None = None
    detail: str | None = None
    approval: dict[str, Any] | None = None
    connect: dict[str, str] | None = None
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
class TtsConfig:
    url: str
    api_key: str | None
    model: str
    voice: str


def tts_config(env: dict[str, str]) -> TtsConfig | None:
    """Backup voice: an OpenAI-compatible /v1/audio/speech endpoint (FreeLLMAPI has one)."""
    if env.get("MAX_TTS_FALLBACK", "").lower() in {"off", "no", "0"}:
        return None
    url = env.get("MAX_TTS_ENDPOINT", "").strip()
    if not url:
        chat = env.get("MAX_TRIAL_ENDPOINT", "").strip()
        if not chat.endswith("/chat/completions"):
            return None
        url = chat[: -len("/chat/completions")] + "/audio/speech"
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        return None
    return TtsConfig(
        url=url,
        api_key=env.get("MAX_TTS_API_KEY") or env.get("MAX_TRIAL_API_KEY") or None,
        model=env.get("MAX_TTS_MODEL", "tts-1"),
        voice=env.get("MAX_TTS_VOICE", "nova"),
    )


async def synthesize_api(text: str, cfg: TtsConfig) -> bytes:
    import httpx

    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    payload = {"model": cfg.model, "voice": cfg.voice, "input": text, "response_format": "mp3"}
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.post(cfg.url, json=payload, headers=headers)
    response.raise_for_status()
    if not response.headers.get("content-type", "").startswith("audio/"):
        raise ValueError("not_audio")
    return response.content[:8_000_000]


@dataclass(frozen=True)
class SttConfig:
    url: str
    api_key: str | None
    model: str
    language: str | None
    prompt: str = ""


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
        prompt=stt_prompt(env.get("MAX_STT_VOCAB", "")),
    )


_STT_PROMPT = "Hey MAX. MAX is the owner's personal assistant."


def stt_prompt(vocab: str, context: str = "") -> str:
    """Whisper's prompt biases spelling toward names/terms the owner uses and the topic.

    Kept short: Whisper only reads the last ~224 tokens of it.
    """
    parts = [_STT_PROMPT]
    words = ", ".join(w.strip() for w in vocab.split(",") if w.strip())[:300]
    if words:
        parts.append(f"Words that may come up: {words}.")
    if context:
        parts.append(" ".join(context.split())[-400:])
    return " ".join(parts)


# Phrases Whisper is known to invent on silence or noise; never act on them alone.
_HALLUCINATIONS = {
    "thanks for watching!",
    "thanks for watching.",
    "thank you for watching.",
    "thank you for watching!",
    "please subscribe.",
    "subtitles by the amara.org community",
    "you",
    ".",
    "...",
}


def clean_transcript(text: str) -> str:
    text = " ".join(str(text).split())
    return "" if text.lower() in _HALLUCINATIONS else text[:4000]


async def transcribe(audio: bytes, content_type: str, cfg: SttConfig) -> str:
    import httpx

    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/wav": "wav",
           "audio/mpeg": "mp3"}.get(content_type.split(";")[0].strip(), "webm")  # fmt: skip
    data = {"model": cfg.model, "response_format": "json", "temperature": "0",
            "prompt": cfg.prompt or _STT_PROMPT}  # fmt: skip
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
    tts_backup: Any = None,
    piper: Any = None,
    github_connector: WebGitHubConnector | None = None,
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
    backup_cfg = tts_config(env)
    backup = tts_backup or synthesize_api
    stt = stt_config(env)
    stt_vocab = env.get("MAX_STT_VOCAB", "")
    hear = transcriber or transcribe
    github = github_connector if github_connector is not None else WebGitHubConnector(env)

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
                        state["session"] = _default_session(env, github_connector=github)
                except Exception as exc:
                    state["error"] = "model_not_configured"
                    # TrialConfigurationError messages are fixed codes, never secrets.
                    log.warning("model not configured: %s", str(exc)[:80] or type(exc).__name__)
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
            "github": github.status(),
        }

    @app.post("/api/connect/github/start")
    def start_github(request: Request) -> dict[str, str]:
        require(request)
        if not ask_limit.allow(_client_ip(request)):
            raise HTTPException(429, "slow_down")
        try:
            return {"url": github.start()}
        except GitHubConnectionError as exc:
            raise HTTPException(503, exc.code) from None

    @app.get("/api/connect/github/callback")
    def github_callback(state: str = "", code: str = "", error: str = "") -> RedirectResponse:
        # GitHub sends a cross-site browser navigation: SameSite=Strict cookies
        # are intentionally absent. The short-lived, one-use OAuth state is
        # the CSRF token, created ONLY by the authenticated owner.
        if error or not state or not code:
            return RedirectResponse("/?github=denied", status_code=303)
        try:
            github.finish(state, code)
        except GitHubConnectionError:
            return RedirectResponse("/?github=failed", status_code=303)
        return RedirectResponse("/?github=connected", status_code=303)

    @app.post("/api/connect/github/disconnect")
    def disconnect_github(request: Request) -> dict[str, bool]:
        require(request)
        github.disconnect()
        return {"ok": True}

    @app.post("/api/ask")
    def ask(body: AskBody, request: Request) -> dict[str, str]:
        require(request)
        if not ask_limit.allow(_client_ip(request)):
            raise HTTPException(429, "slow_down")
        intent = github_connect_intent(body.text)
        if intent is not None:
            # Host-routed intent: the model cannot fabricate OAuth links or
            # authorize external accounts by including instructions in text.
            if any(j.status in {"running", "approval"} for j in jobs.values()):
                raise HTTPException(409, "busy")
            job = Job(uuid.uuid4().hex, status="done")
            if intent == "disconnect":
                github.disconnect()
                job.answer = "GitHub disconnected. Its MCP tools are no longer available."
            elif not github.configured:
                job.answer = (
                    "I can set up GitHub for you, but this MAX server still needs "
                    "its GitHub OAuth application configured by the operator. "
                    "I will not ask you to paste tokens into chat."
                )
                job.connect = {"service": "github", "action": "needs_configuration"}
            elif github.status()["connected"]:
                job.answer = "GitHub is already connected to MAX."
                job.connect = {"service": "github", "action": "connected"}
            else:
                job.answer = (
                    "I'll handle the GitHub MCP endpoint and tools. "
                    "Approve the GitHub authorization in your browser to continue. "
                    "GitHub OAuth may request broad repository access; "
                    "every remote tool invocation still requires your approval."
                )
                job.connect = {"service": "github", "action": "authorize"}
            jobs[job.id] = job
            return {"job": job.id}
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
            # Remote operation approvals must show EXACT arguments. Never silently
            # truncate the values the owner is asked to authorize.
            if tool.startswith("mcp.github."):
                try:
                    encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=True)
                except (TypeError, ValueError):
                    return False
                if len(encoded) > 8192:
                    return False
                safe_arguments = dict(arguments)
            else:
                safe_arguments = {k: str(v)[:300] for k, v in arguments.items()}
            job.approval = {"tool": tool, "arguments": safe_arguments}
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
                    job.detail = getattr(result, "error_code", None)
                else:
                    job.answer, job.status = result.answer.strip() or "…", "done"
            except Exception as exc:
                log.warning("turn failed: %s", type(exc).__name__)
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
            "detail": job.detail,
            "approval": job.approval,
            "connect": job.connect,
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
        cfg = stt
        sess = state["session"]
        history = getattr(sess, "history", None) or []
        last = next((m.content for m in reversed(history) if m.role == "assistant"), "")
        if last:  # what MAX just said is the best hint for what comes next
            cfg = dataclasses.replace(stt, prompt=stt_prompt(stt_vocab, last))
        try:
            text = await hear(audio, content_type, cfg)
        except Exception as exc:
            log.warning("transcription failed: %s", type(exc).__name__)
            raise HTTPException(502, "stt_unavailable") from None
        return {"text": clean_transcript(text)}

    @app.post("/api/speak")
    async def speak_text(body: SpeakBody, request: Request) -> Response:
        require(request)
        if not tts_limit.allow(_client_ip(request)):
            raise HTTPException(429, "slow_down")
        audio = b""
        if piper is not None and piper.ready:
            try:
                audio = await asyncio.to_thread(piper.synthesize, body.text)
            except Exception as exc:
                log.warning("piper voice failed: %s", type(exc).__name__)
        if not audio:
            try:
                audio = await speak(body.text, voice, rate, pitch)
            except Exception as exc:
                log.warning("neural voice failed: %s", type(exc).__name__)
        if not audio and backup_cfg is not None:
            try:
                audio = await backup(body.text, backup_cfg)
            except Exception as exc:
                log.warning("backup voice failed: %s", type(exc).__name__)
        if not audio:
            # The browser then falls back to its own built-in voice.
            raise HTTPException(503, "tts_unavailable")
        kind = "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg"
        return Response(audio, media_type=kind)

    return app


def _default_session(
    env: dict[str, str], *, github_connector: WebGitHubConnector | None = None
) -> Any:
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
    return AssistantSession(TrialSettings.from_env(env), options, github_connector=github_connector)


def main() -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
    from max_core.web.piper_voice import PiperSpeaker

    cache = Path(os.environ.get("MAX_DATA_DIR", "/tmp/max-data")) / "voices"  # noqa: S108
    speaker = PiperSpeaker(dict(os.environ), cache)
    try:
        app = create_app(piper=speaker)
    except RuntimeError as exc:
        print(f"MAX web: {exc}")
        return 2
    speaker.start()
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
