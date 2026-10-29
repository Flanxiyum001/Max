"""Reference-reel parity: working beside real apps, not a fake Jarvis dashboard."""
from pathlib import Path
from types import SimpleNamespace

from max.pc.control import AppLauncher, PCController
from max.pc.automation import WindowsAction, parse_action
from max.pc.web_app import PCBridge, run
from max.config import Config


def test_video_discovery_opens_youtube_results_and_does_not_claim_autoplay(tmp_path):
    opened = []
    launcher = AppLauncher(platform="win32", opener=lambda url: opened.append(url) or True)
    pc = PCController(tmp_path, launcher=launcher)
    pc.handle("hi max")
    r = pc.handle("max, play funny cat videos on youtube")
    assert r.ok and r.action == "browser"
    assert "automatic playback was not verified" in r.text
    assert opened == ["https://www.youtube.com/results?search_query=funny+cat+videos"]


def test_restaurant_search_is_research_not_auto_order(tmp_path):
    opened = []
    launcher = AppLauncher(platform="win32", opener=lambda url: opened.append(url) or True)
    pc = PCController(tmp_path, launcher=launcher)
    pc.handle("hi max")
    assert pc.handle("find biryani restaurants near me").ok
    assert opened == ["https://www.google.com/search?q=biryani+restaurants+near+me"]
    denied = pc.handle("order biryani to my house")
    assert not denied.ok and denied.action == "ordering"
    assert len(opened) == 1


def test_premiere_launch_does_not_lie_about_installation(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    pc = PCController(tmp_path, launcher=AppLauncher(platform="win32"))
    pc.handle("hi max")
    response = pc.handle("open premiere")
    # CI normally lacks Premiere, so honest failure instead of fake success.
    assert response.action == "app"
    assert "Premiere" in response.text


def test_premiere_export_only_opens_reviewable_export_dialog():
    plan = parse_action("export video in premiere")
    assert plan == WindowsAction("shortcut", "premiere", "ctrl+m")
    assert "send CTRL+M" in plan.preview
    assert parse_action("press space in premiere") == WindowsAction(
        "shortcut", "premiere", "space"
    )
    assert parse_action("switch to premiere") == WindowsAction("focus", "premiere")
    assert parse_action("delete footage in premiere") is None


def test_voice_companion_is_default_and_full_dashboard_is_retained(tmp_path, monkeypatch):
    import sys
    from max.pc import web_app

    created = []
    started = []
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(
        create_window=lambda *args, **kwargs: created.append((args, kwargs)),
        start=lambda **kwargs: started.append(kwargs),
    ))
    c = Config(repo_root=tmp_path, data_dir=tmp_path)
    web_app.run(PCController(tmp_path), config=c)
    args, kwargs = created[-1]
    assert "Voice Companion" in args[0]
    assert kwargs["width"] <= 550
    assert "id=\"handsfree\"" in kwargs["html"]
    assert "id=\"approval\"" in kwargs["html"]
    assert started
    web_app.run(PCController(tmp_path), config=c, dashboard=True)
    _, dash = created[-1]
    assert dash["width"] >= 1000
    assert 'id="field"' in dash["html"]


def test_companion_js_has_no_untrusted_html_or_external_code():
    h = (Path(__file__).parents[1] / "src/max/pc/ui/companion.html").read_text(encoding="utf-8")
    assert "textContent" in h
    assert "innerHTML" not in h
    assert "<script src=" not in h
    assert "api('approve_action'" in h
    assert "api('set_handsfree'" in h
    assert "api('install_voice_model'" in h
    assert "api('connect_ollama'" in h
