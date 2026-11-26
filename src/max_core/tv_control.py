"""Living-room TV control over the home network (Android/Google TV and Roku).

Lets the owner say "MAX, play Stranger Things on Netflix" from another room.
Configuration (environment or ``.env``):

* ``MAX_TV_KIND``  - ``androidtv`` (Android TV / Google TV / Fire TV, via ADB)
  or ``roku`` (Roku TV / stick, via the External Control Protocol).
* ``MAX_TV_HOST``  - the TV's LAN IP address, e.g. ``192.168.1.50``.

Only private-LAN addresses are accepted. Every action is ``EFFECTFUL``, so it
goes through the normal MAX Trust approval unless the owner auto-approves the
``tv.*`` tools with ``MAX_TV_CONTROL=auto``.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

from max_core.tools import Tool, ToolError, ToolParam, ToolRisk

# App name -> (Android package, Roku channel id)
APPS: dict[str, tuple[str, str]] = {
    "netflix": ("com.netflix.ninja", "12"),
    "youtube": ("com.google.android.youtube.tv", "837"),
    "prime video": ("com.amazon.amazonvideo.livingroom", "13"),
    "disney+": ("com.disney.disneyplus", "291097"),
    "hotstar": ("in.startv.hotstar", "291097"),
    "spotify": ("com.spotify.tv.android", "22297"),
}
_ALIASES = {"prime": "prime video", "amazon": "prime video", "disney": "disney+",
            "disney plus": "disney+", "jiohotstar": "hotstar"}  # fmt: skip

# Friendly key -> (Android keyevent, Roku key)
KEYS: dict[str, tuple[str, str]] = {
    "power": ("KEYCODE_POWER", "Power"),
    "home": ("KEYCODE_HOME", "Home"),
    "back": ("KEYCODE_BACK", "Back"),
    "up": ("KEYCODE_DPAD_UP", "Up"),
    "down": ("KEYCODE_DPAD_DOWN", "Down"),
    "left": ("KEYCODE_DPAD_LEFT", "Left"),
    "right": ("KEYCODE_DPAD_RIGHT", "Right"),
    "ok": ("KEYCODE_DPAD_CENTER", "Select"),
    "play": ("KEYCODE_MEDIA_PLAY", "Play"),
    "pause": ("KEYCODE_MEDIA_PAUSE", "Play"),
    "playpause": ("KEYCODE_MEDIA_PLAY_PAUSE", "Play"),
    "forward": ("KEYCODE_MEDIA_FAST_FORWARD", "Fwd"),
    "rewind": ("KEYCODE_MEDIA_REWIND", "Rev"),
    "volumeup": ("KEYCODE_VOLUME_UP", "VolumeUp"),
    "volumedown": ("KEYCODE_VOLUME_DOWN", "VolumeDown"),
    "mute": ("KEYCODE_VOLUME_MUTE", "VolumeMute"),
}

Runner = Callable[[list[str]], str]
_TITLE = re.compile(r"[\w .,:'!?&()-]{1,120}", re.UNICODE)


def tv_auto_approved(name: str, env: Mapping[str, str]) -> bool:
    return env.get("MAX_TV_CONTROL", "").strip().lower() == "auto" and name.startswith("tv.")


def _lan_host(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host.strip())
    except ValueError:
        raise ToolError("tv_host_not_ip") from None
    if not ip.is_private or ip.is_loopback:
        raise ToolError("tv_host_not_lan")
    return str(ip)


def _app(name: str) -> tuple[str, tuple[str, str]]:
    key = _ALIASES.get(name.strip().lower(), name.strip().lower())
    if key not in APPS:
        raise ToolError("tv_unknown_app")
    return key, APPS[key]


def _title(raw: str) -> str:
    title = raw.strip()
    if not _TITLE.fullmatch(title):
        raise ToolError("tv_invalid_title")
    return title


def _default_runner(cmd: list[str]) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors="replace")  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        raise ToolError("tv_adb_failed") from None
    if done.returncode != 0:
        raise ToolError("tv_adb_failed")
    return done.stdout


class AndroidTV:
    """Android / Google / Fire TV via ``adb`` over Wi-Fi (port 5555)."""

    def __init__(self, host: str, run: Runner | None = None, adb: str | None = None) -> None:
        self.target = f"{_lan_host(host)}:5555"
        self.adb = adb or shutil.which("adb") or "adb"
        self._run = run or _default_runner

    def _shell(self, *args: str) -> str:
        out = self._run([self.adb, "connect", self.target])
        if "unable" in out.lower() or "failed" in out.lower():
            raise ToolError("tv_unreachable")
        return self._run([self.adb, "-s", self.target, "shell", *args])

    def launch(self, app: str) -> str:
        name, (pkg, _) = _app(app)
        self._shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
        return f"opened {name} on the TV"

    def play(self, app: str, title: str) -> str:
        name, (pkg, _) = _app(app)
        title = _title(title)
        if name == "netflix":
            uri = f"https://www.netflix.com/search?q={quote(title)}"
        elif name == "youtube":
            uri = f"https://www.youtube.com/results?search_query={quote_plus(title)}"
        else:
            uri = ""
        if uri:
            self._shell("am", "start", "-a", "android.intent.action.VIEW", "-d", uri, pkg)
        else:
            self._shell("am", "start", "-a", "android.search.action.GLOBAL_SEARCH",
                        "--es", "query", title)  # fmt: skip
        return f"searching {name} on the TV for '{title}' - use tv.press ok to start it"

    def press(self, key: str, times: int) -> str:
        code = KEYS[key][0]
        for _ in range(times):
            self._shell("input", "keyevent", code)
        return f"pressed {key} x{times}"


class RokuTV:
    """Roku via the External Control Protocol (HTTP on port 8060)."""

    def __init__(self, host: str, http: Callable[[], httpx.Client] | None = None) -> None:
        self.base = f"http://{_lan_host(host)}:8060"
        self._http = http or (lambda: httpx.Client(timeout=5))

    def _post(self, path: str) -> None:
        try:
            with self._http() as client:
                client.post(self.base + path).raise_for_status()
        except httpx.HTTPError:
            raise ToolError("tv_unreachable") from None

    def launch(self, app: str) -> str:
        name, (_, channel) = _app(app)
        self._post(f"/launch/{channel}")
        return f"opened {name} on the TV"

    def play(self, app: str, title: str) -> str:
        name, (_, channel) = _app(app)
        title = _title(title)
        self._post(f"/search/browse?keyword={quote(title)}&provider-id={channel}&launch=true")
        return f"playing '{title}' on {name} on the TV"

    def press(self, key: str, times: int) -> str:
        for _ in range(times):
            self._post(f"/keypress/{KEYS[key][1]}")
        return f"pressed {key} x{times}"


def make_tv(env: Mapping[str, str], **kw: Any) -> AndroidTV | RokuTV | None:
    kind = env.get("MAX_TV_KIND", "").strip().lower()
    host = env.get("MAX_TV_HOST", "").strip()
    if not kind or not host:
        return None
    if kind in {"androidtv", "googletv", "firetv"}:
        return AndroidTV(host, run=kw.get("run"))
    if kind == "roku":
        return RokuTV(host, http=kw.get("http"))
    raise ValueError("unknown MAX_TV_KIND")


def tv_tools(tv: AndroidTV | RokuTV) -> tuple[Tool, ...]:
    apps = ", ".join(APPS)

    def press(a: Mapping[str, Any]) -> str:
        key = a["key"].strip().lower()
        if key not in KEYS:
            raise ToolError("tv_unknown_key")
        times = a.get("times", 1)
        if not 1 <= times <= 20:
            raise ToolError("tv_times_out_of_range")
        return tv.press(key, times)

    return (
        Tool(
            "tv.play",
            f"Play a show/movie/video on the living-room TV. Apps: {apps}.",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("app", str, "streaming app, e.g. netflix", max_length=20),
                ToolParam("title", str, "show, movie or video name", max_length=120),
            ),
            lambda a: tv.play(a["app"], a["title"]),
        ),
        Tool(
            "tv.open_app",
            f"Open an app on the living-room TV. Apps: {apps}.",
            ToolRisk.EFFECTFUL,
            (ToolParam("app", str, "app name", max_length=20),),
            lambda a: tv.launch(a["app"]),
        ),
        Tool(
            "tv.press",
            "Press a TV remote button: " + ", ".join(KEYS) + ".",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("key", str, "button name", max_length=12),
                ToolParam("times", int, "repeat count, 1-20", required=False),
            ),
            press,
        ),
    )


__all__ = ["APPS", "KEYS", "AndroidTV", "RokuTV", "make_tv", "tv_auto_approved", "tv_tools"]
