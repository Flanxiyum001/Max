"""Headless Windows-PC contracts: never execute arbitrary user input in shell."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from max.pc.control import (
    AppLauncher, PCController, ProfileStore, greeting,
    parse_valorant_settings, safe_https_url,
)


def test_greetings_are_local_time_aware():
    assert "morning" in greeting(datetime(2026, 10, 29, 8, 15))
    assert "afternoon" in greeting(datetime(2026, 10, 29, 15, 0))
    assert "evening" in greeting(datetime(2026, 10, 29, 20, 0))


def test_pc_sleep_wake_chat_and_sleep_again(tmp_path):
    requests = []
    pc = PCController(tmp_path, ask=lambda t, s: requests.append((t, s)) or "done")
    assert pc.handle("hello random").action == "sleeping"
    assert requests == []
    assert pc.handle("Hi, MAX!").action == "wake"
    assert pc.handle("do some work").text == "done"
    assert requests == [("do some work", "pc_local")]
    assert pc.handle("bye max").action == "sleep"
    assert pc.handle("what now").action == "sleeping"
    assert len(requests) == 1


def test_save_two_profiles_and_recover_after_relaunch(tmp_path):
    pc = PCController(tmp_path)
    assert pc.handle("hi max").ok
    first = pc.handle("remember my Valorant settings: sens 0.4 crouch C walk shift")
    assert first.ok and first.action == "profile"
    second = pc.handle("remember my Valorant settings for alt: sens 0.35 crouch V")
    assert second.ok
    saved = ProfileStore(tmp_path)
    assert saved.get() == {"sensitivity": 0.4, "crouch": "C", "walk": "SHIFT"}
    assert saved.get("alt") == {"sensitivity": 0.35, "crouch": "V"}
    apply = pc.handle("apply my Valorant settings for alt")
    assert "0.35" in apply.text
    assert "not performed" in apply.text  # never falsely claim game settings applied
    assert pc.handle("apply my Valorant settings for missing").ok is False
    path = tmp_path / "pc" / "valorant_profiles.json"
    assert "password" not in path.read_text(encoding="utf-8")


def test_profile_input_is_bounded_and_malformed_data_is_recoverable(tmp_path):
    store = ProfileStore(tmp_path)
    try:
        store.remember("../../evil", {"sensitivity": .4})
        assert False, "path traversal must be denied"
    except ValueError:
        pass
    assert parse_valorant_settings("sens 500 crouch COMPOSE") == {}
    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert store.load() == {}
    store.remember("default", {"jump": "SPACE"})
    assert json.loads(path.read_text(encoding="utf-8"))["default"]["jump"] == "SPACE"


def test_url_allowlist_prevents_local_and_credential_urls():
    good = [
        "https://www.example.com/",
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
    ]
    bad = [
        "javascript:alert(1)", "file:///etc/passwd", "http://example.com",
        "https://localhost/", "https://127.0.0.1/", "https://10.0.0.4/",
        "https://user:password@example.com/", "https://example.com:444/",
        "https://x.local/", "https://example.com/\ninvalid",
    ]
    assert all(safe_https_url(x) for x in good)
    assert all(safe_https_url(x) is None for x in bad)


def test_whitelisted_app_launch_does_not_execute_arbitrary_shell(tmp_path):
    commands = []
    urls = []
    launcher = AppLauncher(
        platform="win32", spawn=lambda args, **kw: commands.append((args, kw)),
        opener=lambda link: urls.append(link) or True,
    )
    pc = PCController(tmp_path, launcher=launcher)
    pc.handle("hi max")
    assert pc.handle("open notepad").ok
    assert commands == [(["notepad.exe"], {"shell": False})]
    assert pc.handle("open calculator; remove everything").action == "chat"
    assert len(commands) == 1
    assert pc.handle("open https://example.com").ok
    assert urls == ["https://example.com"]
    assert pc.handle("open http://localhost").ok is False or len(urls) == 1


def test_spotify_opens_playlist_but_does_not_fake_auto_play(tmp_path):
    opened = []
    pc = PCController(tmp_path, launcher=AppLauncher(
        platform="win32", opener=lambda u: opened.append(u) or True,
    ))
    pc.handle("hi max")
    pid = "37i9dQZF1DXcBWIGoYBM5M"
    result = pc.handle("play Spotify playlist spotify:playlist:" + pid)
    assert result.ok and "Automatic playback requires" in result.text
    assert opened == ["https://open.spotify.com/playlist/" + pid]
    assert not pc.handle("play Spotify playlist invalid").ok


def test_no_model_does_not_give_fake_success(tmp_path):
    pc = PCController(tmp_path)
    pc.handle("hi max")
    r = pc.handle("write me an app")
    assert r.action == "chat" and not r.ok
    assert "No AI model" in r.text


def test_browser_search_url_encodes_query_and_is_explicit(tmp_path):
    pages = []
    launcher = AppLauncher(platform="win32", opener=lambda url: pages.append(url) or True)
    pc = PCController(tmp_path, launcher=launcher)
    pc.handle("hi max")
    result = pc.handle("search the web for best Valorant sensitivity")
    assert result.action == "browser" and result.ok
    assert pages == ["https://www.google.com/search?q=best+Valorant+sensitivity"]
    assert not launcher.search_web(" " * 10).ok


def test_volume_only_sends_whitelisted_media_key():
    pressed = []
    launcher = AppLauncher(platform="win32")
    assert launcher.volume("up", press=pressed.append).ok
    assert launcher.volume("mute", press=pressed.append).ok
    assert pressed == ["volumeup", "volumemute"]
    assert not launcher.volume("reformat", press=pressed.append).ok
    assert pressed == ["volumeup", "volumemute"]
