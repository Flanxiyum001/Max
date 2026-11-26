"""Opt-in owner commands for one PIN-paired Android TV. Never exposed to models."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import ipaddress
import json
import re
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

_TV = r"(?:dad(?:'s|s|’s)? (?:kodak )?tv|(?:the |my )?(?:kodak )?(?:android )?tv)"
_TITLE_ID = re.compile(r"[0-9]{5,15}")
_TITLE_LINK = re.compile(r"https://www\.netflix\.com/title/([0-9]{5,15})(?:\?source=30)?")
_LAN = tuple(ipaddress.IPv4Network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_NETFLIX = "netflix://"
_WAIT = 5.0


class TVError(ValueError):
    """Only user-safe messages, not transport exceptions."""


@dataclass(frozen=True, slots=True)
class TVCommand:
    action: Literal["on", "off", "netflix", "title"]
    title: str = ""


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


def parse_command(prompt: str) -> TVCommand | None:
    if len(prompt) > 300 or any(ord(char) < 32 for char in prompt):
        return None
    text = re.sub(r"^max\s*[,.:]?\s+", "", _normal(prompt)).rstrip(".!?")
    if re.fullmatch(rf"(?:turn|switch|power) on {_TV}|(?:turn|switch|power) {_TV} on", text):
        return TVCommand("on")
    if re.fullmatch(rf"(?:turn|switch|power) off {_TV}|(?:turn|switch|power) {_TV} off", text):
        return TVCommand("off")
    if re.fullmatch(rf"(?:open|launch|start) netflix on {_TV}", text):
        return TVCommand("netflix")
    combined = re.fullmatch(rf"open netflix on {_TV},? (?:and )?then play (.+)", text)
    if combined:
        return TVCommand("title", combined[1].strip('"\u201c\u201d'))
    match = re.fullmatch(rf"(?:open|play) (.+?) on (?:netflix on )?{_TV}", text)
    if match and (" on netflix on " in text or match[1].startswith("https://www.netflix.com/")):
        return TVCommand("title", match[1].strip('"“”'))
    return None


def private_host(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise TVError("Enter the TV’s private IPv4 address from its network settings.") from None
    if not any(address in network for network in _LAN):
        raise TVError("MAX only supports TV pairing on a private home network.")
    return str(address)


class Remote(Protocol):
    @property
    def is_on(self) -> bool | None: ...

    async def async_connect(self) -> None: ...
    async def async_generate_cert_if_missing(self) -> bool: ...
    async def async_start_pairing(self) -> None: ...
    async def async_finish_pairing(self, pairing_code: str) -> None: ...
    def disconnect(self) -> None: ...
    def send_key_command(self, key_code: str) -> None: ...
    def send_launch_app_command(self, app_link_or_app_id: str) -> None: ...


def remote_client(host: str, folder: Path) -> Remote:
    try:
        android_tv_remote = importlib.import_module("androidtvremote2").AndroidTVRemote
    except ImportError:
        raise TVError("Install MAX’s optional TV support with pip install -e .[tv].") from None
    return cast(
        Remote,
        android_tv_remote(
            "MAX TV Remote",
            str(folder / "client.pem"),
            str(folder / "client.key"),
            host,
            enable_ime=False,
            enable_voice=False,
        ),
    )


def _read(path: Path) -> object:
    with path.open(encoding="utf-8") as stream:
        return json.loads(stream.read(16_385))


def _host(root: Path) -> str:
    try:
        return private_host((root / "paired-tv.txt").read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, TVError):
        raise TVError("Pair your TV first with start-max.cmd --pair-tv <TV IP>.") from None


def _title_link(root: Path, query: str) -> str:
    match = _TITLE_LINK.fullmatch(query)
    if match:
        title_id: object = match[1]
    else:
        try:
            aliases = _read(root / "titles.json")
        except (OSError, ValueError):
            aliases = {}
        title_id = aliases.get(query) if isinstance(aliases, Mapping) else None
    if not isinstance(title_id, str) or not _TITLE_ID.fullmatch(title_id):
        raise TVError("I don’t have that Netflix title saved. Add it to your TV titles file first.")
    return f"https://www.netflix.com/title/{title_id}?source=30"


async def _observe(remote: Remote, expected: bool, seconds: float = _WAIT) -> bool:
    loop = asyncio.get_running_loop()
    cutoff = loop.time() + seconds
    while remote.is_on is not expected and loop.time() < cutoff:
        await asyncio.sleep(0.1)
    return remote.is_on is expected


async def _send(
    remote: Remote, command: TVCommand, link: str, *, observation_seconds: float = _WAIT
) -> str:
    try:
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=_WAIT)
        except (OSError, TimeoutError):
            return "The TV didn’t respond. If it’s offline in standby, use its physical remote."
        except Exception:
            return (
                "I couldn’t connect to the TV. If it’s offline in standby, use its physical "
                "remote. If it’s on, check Wi-Fi and pairing."
            )

        if command.action == "off":
            if remote.is_on is False:
                return "The TV is already in standby."
            remote.send_key_command("KEYCODE_SLEEP")
            if await _observe(remote, False, observation_seconds):
                return "The TV reports that it’s in standby."
            return "I sent standby to the TV, but couldn’t confirm it switched off."

        if remote.is_on is not True:
            remote.send_key_command("KEYCODE_WAKEUP")
            if not await _observe(remote, True, observation_seconds):
                return "I couldn’t confirm the TV woke up. I didn’t send a Netflix command."
        if command.action == "on":
            return "The TV reports that it’s on."
        remote.send_launch_app_command(link)
        # Let the buffered message leave before closing; this is not an acknowledgment.
        await asyncio.sleep(0.25)
        if command.action == "title":
            return "I sent the Netflix title to the TV. I can’t confirm that playback started."
        return "I sent the request to open Netflix. I can’t confirm it opened on the TV."
    except Exception:
        return "The TV connection interrupted the request. I didn’t retry it."
    finally:
        remote.disconnect()


async def _run(root: Path, command: TVCommand) -> str:
    host = _host(root)
    link = _title_link(root, command.title) if command.action == "title" else _NETFLIX
    return await _send(remote_client(host, root / host), command, link)


def run_command(root: Path, command: TVCommand) -> str:
    try:
        return asyncio.run(_run(root, command))
    except TVError as exc:
        return str(exc)


async def _pair(root: Path, host: str) -> None:
    folder = root / host
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    remote = remote_client(host, folder)
    try:
        await asyncio.wait_for(remote.async_generate_cert_if_missing(), timeout=_WAIT)
        await asyncio.wait_for(remote.async_start_pairing(), timeout=10)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            code = getpass.getpass("Enter the six-character code shown on the TV: ").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{6}", code):
            raise TVError("Enter the six hexadecimal characters shown on the TV.")
        await asyncio.wait_for(remote.async_finish_pairing(code), timeout=10)
        (root / "paired-tv.txt").write_text(host + "\n", encoding="ascii")
    finally:
        remote.disconnect()


def pair_main(root: Path, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="start-max.cmd --pair-tv")
    parser.add_argument("tv_ip", help="private IPv4 address shown in the TV’s network settings")
    parsed = parser.parse_args(args)
    try:
        asyncio.run(_pair(root, private_host(parsed.tv_ip)))
    except TVError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (Exception, KeyboardInterrupt):
        print("TV pairing did not complete. Check Wi-Fi and the code on the TV.", file=sys.stderr)
        return 2
    print("Android TV Remote pairing completed. Try opening Netflix from MAX Desktop.")
    return 0
