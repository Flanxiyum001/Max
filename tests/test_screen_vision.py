from __future__ import annotations

import asyncio
import datetime as dt
import struct
import zlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from max_core.assistant_tools import assistant_registry
from max_core.gateway import ModelSpec
from max_core.gateway.adapters import _messages, _params
from max_core.gateway.contracts import (
    ChatMessage,
    Dialect,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    InferenceDestination,
    Privacy,
)
from max_core.gateway_trial import TrialConfigurationError, TrialSettings
from max_core.harness import MaxHarness
from max_core.screen_vision import (
    bgra_to_rgb,
    encode_png,
    list_files,
    save_document,
    screenshot_tool,
)
from max_core.tools import Tool, ToolError, ToolImage, ToolRegistry, ToolRisk

PNG = encode_png(2, 1, bytes([255, 0, 0, 0, 0, 255]))


def _decode(png: bytes) -> tuple[int, int, bytes]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        if kind == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
        if kind == b"IDAT":
            idat += data
        pos += 12 + length
    return width, height, zlib.decompress(idat)


def test_png_encoder_round_trips() -> None:
    w, h, raw = _decode(PNG)
    assert (w, h) == (2, 1)
    assert raw == b"\x00" + bytes([255, 0, 0, 0, 0, 255])
    with pytest.raises(ValueError):
        encode_png(2, 2, b"\x00" * 3)


def test_bgra_to_rgb() -> None:
    assert bgra_to_rgb(bytes([1, 2, 3, 255, 4, 5, 6, 255]), 2) == bytes([3, 2, 1, 6, 5, 4])


def _shot(tmp_path: Path, *, vision: bool, size: tuple[int, int] = (1920, 1080)) -> str:
    take = screenshot_tool(
        tmp_path,
        vision=vision,
        capture=lambda: (PNG, 1920, 1080, *size),
        clock=lambda: dt.datetime(2026, 11, 26, 9, 30, 5),
    )
    return take({})


def test_screenshot_saved_and_attached_for_vision(tmp_path: Path) -> None:
    out = _shot(tmp_path, vision=True)
    saved = tmp_path / "Screenshots" / "2026-11" / "screenshot_2026-11-26_09-30-05.png"
    assert saved.read_bytes() == PNG
    assert isinstance(out, ToolImage) and out.images == (PNG,)
    assert "use them directly" in out and "untrusted" in out
    second = _shot(tmp_path, vision=True)
    assert "screenshot_2026-11-26_09-30-05_2.png" in second


def test_screenshot_reports_scale_and_skips_image_without_vision(tmp_path: Path) -> None:
    assert "multiply image x by 2.0000" in _shot(tmp_path, vision=True, size=(3840, 2160))
    plain = _shot(tmp_path, vision=False)
    assert not isinstance(plain, ToolImage) and "cannot view images" in plain


def test_save_document_rules(tmp_path: Path) -> None:
    save = save_document(tmp_path)
    assert save({"name": "notes", "text": "hi"}).endswith("notes.md")
    assert (tmp_path / "Documents" / "notes.md").read_text(encoding="utf-8") == "hi"
    save({"name": "list.txt", "text": "a"})
    assert (tmp_path / "Documents" / "list.txt").exists()
    assert save({"name": "run.exe", "text": "x"}).endswith("run.exe.md")
    with pytest.raises(ToolError, match="file_exists"):
        save({"name": "notes", "text": "again"})
    for bad in ("../x", "a/b", "", ".hidden"):
        with pytest.raises(ToolError):
            save({"name": bad, "text": "x"})


def test_list_files(tmp_path: Path) -> None:
    listing = list_files(tmp_path)
    assert "(0 files" in listing({})
    assert "empty" in list_files(tmp_path / "missing")({})
    save_document(tmp_path)({"name": "a", "text": "x"})
    assert "Documents" in listing({}) and "a.md" in listing({"folder": "Documents"})
    with pytest.raises(ToolError):
        listing({"folder": ".."})


def test_files_tools_only_for_local_host_sessions(tmp_path: Path) -> None:
    local = {
        t.name for t in assistant_registry(data_dir=tmp_path, enable_web=False, files_root=tmp_path)
    }
    assert {"files.list", "files.save_document"} <= local
    cloud = {
        t.name
        for t in assistant_registry(
            data_dir=tmp_path, enable_web=False, files_root=tmp_path, cloud=True
        )
    }
    hosted = {
        t.name
        for t in assistant_registry(
            data_dir=tmp_path, enable_web=False, files_root=tmp_path, host_tools=False
        )
    }
    assert not {"files.list", "pc.screenshot"} & (cloud | hosted)


# --- gateway -----------------------------------------------------------------


def test_chat_message_image_rules() -> None:
    assert ChatMessage("user", "see", images=(PNG,)).images == (PNG,)
    for bad in ((b"GIF89a",), (PNG, PNG, PNG)):
        with pytest.raises(ValueError):
            ChatMessage("user", "x", images=bad)
    with pytest.raises(ValueError):
        ChatMessage("assistant", "x", images=(PNG,))


def _request(*, local: bool = True, vision: bool = True) -> GatewayRequest:
    return GatewayRequest(
        request_id="r1",
        messages=(ChatMessage("user", "what is this", images=(PNG,)),),
        privacy=Privacy.CONFIDENTIAL,
        max_input_tokens=4000,
        max_output_tokens=100,
        local_only=local,
        cloud_allowed=not local,
        required_capabilities=frozenset({"text", "vision"} if vision else {"text"}),
    )


def test_images_need_local_only_vision_requests() -> None:
    _request()
    with pytest.raises(ValueError):
        _request(local=False)
    with pytest.raises(ValueError):
        _request(vision=False)


def _spec(dialect: Dialect, caps: set[str]) -> ModelSpec:
    return ModelSpec(
        provider_id="local",
        model_id="vl",
        dialect=dialect,
        endpoint="http://127.0.0.1:11434/v1/chat/completions",
        is_local=True,
        context_tokens=8192,
        inference_destination=InferenceDestination.VERIFIED_LOCAL,
        capabilities=frozenset(caps),
    )


def test_adapters_serialize_images() -> None:
    openai = _messages(_request(), Dialect.OPENAI_CHAT)["messages"][0]["content"]
    assert openai[0] == {"type": "text", "text": "what is this"}
    assert openai[1]["image_url"]["url"].startswith("data:image/png;base64,")
    anthropic = _messages(_request(), Dialect.ANTHROPIC_MESSAGES)["messages"][0]["content"]
    assert anthropic[0]["source"]["media_type"] == "image/png"
    assert anthropic[-1] == {"type": "text", "text": "what is this"}
    _params(_spec(Dialect.OPENAI_CHAT, {"text", "vision"}), _request(), stream=False)
    with pytest.raises(GatewayError):
        _params(_spec(Dialect.OPENAI_CHAT, {"text"}), _request(), stream=False)


def _env(**extra: str) -> dict[str, str]:
    env = {
        "MAX_TRIAL_DESTINATION": "verified_local",
        "MAX_TRIAL_LOCAL_EGRESS_REVIEWED": "yes",
        "MAX_TRIAL_MODEL": "qwen2.5vl",
        "MAX_TRIAL_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions",
        "MAX_TRIAL_ENDPOINT_LOCAL": "yes",
        "MAX_TRIAL_CONTEXT_TOKENS": "8192",
    }
    env.update(extra)
    return env


def test_vision_toggle() -> None:
    assert "vision" in TrialSettings.from_env(_env(MAX_TRIAL_VISION="yes")).spec.capabilities
    assert "vision" not in TrialSettings.from_env(_env()).spec.capabilities
    with pytest.raises(TrialConfigurationError):
        TrialSettings.from_env(_env(MAX_TRIAL_VISION="maybe"))
    cloud = _env(
        MAX_TRIAL_VISION="yes",
        MAX_TRIAL_DESTINATION="cloud",
        MAX_TRIAL_CLOUD_ALLOWED="yes",
        MAX_TRIAL_MAX_COST_USD="1",
        MAX_TRIAL_INPUT_USD_PER_MILLION="0",
        MAX_TRIAL_OUTPUT_USD_PER_MILLION="0",
        MAX_TRIAL_API_KEY="k",
        MAX_TRIAL_ENDPOINT_LOCAL="no",
    )
    with pytest.raises(TrialConfigurationError, match="vision_requires_local_model"):
        TrialSettings.from_env(cloud)


# --- harness -----------------------------------------------------------------


class _Gateway:
    def __init__(self, replies: Sequence[str]) -> None:
        self.replies, self.requests = list(replies), []

    async def complete(self, request: GatewayRequest) -> GatewayResponse:
        self.requests.append(request)
        return GatewayResponse(
            request_id=request.request_id,
            provider_id="local",
            model_id="vl",
            content=self.replies.pop(0) if self.replies else "done",
            tool_proposals=(),
            finish_reason="stop",
            usage=None,
            estimated_cost_usd=None,
        )


def _make(messages: tuple[ChatMessage, ...]) -> GatewayRequest:
    return GatewayRequest(
        request_id="r",
        messages=messages,
        privacy=Privacy.CONFIDENTIAL,
        max_input_tokens=16_000,
        max_output_tokens=256,
        local_only=True,
        required_capabilities=frozenset(
            {"text", "vision"} if any(m.images for m in messages) else {"text"}
        ),
    )


def test_harness_sends_only_the_newest_screenshot() -> None:
    other = encode_png(1, 1, b"\x00\x00\x00")
    shots = iter([PNG, other])
    reg = ToolRegistry()
    reg.register(
        Tool(
            "pc.screenshot",
            "see",
            ToolRisk.READ_ONLY,
            (),
            lambda _a: ToolImage("shot", (next(shots),)),
        )
    )
    call = '<tool_call>{"name": "pc.screenshot", "arguments": {}}</tool_call>'
    gw = _Gateway([call, call, "I can see it."])
    report = asyncio.run(
        MaxHarness(gw, reg, enable_planning=False, enable_self_prompt=False).run(
            [ChatMessage("user", "look")], _make
        )
    )
    assert report.answer == "I can see it."
    second, third = gw.requests[1], gw.requests[2]
    assert [m.images for m in second.messages if m.images] == [(PNG,)]
    assert [m.images for m in third.messages if m.images] == [(other,)]
    assert any("older screenshot removed" in m.content for m in third.messages)
