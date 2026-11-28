from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from max_core.gateway.contracts import (
    ChatMessage,
    GatewayRequest,
    GatewayResponse,
    Privacy,
    ToolProposal,
)
from max_core.github_tool_catalogue import (
    GitHubSchemaError,
    validate_github_arguments,
    validate_github_operation,
)
from max_core.tool_loop import ToolLoop, parse_tool_calls
from max_core.tool_recovery import ToolFailureGuard, safe_error_code
from max_core.tools import (
    Tool,
    ToolError,
    ToolParam,
    ToolRegistry,
    ToolRisk,
    Workspace,
    builtin_registry,
)


class ScriptedGateway:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.requests: list[GatewayRequest] = []

    async def complete(self, request: GatewayRequest) -> GatewayResponse:
        self.requests.append(request)
        content = self.replies.pop(0) if self.replies else "done"
        return GatewayResponse(
            request.request_id, "test_provider", "test_model", content, (), "stop", None, None
        )


def make_request(messages: tuple[ChatMessage, ...]) -> GatewayRequest:
    size = sum(len(m.content.encode()) + 32 for m in messages)
    return GatewayRequest(
        "req-hardening", messages, Privacy.CONFIDENTIAL, size, 256, local_only=True
    )


def call_tag(tool: str, **arguments: Any) -> str:
    return f"<tool_call>{json.dumps({'name': tool, 'arguments': arguments})}</tool_call>"


def run_loop(loop: ToolLoop, text: str = "hi"):  # type: ignore[no-untyped-def]
    return asyncio.run(loop.run([ChatMessage("user", text)], make_request))


# ===========================================================================
# 1. Tool Calling Parsing Edge Cases
# ===========================================================================


class TestToolCallingParsing:
    def test_plain_text_without_calls(self) -> None:
        resp = GatewayResponse("r", "p", "m", "Just a regular text answer.", (), None, None, None)
        assert parse_tool_calls(resp) == []

    def test_single_tool_call_basic(self) -> None:
        resp = GatewayResponse("r", "p", "m", call_tag("clock.now"), (), None, None, None)
        calls = parse_tool_calls(resp)
        assert calls == [("clock.now", {})]

    def test_multi_call_blocks(self) -> None:
        text = (
            call_tag("clock.now")
            + "\n"
            + call_tag("math.calculate", expression="2+2")
            + "\n"
            + call_tag("workspace.list", path="docs")
        )
        resp = GatewayResponse("r", "p", "m", text, (), None, None, None)
        calls = parse_tool_calls(resp)
        assert len(calls) == 3
        assert calls[0] == ("clock.now", {})
        assert calls[1] == ("math.calculate", {"expression": "2+2"})
        assert calls[2] == ("workspace.list", {"path": "docs"})

    def test_interleaved_explanation_and_tool_calls(self) -> None:
        text = (
            "I will check the current time first:\n"
            + call_tag("clock.now")
            + "\nNext I will list the workspace:\n"
            + call_tag("workspace.list", path=".")
            + "\nFinally I will compute the sum:\n"
            + call_tag("math.calculate", expression="10*10")
        )
        resp = GatewayResponse("r", "p", "m", text, (), None, None, None)
        calls = parse_tool_calls(resp)
        assert [name for name, _ in calls] == [
            "clock.now",
            "workspace.list",
            "math.calculate",
        ]

    def test_escaped_strings_and_special_characters(self) -> None:
        special_str = 'Quote: "hello", newline:\n, tab:\t, backslash:\\, braces: {foo: "bar"}'
        text = call_tag("test.echo", content=special_str)
        resp = GatewayResponse("r", "p", "m", text, (), None, None, None)
        calls = parse_tool_calls(resp)
        assert len(calls) == 1
        name, args = calls[0]
        assert name == "test.echo"
        assert isinstance(args, dict)
        assert args.get("content") == special_str

    def test_nested_json_arguments(self) -> None:
        nested_args = {
            "query": {"filter": {"tags": ["python", "tests"], "active": True}},
            "limit": 10,
        }
        text = f"<tool_call>{json.dumps({'name': 'search.advanced', 'arguments': nested_args})}</tool_call>"
        resp = GatewayResponse("r", "p", "m", text, (), None, None, None)
        calls = parse_tool_calls(resp)
        assert len(calls) == 1
        name, args = calls[0]
        assert name == "search.advanced"
        assert args == nested_args

    def test_markdown_code_block_wrapping(self) -> None:
        wrapped_variants = [
            f"<tool_call>\n```json\n{json.dumps({'name': 'clock.now', 'arguments': {}})}\n```\n</tool_call>",
            f"<tool_call>```\n{json.dumps({'name': 'clock.now', 'arguments': {}})}\n```</tool_call>",
            f"```json\n<tool_call>{json.dumps({'name': 'clock.now', 'arguments': {}})}</tool_call>\n```",
        ]
        for variant in wrapped_variants:
            resp = GatewayResponse("r", "p", "m", variant, (), None, None, None)
            calls = parse_tool_calls(resp)
            # Either parsed directly or safely handled without crashing
            assert isinstance(calls, list)

    def test_malformed_syntax_fails_safely(self) -> None:
        malformed_samples = [
            "<tool_call>{not valid json at all}</tool_call>",
            "<tool_call></tool_call>",
            "<tool_call>   </tool_call>",
            '<tool_call>{"missing_name": true}</tool_call>',
            '<tool_call>{"name": 12345}</tool_call>',
        ]
        for sample in malformed_samples:
            resp = GatewayResponse("r", "p", "m", sample, (), None, None, None)
            calls = parse_tool_calls(resp)
            assert calls == [] or any(c[0] in ("<malformed>", "malformed_tool_call") for c in calls)

    def test_hybrid_provider_proposals_and_text_calls(self) -> None:
        proposal = ToolProposal("call-p1", "native.tool", {"arg": "val"})
        content = call_tag("clock.now")
        resp = GatewayResponse("r", "p", "m", content, (proposal,), None, None, None)
        calls = parse_tool_calls(resp)
        assert len(calls) == 2
        assert calls[0] == ("native.tool", {"arg": "val"})
        assert calls[1] == ("clock.now", {})

    def test_tool_call_without_arguments_key_defaults_to_dict(self) -> None:
        content = '<tool_call>{"name": "clock.now"}</tool_call>'
        resp = GatewayResponse("r", "p", "m", content, (), None, None, None)
        calls = parse_tool_calls(resp)
        assert len(calls) == 1
        assert calls[0] == ("clock.now", {})


# ===========================================================================
# 2. Coercion & Validation of Arguments Across Tools
# ===========================================================================


class TestArgumentValidationAndCoercion:
    def test_strict_primitive_types(self) -> None:
        tool = Tool(
            "test.strict",
            "Strict tool",
            ToolRisk.READ_ONLY,
            (
                ToolParam("s", str, "string param"),
                ToolParam("i", int, "int param"),
                ToolParam("f", float, "float param"),
                ToolParam("b", bool, "bool param"),
            ),
            lambda _: "ok",
        )
        valid = {"s": "hello", "i": 42, "f": 3.14, "b": True}
        clean = tool.validate(valid)
        assert clean["s"] == "hello"
        assert clean["i"] == 42
        assert clean["f"] == 3.14
        assert clean["b"] is True

    def test_int_to_float_coercion(self) -> None:
        tool = Tool(
            "test.float",
            "Float tool",
            ToolRisk.READ_ONLY,
            (ToolParam("val", float, "numeric float"),),
            lambda _: "ok",
        )
        assert tool.validate({"val": 10}) == {"val": 10.0}
        assert tool.validate({"val": -5}) == {"val": -5.0}

    @pytest.mark.parametrize("bad_val", [float("inf"), float("-inf"), float("nan"), 10**400])
    def test_non_finite_and_overflow_floats_rejected(self, bad_val: object) -> None:
        tool = Tool(
            "test.float",
            "Float tool",
            ToolRisk.READ_ONLY,
            (ToolParam("val", float, "numeric float"),),
            lambda _: "ok",
        )
        with pytest.raises(ToolError) as exc_info:
            tool.validate({"val": bad_val})
        assert exc_info.value.code == "argument_type"

    def test_string_length_bounds(self) -> None:
        tool = Tool(
            "test.bounded",
            "Bounded tool",
            ToolRisk.READ_ONLY,
            (ToolParam("text", str, "short text", max_length=10),),
            lambda _: "ok",
        )
        assert tool.validate({"text": "1234567890"}) == {"text": "1234567890"}
        with pytest.raises(ToolError) as exc_info:
            tool.validate({"text": "12345678901"})
        assert exc_info.value.code == "argument_too_long"

    def test_unknown_and_missing_arguments(self) -> None:
        tool = Tool(
            "test.req",
            "Required param tool",
            ToolRisk.READ_ONLY,
            (
                ToolParam("req", str, "required"),
                ToolParam("opt", str, "optional", required=False),
            ),
            lambda _: "ok",
        )
        # Missing required
        with pytest.raises(ToolError) as exc_info:
            tool.validate({})
        assert exc_info.value.code == "missing_argument"

        # Optional omitted is fine
        assert tool.validate({"req": "v"}) == {"req": "v"}

        # Unknown argument rejected
        with pytest.raises(ToolError) as exc_info:
            tool.validate({"req": "v", "extra": "bad"})
        assert exc_info.value.code == "unknown_argument"

    def test_arguments_must_be_mapping(self) -> None:
        tool = Tool(
            "test.req",
            "Required tool",
            ToolRisk.READ_ONLY,
            (ToolParam("req", str, "required"),),
            lambda _: "ok",
        )
        for bad in [None, "string", 123, [1, 2, 3], True]:
            with pytest.raises(ToolError) as exc_info:
                tool.validate(bad)
            assert exc_info.value.code == "arguments_not_object"

    def test_custom_argument_validator(self) -> None:
        def custom(args: object) -> dict[str, Any]:
            if not isinstance(args, dict):
                raise ToolError("custom_not_dict")
            if "key" not in args:
                raise ToolError("custom_missing_key")
            return {"key": str(args["key"]).upper()}

        tool = Tool(
            "test.custom",
            "Custom validator tool",
            ToolRisk.READ_ONLY,
            (),
            lambda _: "ok",
            argument_validator=custom,
        )
        assert tool.validate({"key": "abc"}) == {"key": "ABC"}
        with pytest.raises(ToolError) as exc_info:
            tool.validate({})
        assert exc_info.value.code == "custom_missing_key"

    def test_github_operation_validation(self) -> None:
        # ui_get requires method in _UI_METHODS and valid owner/repo
        with pytest.raises(GitHubSchemaError) as exc:
            validate_github_operation("ui_get", {"method": "invalid_method"})
        assert str(exc.value) == "github_invalid_ui_request"

        # valid ui_get
        validate_github_operation("ui_get", {"method": "labels", "owner": "max", "repo": "core"})

        # push_files requires non-empty files list
        with pytest.raises(GitHubSchemaError) as exc:
            validate_github_operation("push_files", {"files": []})
        assert str(exc.value) == "github_file_batch_required"

        # run_secret_scanning requires scan files
        with pytest.raises(GitHubSchemaError) as exc:
            validate_github_operation("run_secret_scanning", {"files": []})
        assert str(exc.value) == "github_scan_content_required"

        # add_issue_comment requires body or reaction
        with pytest.raises(GitHubSchemaError) as exc:
            validate_github_operation("add_issue_comment", {})
        assert str(exc.value) == "github_comment_or_reaction_required"

    def test_github_argument_validation_and_size_limits(self) -> None:
        import hashlib

        schema = {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "owner": {"type": "string"}},
            "required": ["repo", "owner"],
        }
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

        # Valid arguments
        clean = validate_github_arguments(encoded, digest, {"repo": "Max", "owner": "user"})
        assert clean == {"repo": "Max", "owner": "user"}

        # Digest mismatch fails closed
        with pytest.raises(GitHubSchemaError) as exc:
            validate_github_arguments(encoded, "0" * 64, {"repo": "Max", "owner": "user"})
        assert str(exc.value) == "github_unsupported_schema"

        # Oversized arguments > 32768 bytes fail closed
        oversized = {"repo": "Max", "owner": "A" * 40_000}
        with pytest.raises(GitHubSchemaError) as exc:
            validate_github_arguments(encoded, digest, oversized)
        assert str(exc.value) == "github_invalid_arguments"

    def test_workspace_confinement_and_security(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path)
        (tmp_path / "visible.txt").write_text("safe content", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
        (tmp_path / "secrets.txt").write_text("token", encoding="utf-8")

        # Regular read succeeds
        assert ws.read({"path": "visible.txt"}) == "safe content"

        # Traversal attempts rejected
        traversals = ["../outside.txt", "../../etc/passwd", "/absolute/path"]
        for path in traversals:
            with pytest.raises(ToolError):
                ws.read({"path": path})

        # Secret and dot files hidden from list
        listing = ws.list({"path": "."})
        assert "visible.txt" in listing
        assert ".env" not in listing
        assert "secrets.txt" not in listing

    def test_workspace_note_writing_invariants(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path)
        # Valid write
        assert "saved max-notes/my-note.md" in ws.write_note(
            {"name": "my-note", "content": "hello"}
        )
        assert (tmp_path / "max-notes" / "my-note.md").read_text() == "hello"

        # Overwrite rejected
        with pytest.raises(ToolError) as exc_info:
            ws.write_note({"name": "my-note", "content": "overwrite"})
        assert exc_info.value.code == "note_exists"

        # Invalid note names rejected
        for bad_name in ["../escape", "note/slash", "note\x00null", ""]:
            with pytest.raises(ToolError) as exc_info:
                ws.write_note({"name": bad_name, "content": "x"})
            assert exc_info.value.code == "invalid_note_name"


# ===========================================================================
# 3. Tool Execution Failure Modes & Hardening
# ===========================================================================


class TestToolExecutionFailureModes:
    def test_non_existent_tool_reports_unknown_tool(self) -> None:
        gw = ScriptedGateway([call_tag("nonexistent.tool", foo="bar"), "done"])
        reg = ToolRegistry()
        result = run_loop(ToolLoop(gw, reg))
        assert len(result.steps) == 1
        assert result.steps[0].tool_name == "nonexistent.tool"
        assert result.steps[0].status == "failed"
        assert result.steps[0].error_code == "unknown_tool"

    def test_read_only_tool_timeout(self) -> None:
        def hanging_tool(_args: Any) -> str:
            import time

            time.sleep(1.0)
            return "delayed"

        reg = ToolRegistry((Tool("slow.read", "Slow", ToolRisk.READ_ONLY, (), hanging_tool),))
        gw = ScriptedGateway([call_tag("slow.read"), "after timeout"])
        loop = ToolLoop(gw, reg, tool_timeout_seconds=0.05)
        result = run_loop(loop)
        assert any(s.error_code == "tool_timeout" for s in result.steps)

    def test_effectful_tool_denied_without_approver(self, tmp_path: Path) -> None:
        reg = builtin_registry(tmp_path)
        gw = ScriptedGateway([call_tag("notes.write", name="test", content="content"), "done"])
        result = run_loop(ToolLoop(gw, reg))
        assert result.steps[-1].status == "denied"
        assert result.steps[-1].error_code == "owner_denied"
        assert not (tmp_path / "max-notes" / "test.md").exists()

    def test_effectful_tool_denied_when_approver_returns_false(self, tmp_path: Path) -> None:
        reg = builtin_registry(tmp_path)
        gw = ScriptedGateway([call_tag("notes.write", name="test", content="content"), "done"])
        loop = ToolLoop(gw, reg, approve=lambda _name, _args: False)
        result = run_loop(loop)
        assert result.steps[-1].status == "denied"
        assert result.steps[-1].error_code == "owner_denied"

    def test_effectful_tool_denied_when_approver_raises_exception(self, tmp_path: Path) -> None:
        reg = builtin_registry(tmp_path)
        gw = ScriptedGateway([call_tag("notes.write", name="test", content="content"), "done"])

        def broken_approver(_name: str, _args: dict[str, Any]) -> bool:
            raise RuntimeError("approval system error")

        loop = ToolLoop(gw, reg, approve=broken_approver)
        result = run_loop(loop)
        assert result.steps[-1].status == "denied"
        assert result.steps[-1].error_code == "owner_denied"

    def test_effectful_outcome_uncertain_halts_loop(self) -> None:
        def failing_write(_args: Any) -> str:
            raise ToolError("disk_full")

        tool = Tool("write.action", "Write action", ToolRisk.EFFECTFUL, (), failing_write)
        reg = ToolRegistry((tool,))
        gw = ScriptedGateway([call_tag("write.action"), "should not be reached"])
        loop = ToolLoop(gw, reg, approve=lambda _n, _a: True)
        result = run_loop(loop)
        assert result.stopped_reason == "step_limit"
        assert result.error_code == "effectful_outcome_uncertain"
        assert len(gw.requests) == 1

    def test_untrusted_tool_name_escapes_xml_attribute(self) -> None:
        malicious_name = 'test" onclick="alert(1)" name="injected'
        gw = ScriptedGateway([call_tag(malicious_name), "done"])
        result = run_loop(ToolLoop(gw, builtin_registry()))
        assert result.answer == "done"
        content = gw.requests[1].messages[-1].content
        assert 'name="unrecognized"' in content
        assert "onclick" not in content

    def test_too_many_calls_per_step_truncated(self) -> None:
        gw = ScriptedGateway(
            [
                call_tag("clock.now")
                + call_tag("clock.now")
                + call_tag("clock.now")
                + call_tag("clock.now")
                + call_tag("clock.now"),
                "finished",
            ]
        )
        loop = ToolLoop(gw, builtin_registry(), max_calls_per_step=3)
        result = run_loop(loop)
        assert result.answer == "finished"
        last_req = gw.requests[1].messages[-1].content
        assert 'name="limit">{"error": "too_many_calls"}' in last_req

    def test_failure_guard_stops_repeated_failures(self) -> None:
        guard = ToolFailureGuard()
        name = "test.failing"
        args = {"arg": 1}
        assert not guard.blocked(name, args)

        # First failure
        hint1 = guard.failed(name, args, "server_error")
        assert "server_error" in hint1
        assert not guard.blocked(name, args)

        # Second failure -> now blocked
        hint2 = guard.failed(name, args, "server_error")
        assert "failed twice" in hint2
        assert guard.blocked(name, args)

        # Different arguments not blocked
        assert not guard.blocked(name, {"arg": 2})

        # Success resets failure
        guard.success(name, args)
        assert not guard.blocked(name, args)

    def test_safe_error_code_scrubbing(self) -> None:
        assert safe_error_code("unknown_tool") == "unknown_tool"
        assert safe_error_code("owner_denied") == "owner_denied"
        assert safe_error_code("path_outside_workspace") == "path_outside_workspace"
        # Non-whitelisted or irregular format falls back to tool_failed
        assert safe_error_code("internal database table users exploded") == "tool_failed"
        assert safe_error_code("") == "tool_failed"
