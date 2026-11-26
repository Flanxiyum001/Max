import shlex

import httpx
import pytest

from max_core.assistant_tools import assistant_registry
from max_core.tools import ToolError
from max_core.tv_control import AndroidTV, RokuTV, make_tv, tv_auto_approved


def test_android_netflix_play_sends_deep_link():
    calls: list[list[str]] = []
    tv = AndroidTV("192.168.1.50", run=lambda c: calls.append(c) or "connected", adb="adb")
    out = tv.play("Netflix", "Stranger Things")
    assert "Stranger Things" in out
    assert calls[0] == ["adb", "connect", "192.168.1.50:5555"]
    assert "https://www.netflix.com/search?q=Stranger%20Things" in shlex.split(calls[1][-1])
    assert "com.netflix.ninja" in shlex.split(calls[1][-1])


def test_roku_play_uses_ecp_search():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200)

    tv = RokuTV("10.0.0.7", http=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    tv.play("netflix", "The Crown")
    tv.press("volumeup", 2)
    assert "provider-id=12" in seen[0] and "keyword=The%20Crown" in seen[0]
    assert seen[1:] == ["http://10.0.0.7:8060/keypress/VolumeUp"] * 2


@pytest.mark.parametrize(
    "host", ["8.8.8.8", "127.0.0.1", "tv.example.com", "169.254.169.254", "0.0.0.0", "::1"]
)
def test_rejects_non_lan_hosts(host):
    with pytest.raises(ToolError):
        AndroidTV(host, run=lambda c: "")


def test_rejects_unknown_app_and_bad_title():
    tv = AndroidTV("192.168.1.50", run=lambda c: "connected")
    with pytest.raises(ToolError):
        tv.launch("evilapp")
    with pytest.raises(ToolError):
        tv.play("netflix", "x; reboot`")


def test_registry_and_auto_approval(tmp_path):
    env = {"MAX_TV_KIND": "roku", "MAX_TV_HOST": "192.168.0.9"}
    names = {t.name for t in assistant_registry(data_dir=tmp_path, enable_web=False, tv_env=env)}
    assert {"tv.play", "tv.open_app", "tv.press"} <= names
    assert make_tv({}) is None
    assert not tv_auto_approved("tv.play", env)
    assert tv_auto_approved("tv.play", {**env, "MAX_TV_CONTROL": "auto"})
    assert not tv_auto_approved("shell.run", {"MAX_TV_CONTROL": "auto"})
    assert not tv_auto_approved("tv.unreviewed", {**env, "MAX_TV_CONTROL": "auto"})
    for options in ({"host_tools": False}, {"cloud": True}):
        names = {
            t.name
            for t in assistant_registry(data_dir=tmp_path, enable_web=False, tv_env=env, **options)
        }
        assert not any(name.startswith("tv.") for name in names)


def test_remote_shell_preserves_title_as_single_argument():
    calls: list[list[str]] = []
    tv = AndroidTV("192.168.1.50", run=lambda c: calls.append(c) or "connected", adb="adb")
    title = "Rock & Roll (Live)"
    tv.play("prime video", title)
    assert shlex.split(calls[-1][-1])[-1] == title
