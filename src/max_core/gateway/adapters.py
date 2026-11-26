"""Explicitly configured HTTP adapters. No automatic provider discovery or credentials."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from max_core.gateway.contracts import (
    ChatMessage,
    Dialect,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    InferenceDestination,
    ModelSpec,
    StreamEvent,
    ToolDefinition,
    request_envelope_bytes,
)
from max_core.gateway.normalize import model_matches, normalize_anthropic, normalize_openai
from max_core.gateway.privacy_guard import contains_high_confidence_secret
from max_core.gateway.streaming import anthropic_events, openai_events, sse_frames

MAX_RESPONSE_BYTES = 1_000_000


_MAX_JSON_UNESCAPE_LAYERS = 10
_JSON_SIMPLE_ESCAPES: dict[str, str] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "'": "'",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_HEX_CHARS = frozenset("0123456789abcdefABCDEF")
_EMBEDDED_SCAN_TRIGGERS = ('"', "'", "\\", "{", "[")
_UNQUOTED_OBJ_KV_RE = re.compile(
    r"(?:^|[{,\[])[ \t\r\n]*([A-Za-z_][A-Za-z0-9_]{0,127})[ \t]*:[ \t]*"
    r"(?:\"((?:[^\"\\\r\n]|\\.)*?)\"|'((?:[^'\\\r\n]|\\.)*?)'|([^\s,}\]\"'#;]{8,}))"
)


def _unescape_json_layer(text: str) -> tuple[str, bool]:
    first = text.find("\\")
    if first == -1:
        return text, False
    n = len(text)
    parts: list[str] = []
    pos = 0
    changed = False
    idx = first
    while idx != -1:
        if idx > pos:
            parts.append(text[pos:idx])
        if idx + 1 >= n:
            parts.append("\\")
            pos = n
            break
        esc = text[idx + 1]
        mapped = _JSON_SIMPLE_ESCAPES.get(esc)
        if mapped is not None:
            parts.append(mapped)
            pos = idx + 2
            changed = True
        elif esc == "u" and idx + 6 <= n and all(c in _HEX_CHARS for c in text[idx + 2 : idx + 6]):
            code = int(text[idx + 2 : idx + 6], 16)
            pos = idx + 6
            if (
                0xD800 <= code <= 0xDBFF
                and pos + 6 <= n
                and text[pos : pos + 2] == "\\u"
                and all(c in _HEX_CHARS for c in text[pos + 2 : pos + 6])
            ):
                low = int(text[pos + 2 : pos + 6], 16)
                if 0xDC00 <= low <= 0xDFFF:
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    pos += 6
            parts.append("\ufffd" if 0xD800 <= code <= 0xDFFF else chr(code))
            changed = True
        else:
            parts.append(text[idx : idx + 2])
            pos = idx + 2
        idx = text.find("\\", pos)
    if pos < n:
        parts.append(text[pos:])
    if not changed:
        return text, False
    return "".join(parts), True


def _is_escaped_quote(s: str, pos: int) -> bool:
    bs = 0
    k = pos - 1
    while k >= 0 and s[k] == "\\":
        bs += 1
        k -= 1
    return (bs & 1) == 1


def _scan_json_number_end(s: str, v: int, n: int) -> int:
    p = v
    if p < n and s[p] == "-":
        p += 1
    digits_start = p
    while p < n and "0" <= s[p] <= "9":
        p += 1
    if p == digits_start:
        return -1
    if p < n and s[p] == ".":
        p += 1
        frac_start = p
        while p < n and "0" <= s[p] <= "9":
            p += 1
        if p == frac_start:
            return -1
    if p < n and s[p] in "eE":
        p += 1
        if p < n and s[p] in "+-":
            p += 1
        exp_start = p
        while p < n and "0" <= s[p] <= "9":
            p += 1
        if p == exp_start:
            return -1
    if p < n and s[p] not in " \t\r\n,}]":
        return -1
    return p


def _extract_quoted_kv(s: str, quote: str, out: list[str]) -> None:
    n = len(s)
    idx = 0
    while idx < n:
        q_start = s.find(quote, idx)
        if q_start == -1:
            break
        if _is_escaped_quote(s, q_start):
            idx = q_start + 1
            continue
        j = q_start + 1
        has_escape = False
        hit_newline = False
        while j < n:
            ch = s[j]
            if ch == "\\":
                has_escape = True
                j += 2
                continue
            if ch == quote:
                break
            if ch in "\r\n":
                hit_newline = True
                break
            j += 1
        if hit_newline:
            raw_line = s[q_start + 1 : j]
            if "=" in raw_line or ":" in raw_line or has_escape:
                line_val = _unescape_json_layer(raw_line)[0] if has_escape else raw_line
                if line_val:
                    out.append(line_val)
            idx = j + 1
            continue
        if j >= n:
            tail = s[q_start + 1 : n]
            if "=" in tail or ":" in tail or has_escape:
                tail_val = _unescape_json_layer(tail)[0] if has_escape else tail
                if tail_val:
                    out.append(tail_val)
            break

        raw_key = s[q_start + 1 : j]
        key_val = _unescape_json_layer(raw_key)[0] if has_escape else raw_key
        if key_val and ("=" in key_val or ":" in key_val or has_escape):
            out.append(key_val)

        k = j + 1
        while k < n and s[k] in " \t\r\n":
            k += 1
        if k < n and s[k] == ":" and key_val:
            v = k + 1
            while v < n and s[v] in " \t\r\n":
                v += 1
            if v < n:
                if s[v] in ('"', "'"):
                    vq = s[v]
                    vj = v + 1
                    val_has_escape = False
                    while vj < n:
                        vch = s[vj]
                        if vch == "\\":
                            val_has_escape = True
                            vj += 2
                            continue
                        if vch == vq:
                            break
                        vj += 1
                    raw_val = s[v + 1 : min(vj, n)]
                    val_str = _unescape_json_layer(raw_val)[0] if val_has_escape else raw_val
                    if val_str:
                        out.append(val_str)
                        out.append(f"{key_val}={val_str}")
                        stripped_val = val_str.lstrip("\r\n")
                        if stripped_val and stripped_val != val_str:
                            out.append(f"{key_val}={stripped_val}")
                elif s[v] in "-0123456789":
                    num_end = _scan_json_number_end(s, v, n)
                    if num_end > v:
                        out.append(f"{key_val}={s[v:num_end]}")
        idx = j


def _scan_embedded_json_and_escapes(text: str, out: list[str]) -> None:
    current = text
    for depth in range(_MAX_JSON_UNESCAPE_LAYERS + 1):
        if '"' in current:
            _extract_quoted_kv(current, '"', out)
        if "'" in current:
            _extract_quoted_kv(current, "'", out)
        if "{" in current or "[" in current or "," in current:
            for match in _UNQUOTED_OBJ_KV_RE.finditer(current):
                k = match.group(1)
                v = (
                    match.group(2)
                    if match.group(2) is not None
                    else (match.group(3) if match.group(3) is not None else match.group(4))
                )
                if v:
                    if "\\" in v:
                        v = _unescape_json_layer(v)[0]
                    out.append(v)
                    out.append(f"{k}={v}")
        if depth == _MAX_JSON_UNESCAPE_LAYERS or "\\" not in current:
            break
        nxt, changed = _unescape_json_layer(current)
        if not changed or nxt == current:
            break
        out.append(nxt)
        current = nxt


def _walk_strings(value: Any, out: list[str]) -> None:
    """Iteratively extract strings, KEY=VALUE pairs, and embedded JSON without depth cutoff."""
    stack: list[Any] = [value]
    visited_containers: dict[int, Any] = {}
    parsed_strings: set[str] = set()

    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if not current:
                continue
            out.append(current)
            if current not in parsed_strings and any(
                ch in current for ch in _EMBEDDED_SCAN_TRIGGERS
            ):
                parsed_strings.add(current)
                _scan_embedded_json_and_escapes(current, out)
            continue

        if isinstance(current, dict):
            obj_id = id(current)
            if obj_id in visited_containers:
                continue
            visited_containers[obj_id] = current
            for key, item in current.items():
                if isinstance(key, str):
                    if key:
                        out.append(key)
                    if isinstance(item, str):
                        if item:
                            out.append(f"{key}={item}")
                    elif isinstance(item, (int, float)) and not isinstance(item, bool):
                        out.append(f"{key}={item}")
                stack.append(item)
            continue

        if isinstance(current, (list, tuple)):
            obj_id = id(current)
            if obj_id in visited_containers:
                continue
            visited_containers[obj_id] = current
            for item in current:
                stack.append(item)


def _chunked_scan_messages(texts: Sequence[str]) -> list[ChatMessage]:
    chunks: list[ChatMessage] = []
    chunk_size = 16_000
    overlap = 512
    step = chunk_size - overlap
    batch: list[str] = []
    batch_len = 0

    def flush_batch() -> None:
        nonlocal batch, batch_len
        if batch:
            chunks.append(ChatMessage("user", "\n".join(batch)))
            batch = []
            batch_len = 0

    for raw_text in texts:
        if not raw_text:
            continue
        text = raw_text.encode("utf-8", errors="replace").decode("utf-8")
        if len(text) > 32_000:
            flush_batch()
            for start in range(0, len(text), step):
                chunks.append(ChatMessage("user", text[start : start + chunk_size]))
            continue
        if len(text) > chunk_size:
            flush_batch()
            chunks.append(ChatMessage("user", text))
            continue
        added_len = len(text) if not batch else len(text) + 1
        if batch_len + added_len > chunk_size:
            flush_batch()
            batch.append(text)
            batch_len = len(text)
        else:
            batch.append(text)
            batch_len += added_len

    flush_batch()
    return chunks


def request_has_high_confidence_secret(
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolDefinition] = (),
) -> bool:
    """Check message content, tool calls, and tool definitions via privacy_guard."""
    if contains_high_confidence_secret(messages):
        return True
    extra_texts: list[str] = []
    for message in messages:
        if message.tool_call_id:
            extra_texts.append(message.tool_call_id)
        if message.content and (
            message.role == "tool" or any(ch in message.content for ch in _EMBEDDED_SCAN_TRIGGERS)
        ):
            _walk_strings(message.content, extra_texts)
        for call in message.tool_calls:
            extra_texts.append(call.call_id)
            extra_texts.append(call.name)
            try:
                extra_texts.append(
                    json.dumps(call.arguments, separators=(",", ":"), ensure_ascii=False)
                )
            except (TypeError, ValueError, RecursionError):
                pass
            _walk_strings(call.arguments, extra_texts)
    for tool in tools:
        extra_texts.append(tool.name)
        if tool.description:
            _walk_strings(tool.description, extra_texts)
        try:
            extra_texts.append(
                json.dumps(tool.parameters, separators=(",", ":"), ensure_ascii=False)
            )
        except (TypeError, ValueError, RecursionError):
            pass
        _walk_strings(tool.parameters, extra_texts)
    if not extra_texts:
        return False
    return contains_high_confidence_secret(_chunked_scan_messages(extra_texts))


def _messages(request: GatewayRequest, dialect: Dialect) -> dict[str, Any]:
    if dialect == Dialect.OPENAI_CHAT:
        openai_messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "tool":
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
            elif message.role == "assistant" and message.tool_calls:
                serialized_calls: list[dict[str, Any]] = []
                for call in message.tool_calls:
                    try:
                        encoded_args = json.dumps(
                            call.arguments,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                    except (TypeError, ValueError, RecursionError):
                        raise GatewayError("invalid_request") from None
                    serialized_calls.append(
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": encoded_args,
                            },
                        }
                    )
                openai_messages.append(
                    {
                        "role": "assistant",
                        "content": message.content if message.content else None,
                        "tool_calls": serialized_calls,
                    }
                )
            elif message.images:
                parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
                for image in message.images:
                    data = base64.b64encode(image).decode("ascii")
                    parts.append(
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}
                    )
                openai_messages.append({"role": message.role, "content": parts})
            else:
                openai_messages.append({"role": message.role, "content": message.content})
        return {"messages": openai_messages}

    system_parts = [m.content for m in request.messages if m.role == "system"]
    regular: list[dict[str, Any]] = []
    last_was_tool_result = False
    for message in request.messages:
        if message.role == "system":
            continue
        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            regular.append({"role": "assistant", "content": blocks})
            last_was_tool_result = False
        elif message.role == "tool":
            tool_block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            if message.is_error:
                tool_block["is_error"] = True
            if last_was_tool_result and regular:
                content_list = regular[-1]["content"]
                if isinstance(content_list, list):
                    content_list.append(tool_block)
                    continue
            regular.append({"role": "user", "content": [tool_block]})
            last_was_tool_result = True
        elif (
            message.role == "user"
            and regular
            and regular[-1]["role"] == "user"
            and isinstance(regular[-1]["content"], list)
        ):
            if message.content:
                regular[-1]["content"].append({"type": "text", "text": message.content})
            last_was_tool_result = False
        elif message.images:
            image_blocks: list[dict[str, Any]] = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image).decode("ascii"),
                    },
                }
                for image in message.images
            ]
            image_blocks.append({"type": "text", "text": message.content or "(screenshot)"})
            regular.append({"role": message.role, "content": image_blocks})
            last_was_tool_result = False
        else:
            regular.append({"role": message.role, "content": message.content})
            last_was_tool_result = False
    if not regular:
        raise GatewayError("invalid_request")
    result: dict[str, Any] = {"messages": regular}
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    return result


def _params(spec: ModelSpec, request: GatewayRequest, *, stream: bool) -> dict[str, Any]:
    if request.tools and "tools" not in spec.capabilities:
        raise GatewayError("unsupported_capability")
    if any(m.images for m in request.messages) and (
        "vision" not in spec.capabilities or not spec.is_local
    ):
        raise GatewayError("unsupported_capability")
    data = _messages(request, spec.dialect)
    data["model"] = spec.model_id
    data["stream"] = stream
    if spec.dialect == Dialect.OPENAI_CHAT:
        # Compatibility baseline. Not every OpenAI-like endpoint supports newer fields.
        data["max_tokens"] = request.max_output_tokens
        if request.tools:
            data["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        if stream:
            data["stream_options"] = {"include_usage": True}
    else:
        data["max_tokens"] = request.max_output_tokens
        if request.tools:
            data["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
    return data


def _headers(spec: ModelSpec, *, stream: bool) -> dict[str, str]:
    result = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if spec.dialect == Dialect.OPENAI_CHAT:
        if spec.api_key is not None:
            result["Authorization"] = f"Bearer {spec.api_key}"
    else:
        result["anthropic-version"] = "2023-06-01"
        if spec.api_key is not None:
            result["x-api-key"] = spec.api_key
    return result


def _check_status(status: int) -> None:
    if status in {429, 503}:
        raise GatewayError(
            "rate_limited" if status == 429 else "provider_unavailable", fallback_safe=True
        )
    if status in {401, 403}:
        raise GatewayError("auth_failure")
    if status == 400 or status == 422:
        raise GatewayError("invalid_request")
    if status >= 500:
        raise GatewayError("provider_unavailable")
    if not 200 <= status < 300:
        raise GatewayError("provider_error")


class HTTPAdapter:
    """Uses an injected HTTPX client. Redirects are never followed with credentials."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def complete(self, spec: ModelSpec, request: GatewayRequest) -> GatewayResponse:
        # GatewayRequest and ChatMessage are frozen, but nested arguments can be
        # mutated after construction. Validate immediately before every egress.
        try:
            request_envelope_bytes(request.messages, request.tools)
        except (TypeError, ValueError, RecursionError):
            raise GatewayError("invalid_request") from None
        # Defence in depth: even a direct adapter call must not leak known
        # credential patterns. This is not an exhaustive secret classifier.
        if (
            spec.inference_destination == InferenceDestination.CLOUD
            and request_has_high_confidence_secret(request.messages, request.tools)
        ):
            raise GatewayError("cloud_sensitive_content_blocked")
        try:
            body = json.dumps(
                _params(spec, request, stream=False),
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise GatewayError("invalid_request") from None
        try:
            async with self.client.stream(
                "POST",
                spec.endpoint,
                content=body,
                headers=_headers(spec, stream=False),
                timeout=request.timeout_seconds,
                follow_redirects=False,
            ) as response:
                _check_status(response.status_code)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise GatewayError("response_too_large")
                    chunks.extend(chunk)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Connection establishment failed before any HTTP request was sent.
            # Never classify read/write timeouts or interrupted responses as safe.
            raise GatewayError("provider_unavailable", fallback_safe=True) from None
        except httpx.TimeoutException:
            raise GatewayError("timeout") from None
        except httpx.RequestError:
            raise GatewayError("provider_unavailable") from None
        try:
            data = json.loads(chunks)
        except (ValueError, UnicodeError, RecursionError):
            raise GatewayError("malformed_provider_response") from None
        if not isinstance(data, dict):
            raise GatewayError("malformed_provider_response")
        if data.get("model") is not None and not model_matches(spec.model_id, data["model"]):
            raise GatewayError("model_mismatch")
        if spec.dialect == Dialect.OPENAI_CHAT:
            return normalize_openai(data, request.request_id, spec)
        return normalize_anthropic(data, request.request_id, spec)

    async def stream(self, spec: ModelSpec, request: GatewayRequest) -> AsyncIterator[StreamEvent]:
        try:
            request_envelope_bytes(request.messages, request.tools)
        except (TypeError, ValueError, RecursionError):
            raise GatewayError("invalid_request") from None
        if (
            spec.inference_destination == InferenceDestination.CLOUD
            and request_has_high_confidence_secret(request.messages, request.tools)
        ):
            raise GatewayError("cloud_sensitive_content_blocked")
        try:
            body = json.dumps(
                _params(spec, request, stream=True),
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise GatewayError("invalid_request") from None
        try:
            async with self.client.stream(
                "POST",
                spec.endpoint,
                content=body,
                headers=_headers(spec, stream=True),
                timeout=request.timeout_seconds,
                follow_redirects=False,
            ) as response:
                _check_status(response.status_code)
                frames = sse_frames(response.aiter_lines())
                if spec.dialect == Dialect.OPENAI_CHAT:
                    async for event in openai_events(frames, request, spec):
                        yield event
                else:
                    async for event in anthropic_events(frames, request, spec):
                        yield event
        except httpx.TimeoutException:
            raise GatewayError("timeout") from None
        except httpx.RequestError:
            raise GatewayError("stream_interrupted") from None
