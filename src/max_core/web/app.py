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
import re
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

from max_core.web.github_connect import GitHubConnectionError, WebGitHubConnector
from max_core.web.mcp_hub import API_KEY_SERVICES, MCPHubError, WebMCPHub, connection_intent
from max_core.web.mcp_hub import CATALOG as MCP_CATALOG
from max_core.web.pr_sandbox import (
    GitHubPRSandbox,
    SandboxError,
    SandboxRun,
    parse_sandbox_request,
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


class ApiKeyBody(BaseModel):
    key: str = Field(min_length=8, max_length=300)


class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


log = logging.getLogger("max_core.web")
_APPS_QUESTION = re.compile(
    r"\b(which|what)\b.{0,40}\b(apps?|connections?|integrations?|connected)\b", re.I
)


@dataclass
class Job:
    id: str
    status: str = "running"  # running | approval | done | failed
    answer: str = ""
    error: str | None = None
    detail: str | None = None
    approval: dict[str, Any] | None = None
    connect: dict[str, str] | None = None
    sandbox_run: SandboxRun | None = None
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
    "thanks for watching",
    "thank you for watching.",
    "thank you for watching!",
    "thank you for watching",
    "please subscribe.",
    "please subscribe!",
    "please subscribe",
    "like and subscribe.",
    "like and subscribe",
    "subtitles by the amara.org community",
    "thank you.",
    "thanks.",
    "bye.",
    "bye!",
    "you",
    "you.",
    ".",
    "...",
}

_NON_SPEECH_TAG_RE = re.compile(r"(\[[^\]]{1,40}\]|\([^)]{1,40}\)|♪[^♪]{0,80}♪)")


def clean_transcript(text: str) -> str:
    cleaned = _NON_SPEECH_TAG_RE.sub(" ", str(text))
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned or cleaned.lower() in _HALLUCINATIONS:
        return ""
    return cleaned[:4000]


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
    mcp_hub: WebMCPHub | None = None,
    sandbox_runner: GitHubPRSandbox | None = None,
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
    hub = mcp_hub if mcp_hub is not None else WebMCPHub(env, github_connector=github)
    sandbox = sandbox_runner if sandbox_runner is not None else GitHubPRSandbox(hub.github)

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
                        state["session"] = _default_session(env, github_connector=hub)
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
            "connections": hub.status(),
        }

    @app.get("/.well-known/oauth-client-metadata/max.json")
    def mcp_client_metadata() -> dict[str, Any]:
        try:
            return hub.client_metadata()
        except MCPHubError as error:
            raise HTTPException(503, error.code) from None

    @app.post("/api/connect/mcp/{service}/start")
    def start_mcp(service: str, request: Request) -> dict[str, str]:
        require(request)
        if not ask_limit.allow(_client_ip(request)):
            raise HTTPException(429, "slow_down")
        try:
            return {"url": hub.start(service)}
        except MCPHubError as error:
            raise HTTPException(503, error.code) from None

    @app.post("/api/connect/mcp/{service}/key")
    def connect_mcp_key(service: str, body: ApiKeyBody, request: Request) -> dict[str, Any]:
        require(request)
        if not login_limit.allow(_client_ip(request)):
            raise HTTPException(429, "slow_down")
        try:
            tools = hub.connect_with_key(service, body.key)
        except MCPHubError as error:
            raise HTTPException(400, error.code) from None
        return {"ok": True, "tools": tools}

    @app.get("/api/connect/mcp/callback")
    def mcp_callback(
        state: str = "", code: str = "", error: str = "", iss: str = ""
    ) -> RedirectResponse:
        # State+PKCE are per-connection, single-use. SameSite=Strict cookies
        # intentionally do not participate in a provider-to-MAX redirect.
        if error or not state or not code:
            return RedirectResponse("/?mcp=denied", status_code=303)
        try:
            hub.finish(state, code, iss)
        except MCPHubError:
            return RedirectResponse("/?mcp=failed", status_code=303)
        return RedirectResponse("/?mcp=connected", status_code=303)

    @app.post("/api/connect/mcp/{service}/disconnect")
    def disconnect_mcp(service: str, request: Request) -> dict[str, bool]:
        require(request)
        try:
            hub.disconnect(service)
        except MCPHubError as error:
            raise HTTPException(404, error.code) from None
        return {"ok": True}

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
        sandbox_request = parse_sandbox_request(body.text)
        if sandbox_request is not None:
            if any(j.status in {"running", "approval"} for j in jobs.values()):
                raise HTTPException(409, "busy")
            repository, pr_number = sandbox_request
            job = Job(uuid.uuid4().hex, status="done")
            if not hub.github.status()["connected"]:
                job.answer = (
                    "I need an authorized GitHub connection before I can run a PR sandbox. "
                    "Connect GitHub first; I won't execute untrusted PR code on Render."
                )
                job.connect = {
                    "service": "github",
                    "action": "authorize" if hub.github.configured else "needs_configuration",
                }
                jobs[job.id] = job
                return {"job": job.id}
            try:
                target = sandbox.preview(repository, pr_number)
            except SandboxError as error:
                job.answer = {
                    "sandbox_not_installed_or_pr_not_found": "This repository needs MAX's sandbox workflow installed on its "
                    "default branch, or that PR does not exist. I haven't run anything.",
                    "sandbox_repo_owner_not_authorized": "I only run sandbox jobs in repositories owned by the connected account.",
                    "sandbox_only_open_same_repo_prs": "This sandbox currently supports open PRs from branches in the same repository.",
                }.get(error.args[0], "I couldn't prepare a safe sandbox for that PR.")
                jobs[job.id] = job
                return {"job": job.id}
            job.status = "approval"
            job.approval = {
                "tool": "sandbox.run_pr",
                "arguments": {
                    "repository": target.repository,
                    "pull_request": target.pr_number,
                    "pinned_sha": target.head_sha,
                    "action": "Run temporary, read-only GitHub Actions checks; no merge",
                },
            }
            jobs[job.id] = job

            def run_sandbox() -> None:
                approved = job._event.wait(120) and job._allowed
                job.approval = None
                if not approved:
                    job.answer = "PR sandbox cancelled or approval expired; nothing was run."
                    job.status = "done"
                    return
                job.status = "running"
                try:
                    run = sandbox.dispatch(target)
                    job.sandbox_run = run
                    job.answer = (
                        f"Sandbox requested for {run.repository} PR #{run.pr_number} "
                        f"at commit {run.head_sha[:12]}. "
                        "The isolated runner will validate that exact commit. "
                        "This is NOT a merge approval. "
                        + (
                            f"Run: {run.run_url}"
                            if run.run_url
                            else "Check GitHub Actions for the run."
                        )
                    )
                    job.status = "done"
                except SandboxError as error:
                    job.error = "sandbox_failed"
                    job.detail = error.args[0]
                    job.status = "failed"

            threading.Thread(target=run_sandbox, daemon=True).start()
            return {"job": job.id}

        if _APPS_QUESTION.search(body.text[:200]) and connection_intent(body.text) is None:
            services = hub.status()["services"]
            names = {"github": "GitHub", **{k: v.name for k, v in MCP_CATALOG.items()}}
            on = [names[k] for k, v in services.items() if v.get("connected")]
            off = [names[k] for k, v in services.items() if not v.get("connected")]
            job = Job(uuid.uuid4().hex, status="done")
            job.answer = (
                ("Connected: " + ", ".join(on) + ". " if on else "No apps are connected yet. ")
                + "You can connect "
                + ", ".join(off)
                + ' by saying "connect to" and the app name.'
            )
            jobs[job.id] = job
            return {"job": job.id}
        intent = connection_intent(body.text)
        if intent is not None:
            # Trusted, authenticated user intent; model output cannot add apps.
            if any(j.status in {"running", "approval"} for j in jobs.values()):
                raise HTTPException(409, "busy")
            action, service = intent
            label = service.capitalize()
            job = Job(uuid.uuid4().hex, status="done")
            if action == "disconnect":
                hub.disconnect(service)
                job.answer = f"{label} disconnected. Its MCP tools are no longer available."
            elif service == "github" and not github.configured:
                job.answer = (
                    "I can prepare GitHub for you, but this deployment needs a one-time "
                    "GitHub OAuth App registration. I won't ask you to paste a token in chat."
                )
                job.connect = {"service": service, "action": "needs_configuration"}
            elif service in API_KEY_SERVICES and not hub.status()["services"][service]["connected"]:
                job.answer = (
                    f"{label} doesn't offer one-click sign-in, so I need an API key once. "
                    f"Open {label}'s key page, create a key, and paste it in the box. "
                    "I'll keep it encrypted and ask before every action."
                )
                job.connect = {
                    "service": service,
                    "action": "api_key",
                    "url": API_KEY_SERVICES[service][0],
                }
            elif service != "github" and not hub.configured:
                job.answer = (
                    "MAX needs its public HTTPS URL to register as an OAuth client. "
                    "On Render I detect that automatically; the deployment has no public URL yet."
                )
                job.connect = {"service": service, "action": "needs_configuration"}
            elif hub.status()["services"][service]["connected"]:
                job.answer = f"{label} is already connected to MAX."
                job.connect = {"service": service, "action": "connected"}
            else:
                job.answer = (
                    "I'll discover the official " + label + " MCP server and handle setup. "
                    "Approve access on the provider's authorization page. "
                    "Every remote action will still ask for your approval."
                )
                if service == "github":
                    job.answer = (
                        "I'll handle the GitHub MCP endpoint and tools. "
                        "Approve the GitHub authorization in your browser to continue. "
                        "GitHub OAuth may request broad repository access; "
                        "every remote tool invocation still requires your approval."
                    )
                job.connect = {"service": service, "action": "authorize"}
            jobs[job.id] = job
            return {"job": job.id}
        if any(j.status in {"running", "approval"} for j in jobs.values()):
            raise HTTPException(409, "busy")
        sess = session()
        if sess is None:
            raise HTTPException(503, state["error"] or "model_not_configured")
        for old in [k for k, j in jobs.items() if time.time() - j.created > 600]:
            jobs.pop(old, None)
        job = Job(uuid.uuid4().hex)
        jobs[job.id] = job

        def approve(tool: str, arguments: dict[str, Any]) -> bool:
            job._event.clear()
            # Remote operation approvals must show EXACT arguments. Never silently
            # truncate the values the owner is asked to authorize.
            if tool.startswith("mcp."):
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
            "sandbox": dataclasses.asdict(job.sandbox_run) if job.sandbox_run else None,
            "steps": job.steps[-6:],
        }

    @app.get("/api/sandbox/run/{job_id}")
    def sandbox_run_status(job_id: str, request: Request) -> dict[str, Any]:
        require(request)
        job = jobs.get(job_id)
        if job is None or job.sandbox_run is None:
            raise HTTPException(404, "unknown_sandbox_run")
        try:
            status = sandbox.status(job.sandbox_run)
        except SandboxError as error:
            raise HTTPException(502, error.args[0]) from None
        return {**dataclasses.asdict(job.sandbox_run), **status}

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
    env: dict[str, str], *, github_connector: WebGitHubConnector | WebMCPHub | None = None
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
