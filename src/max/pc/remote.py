"""Opt-in phone companion for local MAX PC, intended for Tailscale Serve.

The listener binds ONLY to loopback, never the LAN. The operator can
separately expose it through private authenticated HTTPS via Tailscale Serve.
An ephemeral per-start bearer token is carried in the URL fragment (not
sent in the initial GET), then in an X-Max-Token header on commands.
No remote endpoint can approve a desktop keyboard action.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAX | PHONE COMPANION</title><style>
*{box-sizing:border-box}body{margin:0;background:#090604;color:#fce3bf;
font:16px system-ui,Segoe UI,Arial,sans-serif;min-height:100vh;padding:28px 18px}
main{margin:auto;max-width:540px}.title{font-size:26px;letter-spacing:.33em;font-weight:800}
.sub{font:12px Consolas,monospace;letter-spacing:.14em;color:#bc8852}
.orb{margin:40px auto 25px;width:154px;height:154px;border-radius:50%;
background:radial-gradient(circle,#ffe1a1 1%,#f9a33a 9%,#8c3913 26%,#251008 54%,#070503 65%);
box-shadow:0 0 55px #bd5a1755,0 0 12px #ffb35033 inset;border:1px solid #9e5927;
animation:pulse 3.2s ease-in-out infinite}
@keyframes pulse{50%{filter:brightness(1.5);transform:scale(1.04)}}
.card{background:#180f0b;border:1px solid #684322;border-radius:13px;padding:20px}
#log{min-height:155px;max-height:38vh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5;
font-size:14px;color:#f9d3a1}
form{display:flex;gap:8px;margin-top:12px}input{min-width:0;flex:1;background:#231409;color:#ffe1b9;
border:1px solid #8e572b;border-radius:8px;padding:15px;font:inherit}
button{background:#e89c43;color:#1b0e08;border:0;border-radius:8px;font-weight:800;padding:0 17px}
.warning{margin-top:18px;font-size:12px;color:#b68c6c;line-height:1.5}
</style></head><body><main><div class="title">MAX</div><div class="sub">PRIVATE REMOTE // AUTH REQUIRED</div>
<div class="orb" aria-hidden="true"></div>
<section class="card"><div id="log" role="log" aria-live="polite">Connected locally. Type “hi max” to wake MAX.</div>
<form id="form"><input id="message" type="text" maxlength="1000" autocomplete="off"
placeholder="Ask MAX..." aria-label="Message MAX"><button type="submit">SEND</button></form></section>
<p class="warning">Remote keyboard actions require confirmation on the gaming PC. The microphone stays on your phone only when you use your phone's own voice dictation. Do not share this private link.</p>
</main><script>
(function(){
'use strict';const token=decodeURIComponent(location.hash.slice(1));
const log=document.getElementById('log'),box=document.getElementById('message');
const send=document.querySelector('button');
if(!token){log.textContent='This link is missing its pairing token. Re-enable Phone Companion on your PC.';send.disabled=true;return;}
document.getElementById('form').addEventListener('submit',async e=>{
e.preventDefault();const msg=box.value.trim();if(!msg)return;
box.value='';send.disabled=true;log.textContent+='\n\nYOU: '+msg;
try{
const response=await fetch('/api/command',{method:'POST',
headers:{'Content-Type':'application/json','X-Max-Token':token},body:JSON.stringify({text:msg})});
const result=await response.json();
log.textContent+='\nMAX: '+(result.text||result.error||'No reply');
}catch(_){log.textContent+='\nSYSTEM: Connection unavailable.';}
finally{send.disabled=false;log.scrollTop=log.scrollHeight;}
});
})();
</script></body></html>"""


class PhoneCompanion:
    def __init__(self, bridge, *, port: int = 8765, now=time.monotonic):
        self.bridge = bridge
        self.port = port
        self._now = now
        self._server = None
        self._thread = None
        self._token = ""
        self._rate_lock = threading.Lock()
        self._last = -1e12

    @property
    def active(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> dict:
        if self.active:
            return {"ok": False, "message": "Phone Companion is already running."}
        self._token = secrets.token_urlsafe(32)
        companion = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return  # Do not log tokens, requests or message contents.

            def _host_allowed(self) -> bool:
                # Disallow DNS rebinding to an unrelated host. Tailscale Serve
                # may forward its HTTPS tailnet hostname or 127.0.0.1.
                host = self.headers.get("Host", "")
                try:
                    parsed = urlsplit("http://" + host)
                    name = (parsed.hostname or "").lower()
                    return name in {"127.0.0.1", "localhost"} or (
                        bool(re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net", name))
                    )
                except ValueError:
                    return False

            def _send(self, status: int, payload: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store, private")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'; connect-src 'self'; "
                                 "base-uri 'none'; form-action 'none'")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _json(self, status: int, obj: dict) -> None:
                self._send(status, json.dumps(obj).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_GET(self):
                if not self._host_allowed() or self.path != "/":
                    return self._json(404, {"error": "Not found"})
                self._send(200, _HTML.encode("utf-8"), "text/html; charset=utf-8")

            def do_POST(self):
                if not self._host_allowed() or self.path != "/api/command":
                    return self._json(404, {"error": "Not found"})
                origin = self.headers.get("Origin", "")
                if origin:
                    try:
                        from urllib.parse import urlsplit
                        parsed = urlsplit(origin)
                        if parsed.netloc.lower() != self.headers.get("Host", "").lower() or (
                            parsed.scheme not in {"http", "https"}
                        ):
                            return self._json(403, {"error": "Origin denied"})
                    except ValueError:
                        return self._json(403, {"error": "Origin denied"})
                supplied = self.headers.get("X-Max-Token", "")
                if not hmac.compare_digest(supplied, companion._token):
                    return self._json(401, {"error": "Pairing token required"})
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    return self._json(415, {"error": "JSON required"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not (0 < length <= 4096):
                    return self._json(413, {"error": "Request too large"})
                try:
                    obj = json.loads(self.rfile.read(length))
                except (UnicodeError, ValueError):
                    return self._json(400, {"error": "Invalid JSON"})
                text = obj.get("text") if isinstance(obj, dict) else None
                if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                    return self._json(400, {"error": "Message is empty or too long"})
                with companion._rate_lock:
                    timestamp = companion._now()
                    if timestamp - companion._last < .65:
                        return self._json(429, {"error": "Slow down"})
                    companion._last = timestamp
                # Copy the result: the PC UI event must retain its approval
                # while the phone response must never include the token.
                response = dict(companion.bridge.submit_remote(text))
                response.pop("approval", None)
                if response.get("action") == "approval":
                    response["text"] = "Approval pending on your PC. Confirm there, not on your phone."
                self._json(200, {
                    "text": response.get("text", ""),
                    "ok": bool(response.get("ok")),
                    "action": response.get("action", "unknown"),
                })

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_port
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return {
            "ok": True,
            "url": f"http://127.0.0.1:{self.port}/#{self._token}",
            "message": "Phone Companion runs on loopback only. To reach it from your "
                "phone, use private HTTPS Tailscale Serve (NOT Funnel).",
        }

    def stop(self) -> None:
        server = self._server
        self._server = None
        self._token = ""
        if server:
            server.shutdown()
            server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
