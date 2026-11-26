from max_core.tool_recovery import ToolFailureGuard, safe_error_code


def test_guard_does_not_expose_arguments_and_blocks_exact_replay() -> None:
    guard = ToolFailureGuard()
    args = {"path": "/private/work/notes.txt", "token": "sensitive"}
    hint = guard.failed("notes.read", args, "network_error", can_search=True)
    assert "/private/work/notes.txt" not in hint and "sensitive" not in hint
    assert "generic error terms" in hint
    assert not guard.blocked("notes.read", args)
    guard.failed("notes.read", args, "network_error")
    assert guard.blocked("notes.read", args)
    assert not guard.blocked("notes.read", {"path": "different"})
    guard.success("notes.read", args)
    assert not guard.blocked("notes.read", args)


def test_denied_and_unsafe_codes() -> None:
    guard = ToolFailureGuard()
    hint = guard.failed("notes.write", {}, "owner_denied", denied=True)
    assert "owner refused" in hint
    assert guard.blocked("notes.write", {})
    assert safe_error_code("password=secret injected\n") == "tool_failed"
