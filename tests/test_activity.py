from __future__ import annotations

from max_core.activity import activity_line, describe


def test_known_tools_have_plain_labels() -> None:
    assert describe("web.search") == "Searching the web"
    assert describe("pc.type_text") == "Typing"
    assert describe("apps.open") == "Opening an app or website"


def test_mcp_tools_name_the_service_and_action() -> None:
    assert describe("mcp.github.list_pull_requests") == "GitHub: list pull requests"
    assert describe("mcp.notion.search") == "Notion: search"
    assert describe("mcp.acme.do_thing") == "Acme: do thing"


def test_unknown_tools_fall_back_safely() -> None:
    assert describe("foo.bar_baz") == "Using foo bar baz"
    assert len(describe("x." + "a" * 500)) <= 70


def test_activity_line_by_status() -> None:
    assert activity_line("web.search", "proposed") == "Searching the web"
    assert activity_line("web.search", "executed") is None
    assert activity_line("shell.run", "denied") == "Running a command — not allowed"
    assert activity_line("web.read", "failed") == "Reading a web page — failed"
