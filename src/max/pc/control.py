"""Windows PC actions and local Valorant profiles.

Security: only fixed app mappings and vetted HTTPS/Spotify URLs execute here.
General requests go through MAX's existing Agent and tool permission gates.
Never executes user-supplied shell text, changes protected game memory, or
stores login credentials. Profile application is explicitly guided, not
misrepresented as a successful game UI change.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$", re.I)
_SPOTIFY_RE = re.compile(
    r"(?:spotify:playlist:|https://open\.spotify\.com/playlist/)([A-Za-z0-9]{22})(?:[?/#].*)?$",
    re.I,
)
_WAKE_RE = re.compile(r"^(?:hi|hey|hello)[,\s]+max[.!?]*$|^wake up[,\s]+max[.!?]*$", re.I)
_SLEEP_RE = re.compile(r"^bye[,\s]+max[.!?]*$|^goodbye[,\s]+max[.!?]*$|^go to sleep[,\s]+max[.!?]*$", re.I)
_KEYS = frozenset(
    {"shift", "ctrl", "alt", "space", "tab", "capslock", "c", "v", "x", "z",
     "q", "e", "f", "g", "b", "1", "2", "3", "4", "5", "mouse4", "mouse5",
     "leftshift", "leftctrl", "rightshift", "rightctrl"}
)


@dataclass(frozen=True)
class PCReply:
    text: str
    action: str = "chat"
    ok: bool = True


def greeting(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    period = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    return f"Good {period}, boss. MAX is ready. Say hi Max when you want me to listen."


def safe_https_url(value: str) -> str | None:
    """Only ordinary public HTTPS URLs; no embedded credentials/local hosts."""
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 443):
            return None
        if host.lower() in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return None
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or "." not in host:
                return None
        else:
            if not addr.is_global:
                return None
        if any(c in value for c in "\r\n\\"):
            return None
        return value
    except (ValueError, TypeError):
        return None


class ProfileStore:
    """Small, atomic, local-only gaming settings store (never game secrets)."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "pc" / "valorant_profiles.json"

    def load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            result = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                return {}
            return {
                k: v for k, v in result.items()
                if _PROFILE_RE.fullmatch(k) and isinstance(v, dict)
            }
        except (OSError, ValueError):
            return {}

    def remember(self, profile: str, settings: dict) -> dict:
        if not _PROFILE_RE.fullmatch(profile):
            raise ValueError("profile name must be 1-32 letters, digits, hyphens or underscores")
        if not settings:
            raise ValueError("no recognized settings found")
        saved = self.load()
        new = dict(saved.get(profile, {}))
        new.update(settings)
        saved[profile] = new
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".profiles-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(saved, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return new

    def get(self, profile: str = "default") -> dict:
        if not _PROFILE_RE.fullmatch(profile):
            return {}
        return self.load().get(profile, {})


def parse_valorant_settings(text: str) -> dict:
    """Intentionally narrow, validated settings (no account/password capture)."""
    found: dict = {}
    sens = re.search(r"\b(?:sensitivity|sens)\s*(?:to|=|:)?\s*(\d+(?:\.\d+)?)\b", text, re.I)
    if sens:
        value = float(sens.group(1))
        if 0.01 <= value <= 10:
            found["sensitivity"] = value
    for name in ("crouch", "walk", "jump"):
        match = re.search(rf"\b{name}\s*(?:key|bind|to|=|:)?\s*(?:to|=|:)?\s*([A-Za-z0-9]+)\b", text, re.I)
        if match and match.group(1).lower() in _KEYS:
            found[name] = match.group(1).upper()
    return found


class AppLauncher:
    """Narrow action surface: installed apps, Spotify links and public URLs."""

    _EXE = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "paint": "mspaint.exe",
        "explorer": "explorer.exe",
    }

    def __init__(self, platform: str | None = None, opener=None, spawn=None):
        self.platform = platform or sys.platform
        self._open = opener or webbrowser.open
        self._spawn = spawn or subprocess.Popen

    def open_app(self, name: str) -> PCReply:
        app = name.casefold().strip()
        if self.platform != "win32":
            return PCReply("App launching is available only on Windows.", "app", False)
        if app in self._EXE:
            try:
                self._spawn([self._EXE[app]], shell=False)
                return PCReply(f"Opening {app}.", "app")
            except OSError:
                return PCReply(f"Couldn't start {app}. Is it installed?", "app", False)
        if app in {"browser", "chrome"}:
            return self.open_url("https://www.google.com")
        if app == "spotify":
            return self.open_url("https://open.spotify.com/")
        if app == "discord":
            home = Path(os.environ.get("LOCALAPPDATA", "")) / "Discord" / "Update.exe"
            if home.is_file():
                try:
                    self._spawn([str(home), "--processStart", "Discord.exe"], shell=False)
                    return PCReply("Opening Discord.", "app")
                except OSError:
                    pass
            return self.open_url("https://discord.com/app")
        if app == "valorant":
            paths = [
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Riot Games" / "Riot Client" / "RiotClientServices.exe",
                Path("C:/Riot Games/Riot Client/RiotClientServices.exe"),
            ]
            for p in paths:
                if p.is_file():
                    try:
                        self._spawn([str(p), "--launch-product=valorant", "--launch-patchline=live"], shell=False)
                        return PCReply("Launching Valorant through Riot Client.", "app")
                    except OSError:
                        continue
            return PCReply("Riot Client wasn't found in the standard locations. Launch it manually once.", "app", False)
        return PCReply("This app is not in the trusted launcher list yet.", "app", False)

    def open_url(self, url: str) -> PCReply:
        if not safe_https_url(url):
            return PCReply("I can open public HTTPS websites only.", "browser", False)
        if self._open(url) is False:
            return PCReply("The browser did not accept the URL.", "browser", False)
        return PCReply("Opened the website in your browser.", "browser")

    def search_web(self, query: str) -> PCReply:
        query = query.strip()
        if not query or len(query) > 250:
            return PCReply("Use a search query of 1-250 characters.", "browser", False)
        return self.open_url("https://www.google.com/search?q=" + quote_plus(query))

    def volume(self, action: str, *, press=None) -> PCReply:
        if self.platform != "win32":
            return PCReply("Windows audio controls are unavailable here.", "volume", False)
        key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}.get(action)
        if not key:
            return PCReply("Unsupported volume action.", "volume", False)
        if press is None:
            try:
                import pyautogui
                press = pyautogui.press
            except ImportError:
                return PCReply("Install the desktop extra to control volume.", "volume", False)
        try:
            press(key)
            return PCReply("Windows volume " + action + " command sent.", "volume")
        except Exception:
            return PCReply("Windows did not accept the volume command.", "volume", False)

    def spotify_playlist(self, uri: str) -> PCReply:
        match = _SPOTIFY_RE.fullmatch(uri.strip())
        if not match:
            return PCReply("Use a Spotify playlist link or spotify:playlist:ID.", "spotify", False)
        link = f"https://open.spotify.com/playlist/{match.group(1)}"
        result = self.open_url(link)
        if not result.ok:
            return result
        return PCReply(
            "Opened the playlist in Spotify. Automatic playback requires Spotify Premium "
            "and authorized playback-control integration; opening alone does not start music.",
            "spotify",
        )


class PCController:
    """Wake-controlled local actions; LLM fallback uses MAX's existing policy."""

    def __init__(self, data_dir: Path, ask=None, launcher=None):
        self.awake = False
        self.profiles = ProfileStore(data_dir)
        self.launcher = launcher or AppLauncher()
        self.ask = ask

    def handle(self, message: str) -> PCReply:
        text = message.strip()
        if not text:
            return PCReply("I didn't catch that.", "empty", False)
        if _WAKE_RE.fullmatch(text):
            self.awake = True
            return PCReply("I'm here, boss. What do you need?", "wake")
        if _SLEEP_RE.fullmatch(text):
            self.awake = False
            return PCReply("Going quiet, boss. Say hi Max when you need me.", "sleep")
        if not self.awake:
            return PCReply("MAX is sleeping. Say hi Max to wake him.", "sleeping", False)

        value = re.fullmatch(r"(?:launch|open|start)\s+(notepad|calculator|paint|explorer|browser|chrome|spotify|discord|valorant)[.!]?", text, re.I)
        if value:
            return self.launcher.open_app(value.group(1))
        url = re.fullmatch(r"(?:open|browse|visit)\s+(https://\S+)", text, re.I)
        if url:
            return self.launcher.open_url(url.group(1))
        search = re.fullmatch(r"(?:search (?:the )?web for|google)\s+(.+)", text, re.I)
        if search:
            return self.launcher.search_web(search.group(1))
        volume = re.fullmatch(r"(?:volume\s+(up|down)|mute(?:\s+volume)?)", text, re.I)
        if volume:
            return self.launcher.volume(volume.group(1).lower() if volume.group(1) else "mute")
        if re.fullmatch(r"(?:what(?:'s| is) the time|what time is it)[?!.]?", text, re.I):
            return PCReply("Your PC local time is " + datetime.now().strftime("%I:%M %p") + ".", "clock")
        if re.fullmatch(r"(?:help|what can you do)[?!.]?", text, re.I):
            return PCReply(
                "I can open approved apps, search the web, change volume, remember Valorant "
                "settings profiles, and answer questions with your configured AI. "
                "Say bye max to put me to sleep.", "help"
            )
        play = re.fullmatch(r"(?:play|open)\s+(?:my\s+)?(?:spotify\s+)?playlist\s+(.+)", text, re.I)
        if play:
            return self.launcher.spotify_playlist(play.group(1))

        save = re.fullmatch(
            r"(?:remember|save)\s+(?:my\s+)?valorant\s+settings"
            r"(?:\s+(?:for|as)\s+([A-Za-z0-9_-]+))?\s*[:,-]?\s*(.*)",
            text, re.I,
        )
        if save:
            profile = (save.group(1) or "default").lower()
            settings = parse_valorant_settings(save.group(2))
            if not settings:
                return PCReply("Try: remember my Valorant settings: sens 0.4 crouch C walk shift", "profile", False)
            try:
                stored = self.profiles.remember(profile, settings)
            except (OSError, ValueError) as exc:
                return PCReply(f"Could not save the profile ({type(exc).__name__}).", "profile", False)
            return PCReply(f"Saved Valorant profile '{profile}': " + _describe(stored), "profile")

        apply = re.fullmatch(
            r"(?:apply|show|recall)\s+(?:my\s+)?valorant\s+settings"
            r"(?:\s+(?:for|from)\s+([A-Za-z0-9_-]+))?[.!]?",
            text, re.I,
        )
        if apply:
            profile = (apply.group(1) or "default").lower()
            values = self.profiles.get(profile)
            if not values:
                return PCReply(f"No saved Valorant profile named '{profile}' yet.", "profile", False)
            return PCReply(
                f"Valorant profile '{profile}': " + _describe(values)
                + ". These settings are saved locally. For now apply them in the game's Settings "
                  "screen; automatic game UI application has NOT been verified and is not performed.",
                "profile",
            )

        if self.ask is None:
            return PCReply("No AI model connected. Configure MAX_PROVIDER, MAX_MODEL and MAX_BASE_URL.", "chat", False)
        try:
            result = self.ask(text, "pc_local")
            return PCReply(str(getattr(result, "text", result)), "chat", bool(getattr(result, "ok", True)))
        except Exception:
            return PCReply("My AI provider couldn't answer. Check your model settings.", "chat", False)


def _describe(settings: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(settings.items()))
