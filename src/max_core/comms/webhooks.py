"""Inbound webhooks: WhatsApp Cloud API and Exotel. Signed, deduplicated, untrusted."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from max_core.comms.store import CommsStore, normalize_phone
from max_core.tools import ToolError

MAX_BODY = 256_000


def verify_meta_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def parse_whatsapp(payload: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """(sender, text, message_id) for each inbound message in a Cloud API payload."""
    out: list[tuple[str, str, str]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            for msg in (change.get("value") or {}).get("messages", []) or []:
                kind = msg.get("type")
                if kind == "text":
                    text = str((msg.get("text") or {}).get("body", ""))
                elif kind in ("image", "audio", "video", "document", "sticker", "location"):
                    text = f"[{kind} received]"
                else:
                    text = f"[{kind or 'unknown'} message]"
                sender = "+" + str(msg.get("from", "")).lstrip("+")
                out.append((sender, text, str(msg.get("id", ""))))
    return out


def comms_router(store: CommsStore, env: Mapping[str, str]) -> APIRouter:
    router = APIRouter()
    verify_token = env.get("MAX_WHATSAPP_VERIFY_TOKEN", "")
    app_secret = env.get("MAX_WHATSAPP_APP_SECRET", "")
    exotel_secret = env.get("MAX_EXOTEL_WEBHOOK_SECRET", "")

    @router.get("/api/comms/whatsapp")
    def whatsapp_verify(request: Request) -> PlainTextResponse:
        q = request.query_params
        if (
            verify_token
            and q.get("hub.mode") == "subscribe"
            and hmac.compare_digest(q.get("hub.verify_token", ""), verify_token)
        ):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403)

    @router.post("/api/comms/whatsapp")
    async def whatsapp_inbound(request: Request) -> dict[str, int]:
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413)
        if not verify_meta_signature(app_secret, body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401)
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400) from None
        stored = sum(
            store.receive("whatsapp", sender, text, f"wa:{mid}" if mid else None)
            for sender, text, mid in parse_whatsapp(payload)
        )
        return {"stored": stored}

    def _exotel_ok(request: Request) -> None:
        # Exotel has no request signing; use a long secret in the webhook URL.
        given = request.query_params.get("key", "")
        if not exotel_secret or not hmac.compare_digest(given, exotel_secret):
            raise HTTPException(401)

    @router.api_route("/api/comms/exotel/incoming", methods=["GET", "POST"])
    async def exotel_incoming(request: Request) -> PlainTextResponse:
        _exotel_ok(request)
        params = dict(request.query_params)
        if request.method == "POST":
            params.update({k: str(v) for k, v in (await request.form()).items()})
        try:
            sender = normalize_phone(params.get("CallFrom", "") or params.get("From", ""))
        except ToolError:
            sender = "unknown"
        call_sid = params.get("CallSid", "")
        store.receive("call", sender, "Incoming call", f"ex:{call_sid}" if call_sid else None)
        return PlainTextResponse("ok")

    @router.api_route("/api/comms/exotel/status", methods=["GET", "POST"])
    async def exotel_status(request: Request) -> PlainTextResponse:
        _exotel_ok(request)
        return PlainTextResponse("ok")

    return router
