"""The opt-in phone companion is loopback-only and token authenticated."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from max.config import Config
from max.pc.control import PCController
from max.pc.web_app import PCBridge
from max.pc.remote import PhoneCompanion


def request(url, *, token=None, text="hi max", extra=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Max-Token"] = token
    if extra:
        headers.update(extra)
    req = urllib.request.Request(
        url, headers=headers, data=json.dumps({"text": text}).encode(), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_phone_server_requires_token_and_cannot_confirm_pc_keyboard(tmp_path):
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path)
    bridge = PCBridge(PCController(tmp_path), config=cfg, speaker=lambda _: False)
    bridge.set_voice(False)
    ticks = [0.0]
    def next_time():
        ticks[0] += 1.0
        return ticks[0]
    remote = PhoneCompanion(bridge, port=0, now=next_time)
    try:
        info = remote.start()
        assert info["ok"] and remote.active
        assert "127.0.0.1" in info["url"]
        root, token = info["url"].split("#", 1)
        path = root.split("#")[0].rstrip("/") + "/api/command"
        with urllib.request.urlopen(root, timeout=2) as res:
            html = res.read().decode()
            assert "PHONE COMPANION" in html
            assert token not in html
            assert "X-Max-Token" in html
        assert request(path)[0] == 401
        assert request(path, token="fake")[0] == 401
        assert request(path, token=token, text="hi max")[1]["action"] == "wake"
        status, reply = request(path, token=token, text="type into notepad: test note")
        assert status == 200 and reply["action"] == "approval"
        assert "approval" not in reply  # cannot remotely obtain consent token
        assert "Approval pending on your PC" in reply["text"]
        events = bridge.poll_events()
        assert any(e["type"] == "remote" and e["response"].get("approval") for e in events)
        # No endpoint exists for remote approval or remote settings changes.
        assert request(root.rstrip("/") + "/api/approve", token=token)[0] == 404
    finally:
        remote.stop()
    assert not remote.active


def test_phone_origin_and_invalid_host_are_denied(tmp_path):
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path)
    bridge = PCBridge(PCController(tmp_path), config=cfg, speaker=lambda _: False)
    remote = PhoneCompanion(bridge, port=0)
    try:
        root, token = remote.start()["url"].split("#", 1)
        url = root.rstrip("/") + "/api/command"
        assert request(url, token=token, extra={"Origin": "https://evil.invalid"})[0] == 403
        assert request(url, token=token, extra={"Host": "evil.invalid"})[0] == 404
        assert request(url, token=token, text="") [0] == 400
    finally:
        remote.stop()


def test_bridge_companion_lifecycle_revokes_token_and_stops_server(tmp_path):
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path)
    bridge = PCBridge(PCController(tmp_path), config=cfg, speaker=lambda _: False)
    # Default port is fixed to 8765; tests avoid stealing a real listener.
    from unittest.mock import patch
    class TestingCompanion(PhoneCompanion):
        def __init__(self, bridge):
            super().__init__(bridge, port=0)
    with patch("max.pc.remote.PhoneCompanion", TestingCompanion):
        assert bridge.state()["remote"] is False
        started = bridge.remote_start()
        assert started["ok"] and started["state"]["remote"]
        again = bridge.remote_start()
        assert not again["ok"]
        stopped = bridge.remote_stop()
        assert stopped["ok"] and not stopped["state"]["remote"]
        assert bridge._remote is None
