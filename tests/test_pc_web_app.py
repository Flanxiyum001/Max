"""PC WebView native bridge tests without a GUI, audio hardware or network."""
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

from max.config import Config
from max.pc.control import PCController
from max.pc.web_app import PCBridge, detect_ollama


def make_bridge(tmp_path, **kwargs):
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path, provider="mock")
    return PCBridge(PCController(tmp_path), config=cfg, **kwargs)


def test_mock_provider_does_not_pretend_to_be_real_ai(tmp_path):
    bridge = make_bridge(tmp_path)
    assert not bridge.state()["ready"]
    assert bridge.state()["model"] == "NO MODEL CONNECTED"
    assert bridge.submit("hi max")["action"] == "wake"
    answer = bridge.submit("write me some code")
    assert answer["action"] == "setup" and not answer["ok"]
    assert "CONNECT MODEL" in answer["text"]
    assert bridge.submit("open notepad")["action"] == "app"  # platform-dependent result
    assert bridge.submit("bye max")["action"] == "sleep"
    assert not bridge.state()["awake"]


def test_existing_provider_initializes_real_agent_without_mock_fallback(tmp_path):
    agent = SimpleNamespace(ask=lambda text, session_id: SimpleNamespace(
        text="Real model reply", ok=True
    ))
    cfg = Config(
        repo_root=tmp_path, data_dir=tmp_path, provider="openai",
        base_url="https://api.example.org/v1", model="model-1",
    )
    bridge = PCBridge(PCController(tmp_path), config=cfg, builder=lambda config: agent)
    assert bridge.state()["ready"] is True
    bridge.submit("hi max")
    response = bridge.submit("hello")
    assert response["text"] == "Real model reply"
    assert response["action"] == "chat"


def test_one_click_ollama_uses_only_confirmed_local_models(tmp_path):
    received = []
    def build(cfg):
        received.append((cfg.provider, cfg.base_url, cfg.model))
        return SimpleNamespace(ask=lambda txt, session_id: SimpleNamespace(text="ok", ok=True))
    bridge = make_bridge(tmp_path, scanner=lambda: ["llama3.2:latest"], builder=build)
    assert not bridge.connect_ollama("not-installed")["ok"]
    r = bridge.connect_ollama()
    assert r["ok"] is True and r["state"]["ready"]
    assert received == [("openai", "http://127.0.0.1:11434/v1", "llama3.2:latest")]
    bridge.submit("hi max")
    assert bridge.submit("what can you do")["action"] == "help"
    assert bridge.submit("tell me a joke")["text"] == "ok"


def test_missing_ollama_reports_actionable_error(tmp_path):
    bridge = make_bridge(tmp_path, scanner=lambda: (_ for _ in ()).throw(URLError("offline")))
    result = bridge.connect_ollama()
    assert not result["ok"]
    assert "Start Ollama" in result["message"]
    assert not bridge.state()["ready"]


def test_detect_ollama_parses_only_valid_model_names():
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return json.dumps({"models": [
            {"name": "llama3.2:latest"}, {"name": 27}, {"unused": "none"},
        ]}).encode()
    called = []
    def opener(req, timeout):
        called.append((req.full_url, timeout))
        return Response()
    assert detect_ollama(opener=opener) == ["llama3.2:latest"]
    assert called == [("http://127.0.0.1:11434/api/tags", 2.0)]


def test_microphone_only_runs_when_explicitly_requested(tmp_path):
    calls = []
    bridge = make_bridge(
        tmp_path, recorder=lambda seconds: calls.append(seconds) or b"WAV",
        stt=lambda wav: "hi max",
        speaker=lambda message: None,
    )
    assert calls == []
    result = bridge.record()
    assert result == {"ok": True, "text": "hi max"}
    assert calls == [4.5]
    assert bridge.set_voice(False)["voice"] is False


def test_app_html_is_packaged_and_has_responsive_controls():
    html = (Path(__file__).parents[1] / "src/max/pc/ui/index.html").read_text(encoding="utf-8")
    assert '<canvas id="field"' in html
    assert 'id="prompt"' in html
    assert 'id="send"' in html
    assert 'id="mic"' in html
    assert 'id="connect"' in html
    assert 'id="ollama"' in html
    assert 'minmax(0,1fr)' in html
    assert '@media(max-width:860px)' in html
    assert "window.pywebview.api" in html
    assert "textContent=" in html  # no untrusted model HTML injection
    assert "https://cdn." not in html


def test_custom_provider_accepts_explicit_https_without_persisting_key(tmp_path):
    received = []
    def build(cfg, provider=None):
        received.append((cfg.base_url, cfg.model, provider.api_key))
        return SimpleNamespace(ask=lambda t, session_id: SimpleNamespace(text="AI says hello", ok=True))
    bridge = make_bridge(tmp_path, builder=build)
    result = bridge.connect_provider("https://api.example.com/v1", "test-model", "private-key")
    assert result["ok"] is True
    assert result["state"]["model"] == "test-model"
    assert "private-key" not in str(result)
    assert received == [("https://api.example.com/v1", "test-model", "private-key")]
    assert not (tmp_path / ".env.local").exists()
    bridge.submit("hi max")
    assert bridge.submit("hello")["text"] == "AI says hello"


def test_provider_rejects_insecure_nonlocal_endpoint(tmp_path):
    bridge = make_bridge(tmp_path)
    for url in (
        "http://api.example.org/v1", "https://user:pass@api.example.org/v1",
        "http://127.0.0.2:11434/v1", "file:///etc/passwd",
        "https://api.example.org:443/v1?q=secret", "https://127.0.0.1/v1",
        "https://api.example.org/other", "https://10.0.0.4/v1",
    ):
        result = bridge.connect_provider(url, "model", "secret")
        assert result["ok"] is False, url
    assert not bridge.state()["ready"]


def test_model_connection_required_fields(tmp_path):
    bridge = make_bridge(tmp_path)
    assert not bridge.connect_provider("https://example.org/v1", "")["ok"]
    assert not bridge.connect_provider("https://example.org/v1", "x" * 129)["ok"]
    assert not bridge.connect_provider("https://example.org/v1", "ok", "secret" * 1000)["ok"]

