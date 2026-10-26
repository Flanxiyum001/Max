"""OpenAI-compatible Chat Completions adapter (FreeLLM, vLLM, Ollama, ...).

Design goals:
* configurable base URL + model identifier,
* API key only from the environment,
* bounded timeouts and retries,
* precise, classified errors that never crash the caller,
* no secret material in any error message.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from .base import (
    ChatProvider,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimit,
    ProviderResult,
    ProviderServerError,
    ProviderTimeout,
    ToolCall,
)

_MAX_BODY_BYTES = 4 * 1024 * 1024
_ERROR_SNIPPET = 400  # chars of provider error body echoed back (key-free)


class OpenAICompatProvider(ChatProvider):
    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        retries: int = 1,
        opener=None,
        sleep=time.sleep,
    ):
        if not base_url:
            raise ProviderConfigError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    # ------------------------------------------------------------- public
    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> ProviderResult:
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
                for t in tools
            ]

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "max-assistant/0.2",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        attempt = 0
        while True:
            started = time.monotonic()
            try:
                request = urllib.request.Request(url, data=body, headers=headers, method="POST")
                with self._open(request, timeout=self.timeout) as response:
                    raw = response.read(_MAX_BODY_BYTES)
            except urllib.error.HTTPError as exc:
                err = self._classify_http(exc)
                if err.retryable and attempt < self.retries:
                    attempt += 1
                    self._sleep(min(0.5 * (2 ** (attempt - 1)), 2.0))
                    continue
                raise err from None
            except (TimeoutError, socket.timeout) as exc:
                err = ProviderTimeout(f"request exceeded {self.timeout}s: {exc.__class__.__name__}")
                if attempt < self.retries:
                    attempt += 1
                    self._sleep(0.5)
                    continue
                raise err from None
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, (TimeoutError, socket.timeout)):
                    err = ProviderTimeout(f"request exceeded {self.timeout}s")
                    if attempt < self.retries:
                        attempt += 1
                        self._sleep(0.5)
                        continue
                    raise err from None
                err = ProviderServerError(
                    f"connection failed: {reason}", code="connection_error", retryable=True
                )
                if attempt < self.retries:
                    attempt += 1
                    self._sleep(0.5)
                    continue
                raise err from None
            except OSError as exc:
                raise ProviderServerError(
                    f"network failure: {exc.__class__.__name__}", code="connection_error"
                ) from None

            latency_ms = (time.monotonic() - started) * 1000.0
            return self._parse(raw, latency_ms)

    # ------------------------------------------------------------ helpers
    def _classify_http(self, exc: urllib.error.HTTPError) -> ProviderError:
        status = getattr(exc, "code", 0) or 0
        try:
            snippet = exc.read(_ERROR_SNIPPET).decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001 - reading an error body must not mask the error
            snippet = ""
        # Never echo request headers, so the API key cannot leak.
        detail = f"HTTP {status}" + (f": {snippet}" if snippet else "")
        if status in (401, 403):
            return ProviderAuthError(detail, code=f"http_{status}")
        if status == 429:
            return ProviderRateLimit(detail, code="http_429")
        if status >= 500:
            return ProviderServerError(detail, code=f"http_{status}")
        return ProviderServerError(detail, code=f"http_{status}", retryable=False)

    def _parse(self, raw: bytes, latency_ms: float) -> ProviderResult:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderServerError(
                f"provider returned invalid JSON ({exc.__class__.__name__})", code="bad_json"
            ) from None
        if not isinstance(data, dict) or "choices" not in data:
            raise ProviderServerError(
                "provider response missing 'choices'", code="bad_shape"
            )
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderServerError("provider returned no choices", code="empty_choices")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        tool_calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except ValueError:
                    args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {"_raw": raw_args}
            tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {"_raw": args},
                )
            )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ProviderResult(
            text=text,
            tool_calls=tool_calls,
            usage={k: int(v) for k, v in usage.items() if isinstance(v, (int, float))},
            latency_ms=latency_ms,
            model=str(data.get("model") or self.model),
            raw={"id": data.get("id")},
        )

    def health(self) -> dict:
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "key_configured": bool(self.api_key),
        }
