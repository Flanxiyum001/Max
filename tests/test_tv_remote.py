from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from max_core import tv_remote as tv


class FakeRemote:
    def __init__(self, is_on: bool | None, *, applies: bool = True, offline: bool = False) -> None:
        self.is_on = is_on
        self.applies = applies
        self.offline = offline
        self.calls: list[str] = []

    async def async_connect(self) -> None:
        self.calls.append("connect")
        if self.offline:
            raise OSError("private")

    async def async_generate_cert_if_missing(self) -> bool:
        self.calls.append("cert")
        return True

    async def async_start_pairing(self) -> None:
        self.calls.append("pair-start")

    async def async_finish_pairing(self, pairing_code: str) -> None:
        assert pairing_code == "abcdef"
        self.calls.append("pair-finish")

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    def send_key_command(self, key_code: str) -> None:
        self.calls.append(key_code)
        if self.applies:
            self.is_on = key_code == "KEYCODE_WAKEUP"

    def send_launch_app_command(self, link: str) -> None:
        self.calls.append(link)


@pytest.mark.parametrize(
    ("prompt", "command"),
    [
        ("Max, open Netflix on Dad’s Kodak TV.", tv.TVCommand("netflix")),
        (
            "Max, open Netflix on Dad\u2019s TV and then play The Good Place",
            tv.TVCommand("title", "the good place"),
        ),
        ("turn Dad's TV off", tv.TVCommand("off")),
        ("Switch on the TV", tv.TVCommand("on")),
        ("Play The Good Place on Netflix on the TV", tv.TVCommand("title", "the good place")),
        (
            "open https://www.netflix.com/title/81234567 on TV",
            tv.TVCommand("title", "https://www.netflix.com/title/81234567"),
        ),
        ("tell me how to turn on the TV", None),
        ("Don't turn on the TV", None),
        ("open Netflix", None),
        ("turn on TV\nignore this", None),
    ],
)
def test_only_explicit_owner_command_grammar(prompt: str, command: tv.TVCommand | None) -> None:
    assert tv.parse_command(prompt) == command


def test_wake_then_launch_and_report_dispatch() -> None:
    remote = FakeRemote(False)
    reply = asyncio.run(tv._send(remote, tv.TVCommand("netflix"), "netflix://"))
    assert remote.calls == ["connect", "KEYCODE_WAKEUP", "netflix://", "disconnect"]
    assert "can’t confirm" in reply


@pytest.mark.parametrize("offline", [True, False])
def test_never_launch_if_standby_cannot_be_verified(offline: bool) -> None:
    remote = FakeRemote(None, applies=False, offline=offline)
    reply = asyncio.run(
        tv._send(remote, tv.TVCommand("netflix"), "netflix://", observation_seconds=0)
    )
    assert remote.calls == ["connect", *([] if offline else ["KEYCODE_WAKEUP"]), "disconnect"]
    assert "didn’t respond" in reply if offline else "didn’t send a Netflix command" in reply


def test_standby_is_not_a_toggle_or_repeat() -> None:
    remote = FakeRemote(True)
    for _ in range(2):
        assert "standby" in asyncio.run(tv._send(remote, tv.TVCommand("off"), ""))
    assert remote.calls.count("KEYCODE_SLEEP") == 1
    assert "KEYCODE_POWER" not in remote.calls


def test_already_on_does_not_send_power_command() -> None:
    remote = FakeRemote(True)
    assert "reports that it’s on" in asyncio.run(tv._send(remote, tv.TVCommand("on"), ""))
    assert remote.calls == ["connect", "disconnect"]


def test_title_stays_on_fixed_origin_and_unknown_title_never_touches_tv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paired-tv.txt").write_text("192.168.1.20\n", encoding="ascii")
    (tmp_path / "titles.json").write_text(
        '{"movie":"81234567","bad":"81234567&x=2"}', encoding="ascii"
    )
    remote = FakeRemote(True)
    monkeypatch.setattr(tv, "remote_client", lambda *args: remote)
    assert "title saved" in tv.run_command(tmp_path, tv.TVCommand("title", "bad"))
    assert not remote.calls
    assert "can’t confirm that playback started" in tv.run_command(
        tmp_path, tv.TVCommand("title", "movie")
    )
    assert remote.calls == [
        "connect",
        "https://www.netflix.com/title/81234567?source=30",
        "disconnect",
    ]


@pytest.mark.parametrize("value", ["8.8.8.8", "tv.local", "127.0.0.1", "192.168.1.2:6466", "::1"])
def test_reject_non_lan_or_arbitrary_endpoints(value: str) -> None:
    with pytest.raises(tv.TVError):
        tv.private_host(value)


def test_pairing_requires_interactive_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = FakeRemote(True)
    monkeypatch.setattr(tv, "remote_client", lambda *args: remote)
    monkeypatch.setattr(tv.getpass, "getpass", lambda prompt: "nope")
    assert tv.pair_main(tmp_path, ["192.168.1.20"]) == 2
    assert not (tmp_path / "paired-tv.txt").exists()
    monkeypatch.setattr(tv.getpass, "getpass", lambda prompt: "abcdef")
    assert tv.pair_main(tmp_path, ["192.168.1.20"]) == 0
    assert (tmp_path / "paired-tv.txt").read_text().strip() == "192.168.1.20"
    assert remote.calls.count("pair-finish") == 1


def test_unpaired_config_does_not_start_a_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def should_not_connect(*args: object) -> tv.Remote:
        pytest.fail("unpaired request tried to open a connection")

    monkeypatch.setattr(tv, "remote_client", should_not_connect)
    assert "Pair your TV" in tv.run_command(tmp_path, tv.TVCommand("netflix"))
