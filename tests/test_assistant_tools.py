from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import httpx
import pytest

from max_core.assistant_session import (
    AssistantSession,
    SessionOptions,
    default_data_dir,
    merged_environment,
)
from max_core.assistant_tools import (
    AssistantStore,
    WebTools,
    assistant_registry,
    html_to_text,
    open_target,
    parse_due,
    run_shell,
    system_status,
)
from max_core.gateway_trial import TrialSettings
from max_core.tools import ToolError

NOW = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.UTC)

LOCAL_ENV = {
    "MAX_TRIAL_DESTINATION": "verified_local",
    "MAX_TRIAL_LOCAL_EGRESS_REVIEWED": "yes",
    "MAX_TRIAL_ENDPOINT_LOCAL": "yes",
    "MAX_TRIAL_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions",
    "MAX_TRIAL_MODEL": "qwen2.5:7b",
    "MAX_TRIAL_CONTEXT_TOKENS": "16384",
}
CLOUD_ENV = {
    "MAX_TRIAL_DESTINATION": "cloud",
    "MAX_TRIAL_CLOUD_ALLOWED": "yes",
    "MAX_TRIAL_ENDPOINT": "https://api.example.test/v1/chat/completions",
    "MAX_TRIAL_MODEL": "free-model",
    "MAX_TRIAL_CONTEXT_TOKENS": "16384",
    "MAX_TRIAL_API_KEY": "test-key-not-real",
    "MAX_TRIAL_MAX_COST_USD": "0",
    "MAX_TRIAL_INPUT_USD_PER_MILLION": "0",
    "MAX_TRIAL_OUTPUT_USD_PER_MILLION": "0",
}


# --- store -----------------------------------------------------------------


def test_memory_remember_recall_forget(tmp_path: Path) -> None:
    store = AssistantStore(tmp_path)
    assert "remembered" in store.remember("My sister is called Asha")
    assert store.remember("my sister is called asha") == "already remembered"
    store.remember("I prefer tea over coffee")
    hits = store.recall("sister")
    assert [h["fact"] for h in hits] == ["My sister is called Asha"]
    assert len(store.recall("")) == 2
    store.forget(hits[0]["id"])
    assert store.recall("sister") == []
    with pytest.raises(ToolError):
        store.forget("nope")


def test_reminders_due_once(tmp_path: Path) -> None:
    store = AssistantStore(tmp_path)
    store.add_reminder("call mum", NOW - dt.timedelta(minutes=1))
    store.add_reminder("later", NOW + dt.timedelta(hours=1))
    todo = store.add_reminder("buy milk", None)
    assert [r["text"] for r in store.pop_due(NOW)] == ["call mum"]
    assert store.pop_due(NOW) == []  # announced only once
    store.complete(todo["id"])
    assert "buy milk" not in [r["text"] for r in store.reminders()]
    assert "buy milk" in [r["text"] for r in store.reminders(include_done=True)]


def test_corrupt_store_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "memory.json").write_text("{not json")
    with pytest.raises(ToolError):
        AssistantStore(tmp_path).recall("")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("in 10 minutes", NOW + dt.timedelta(minutes=10)),
        ("in 2 hours", NOW + dt.timedelta(hours=2)),
        ("tomorrow 09:30", dt.datetime(2026, 11, 22, 9, 30, tzinfo=dt.UTC)),
        ("today", dt.datetime(2026, 11, 21, 9, 0, tzinfo=dt.UTC)),
        ("2026-12-01 18:00", dt.datetime(2026, 12, 1, 18, 0, tzinfo=dt.UTC)),
    ],
)
def test_parse_due(raw: str, expected: dt.datetime) -> None:
    assert parse_due(raw, NOW) == expected


@pytest.mark.parametrize("raw", ["whenever", "tomorrow 25:00", "in -3 minutes"])
def test_parse_due_rejects(raw: str) -> None:
    with pytest.raises(ToolError):
        parse_due(raw, NOW)


# --- web ---------------------------------------------------------------------


def _web(handler) -> WebTools:  # type: ignore[no-untyped-def]
    return WebTools(
        lambda: httpx.Client(transport=httpx.MockTransport(handler)), resolve_hosts=False
    )


def test_web_search_parses_results() -> None:
    page = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa">'
        'Example <b>A</b></a><a class="result__snippet">First &amp; best</a>'
    )
    out = _web(lambda r: httpx.Response(200, text=page)).search({"query": "x"})
    assert "Example A" in out and "https://example.org/a" in out and "First & best" in out


def test_web_read_strips_scripts_and_follows_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><script>evil()</script><p>Hello&nbsp;world</p></html>",
        )

    assert _web(handler).read({"url": "https://example.org/old"}) == "Hello world"


def test_web_refuses_bad_schemes_and_private_hosts() -> None:
    web = WebTools(lambda: httpx.Client(), resolve_hosts=True)
    for url in ["file:///etc/passwd", "ftp://x", "http://127.0.0.1/admin", "http://localhost/"]:
        with pytest.raises(ToolError):
            web.read({"url": url})


def test_weather() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(
                200, json={"results": [{"name": "Pune", "country": "India",
                                        "latitude": 18.5, "longitude": 73.8}]}
            )  # fmt: skip
        return httpx.Response(
            200,
            json={
                "current": {"temperature_2m": 27, "apparent_temperature": 29,
                            "relative_humidity_2m": 60, "weather_code": 2,
                            "wind_speed_10m": 8},
                "daily": {"time": ["2026-11-21"], "temperature_2m_min": [18],
                          "temperature_2m_max": [30], "precipitation_probability_max": [10]},
            },
        )  # fmt: skip

    out = _web(handler).weather({"place": "Pune"})
    assert "Pune, India: 27°C" in out and "partly cloudy" in out and "18–30°C" in out


def test_html_to_text() -> None:
    assert html_to_text("<style>x{}</style><h1>A</h1> <i>b</i>") == "A b"


# --- PC ----------------------------------------------------------------------


def test_system_status_reports_disk() -> None:
    assert "Disk (home)" in system_status({})


def test_open_target_validates_app_names() -> None:
    launched: list[list[str]] = []
    with pytest.raises(ToolError):
        open_target({"target": "calc; rm -rf ~"}, launcher=launched.append)
    with pytest.raises(ToolError):
        open_target({"target": "javascript:alert(1)"}, launcher=launched.append)
    assert launched == []


def test_run_shell_bounded(tmp_path: Path) -> None:
    out = run_shell({"command": f'"{sys.executable}" -c "print(6*7)"'}, tmp_path)
    assert out.startswith("exit code 0") and "42" in out


# --- registry policy ------------------------------------------------------------


def names(**kw) -> set[str]:  # type: ignore[no-untyped-def]
    return {t.name for t in assistant_registry(**kw)}


def test_local_registry_has_personal_tools(tmp_path: Path) -> None:
    got = names(data_dir=tmp_path, workspace=tmp_path)
    assert {"memory.remember", "reminders.add", "system.status", "web.search",
            "weather.current", "apps.open", "workspace.read"} <= got  # fmt: skip
    assert "shell.run" not in got  # shell is opt-in
    assert "shell.run" in names(data_dir=tmp_path, enable_shell=True)


def test_cloud_registry_hides_personal_data_unless_opted_in(tmp_path: Path) -> None:
    got = names(data_dir=tmp_path, workspace=tmp_path, cloud=True, enable_shell=True)
    assert not got & {"memory.recall", "reminders.list", "system.status", "shell.run"}
    assert "workspace.read" not in got and "web.search" in got
    opted = names(data_dir=tmp_path, workspace=tmp_path, cloud=True, cloud_personal_tools=True)
    assert "memory.recall" in opted and "workspace.read" not in opted


def test_effectful_tools_need_approval(tmp_path: Path) -> None:
    registry = assistant_registry(data_dir=tmp_path, enable_shell=True)
    effectful = {t.name for t in registry if t.risk.value == "effectful"}
    assert {"memory.remember", "memory.forget", "reminders.add", "reminders.complete",
            "apps.open", "shell.run"} <= effectful  # fmt: skip


def test_hosted_notes_are_bounded_and_require_cloud_opt_in(tmp_path: Path) -> None:
    registry = assistant_registry(
        data_dir=tmp_path, host_tools=False, cloud=True, cloud_personal_tools=True
    )
    write = registry.get("notes.write")
    read = registry.get("notes.read")
    listing = registry.get("notes.list")
    assert write is not None and read is not None and listing is not None
    assert write.risk.value == "effectful"
    assert read.risk.value == listing.risk.value == "read_only"
    assert write.handler(write.validate({"name": "Plan", "content": "hello"})) == "saved note Plan"
    assert read.handler({"name": "plan"}) == "hello"
    assert listing.handler({}) == "Plan"
    with pytest.raises(ToolError, match="invalid_note_name"):
        write.handler({"name": "../secret", "content": "no"})
    with pytest.raises(ToolError, match="note_exists"):
        write.handler({"name": "plan", "content": "overwrite"})
    assert not (tmp_path.parent / "secret.json").exists()
    assert not names(data_dir=tmp_path, host_tools=False, cloud=True) & {
        "notes.write",
        "notes.list",
        "notes.read",
    }


# --- session -------------------------------------------------------------------


def test_options_and_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("MAX_TRIAL_MODEL=from-dotenv\nMAX_ENABLE_SHELL=yes\n")
    merged = merged_environment({"MAX_TRIAL_MODEL": "from-env"}, dotenv)
    assert merged["MAX_TRIAL_MODEL"] == "from-env" and merged["MAX_ENABLE_SHELL"] == "yes"
    opts = SessionOptions.from_env({"MAX_DATA_DIR": str(tmp_path), "MAX_ENABLE_SHELL": "yes"})
    assert opts.enable_shell and opts.enable_web and not opts.cloud_personal_tools
    assert default_data_dir({"MAX_DATA_DIR": str(tmp_path)}) == tmp_path


def _model(replies: list[str], seen: list[dict]) -> httpx.MockTransport:  # type: ignore[type-arg]
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={"id": "x", "model": body["model"], "choices": [{"message": {"role": "assistant",
                  "content": replies.pop(0)}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )  # fmt: skip

    return httpx.MockTransport(handler)


def test_session_remembers_with_approval_and_injects_memory(tmp_path: Path) -> None:
    seen: list[dict] = []  # type: ignore[type-arg]
    call = '<tool_call>{"name": "memory.remember", "arguments": {"fact": "I live in Pune"}}'
    replies = [call + "</tool_call>", "Noted.", "You live in Pune."]
    session = AssistantSession(
        TrialSettings.from_env(LOCAL_ENV),
        SessionOptions(tmp_path, None, False, False, False),
        transport=_model(replies, seen),
    )
    try:
        asked: list[str] = []
        result = session.ask(
            "remember I live in Pune", approve=lambda n, a: asked.append(n) or True
        )
        assert result.answer == "Noted." and asked == ["memory.remember"]
        assert session.ask("where do I live?").answer == "You live in Pune."
        system_text = " ".join(m["content"] for m in seen[-1]["messages"] if m["role"] == "system")
        assert "I live in Pune" in system_text  # remembered fact injected as data
        assert [m["content"] for m in seen[-1]["messages"] if m["role"] == "user"][-1] == (
            "where do I live?"
        )
    finally:
        session.close()


def test_cloud_session_does_not_leak_memory(tmp_path: Path) -> None:
    AssistantStore(tmp_path).remember("secret fact")
    seen: list[dict] = []  # type: ignore[type-arg]
    session = AssistantSession(
        TrialSettings.from_env(CLOUD_ENV),
        SessionOptions(tmp_path, None, False, False, False),
        transport=_model(["hi"], seen),
    )
    try:
        assert session.ask("hello").answer == "hi"
        assert "secret fact" not in json.dumps(seen)
        assert session.due_reminders() == []
    finally:
        session.close()
