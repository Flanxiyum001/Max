"""MAX personal-assistant toolkit: memory, reminders, web, weather, PC and apps.

Every tool goes through ``ToolRegistry``/``ToolLoop``: the model only proposes,
trusted code validates arguments, and ``EFFECTFUL`` tools need the owner's
approval for each exact call. Personal-data tools are local-only unless the
owner explicitly opts in for cloud models (see ``assistant_registry``).
"""

from __future__ import annotations

import datetime as _dt
import html
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import uuid
import webbrowser
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

import httpx

from max_core.tools import (
    MAX_RESULT_CHARS,
    Tool,
    ToolError,
    ToolParam,
    ToolRegistry,
    ToolRisk,
    builtin_registry,
)

HttpFactory = Callable[[], httpx.Client]
_USER_AGENT = "MAX-personal-assistant/0.1"


# ---------------------------------------------------------------------------
# Local store (JSON files in the owner's private MAX data directory)


class AssistantStore:
    """Small JSON-file store. Single process; writes are atomic replace."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir).expanduser()
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def _load(self, name: str) -> list[dict[str, Any]]:
        path = self._path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise ToolError("store_unreadable") from None
        if not isinstance(data, list):
            raise ToolError("store_unreadable")
        return [item for item in data if isinstance(item, dict)]

    def _save(self, name: str, items: list[dict[str, Any]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(name).with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path(name))

    # memory ---------------------------------------------------------------
    def remember(self, fact: str) -> str:
        fact = " ".join(fact.split())
        if not fact:
            raise ToolError("empty_fact")
        with self._lock:
            items = self._load("memory")
            if any(item.get("fact", "").lower() == fact.lower() for item in items):
                return "already remembered"
            if len(items) >= 2_000:
                raise ToolError("memory_full")
            item_id = uuid.uuid4().hex[:8]
            items.append({"id": item_id, "fact": fact, "created": _now().isoformat()})
            self._save("memory", items)
        return f"remembered (id {item_id})"

    def recall(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        with self._lock:
            items = self._load("memory")
        if words:
            scored = [
                (sum(w in item.get("fact", "").lower() for w in words), item) for item in items
            ]
            items = [item for score, item in sorted(scored, key=lambda s: -s[0]) if score]
        return items[-limit:] if not words else items[:limit]

    def forget(self, item_id: str) -> str:
        with self._lock:
            items = self._load("memory")
            kept = [item for item in items if item.get("id") != item_id]
            if len(kept) == len(items):
                raise ToolError("not_found")
            self._save("memory", kept)
        return "forgotten"

    # reminders ------------------------------------------------------------
    def add_reminder(self, text: str, due: _dt.datetime | None) -> dict[str, Any]:
        text = " ".join(text.split())
        if not text:
            raise ToolError("empty_reminder")
        item = {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "due": due.isoformat() if due else None,
            "done": False,
            "notified": False,
            "created": _now().isoformat(),
        }
        with self._lock:
            items = self._load("reminders")
            if len([i for i in items if not i.get("done")]) >= 500:
                raise ToolError("too_many_reminders")
            items.append(item)
            self._save("reminders", items)
        return item

    def reminders(self, include_done: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            items = self._load("reminders")
        if not include_done:
            items = [i for i in items if not i.get("done")]
        return sorted(items, key=lambda i: (i.get("due") is None, i.get("due") or ""))

    def complete(self, item_id: str) -> str:
        with self._lock:
            items = self._load("reminders")
            for item in items:
                if item.get("id") == item_id:
                    item["done"] = True
                    self._save("reminders", items)
                    return "marked done"
        raise ToolError("not_found")

    def pop_due(self, now: _dt.datetime | None = None) -> list[dict[str, Any]]:
        """Reminders now due that have not been announced yet (marks them notified)."""
        now = now or _now()
        due: list[dict[str, Any]] = []
        with self._lock:
            items = self._load("reminders")
            for item in items:
                when = item.get("due")
                if item.get("done") or item.get("notified") or not isinstance(when, str):
                    continue
                try:
                    if _dt.datetime.fromisoformat(when) <= now:
                        item["notified"] = True
                        due.append(item)
                except ValueError:
                    continue
            if due:
                self._save("reminders", items)
        return due


def _now() -> _dt.datetime:
    return _dt.datetime.now().astimezone().replace(microsecond=0)


_REL = re.compile(r"^in\s+(\d{1,4})\s*(minute|min|hour|hr|day|week)s?$", re.IGNORECASE)


def parse_due(raw: str, now: _dt.datetime | None = None) -> _dt.datetime:
    """Accept 'in 10 minutes', 'tomorrow 09:00', 'today 18:30', 'YYYY-MM-DD HH:MM'."""
    now = now or _now()
    text = raw.strip().lower()
    match = _REL.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)[0]
        delta = {
            "m": _dt.timedelta(minutes=amount),
            "h": _dt.timedelta(hours=amount),
            "d": _dt.timedelta(days=amount),
            "w": _dt.timedelta(weeks=amount),
        }[unit]
        return now + delta
    day_match = re.fullmatch(r"(today|tomorrow)(?:\s+(?:at\s+)?(\d{1,2}):(\d{2}))?", text)
    if day_match:
        base = now.date() + _dt.timedelta(days=1 if day_match.group(1) == "tomorrow" else 0)
        hour, minute = (
            (int(day_match.group(2)), int(day_match.group(3))) if day_match.group(2) else (9, 0)
        )
        try:
            return _dt.datetime.combine(base, _dt.time(hour, minute), tzinfo=now.tzinfo)
        except ValueError:
            raise ToolError("invalid_due_time") from None
    try:
        parsed = _dt.datetime.fromisoformat(raw.strip())
    except ValueError:
        raise ToolError("invalid_due_time") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=now.tzinfo)


# ---------------------------------------------------------------------------
# Web access (public internet only; private/loopback addresses are refused)


def _public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ToolError("host_unresolvable") from None
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise ToolError("private_address_refused")


def _check_url(url: str, resolve: bool) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ToolError("invalid_url")
    if resolve:
        _public_host(parts.hostname)
    return url


_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)


def html_to_text(raw: str) -> str:
    text = html.unescape(_TAGS.sub(" ", raw))
    return re.sub(r"\s+", " ", text).strip()


class WebTools:
    def __init__(self, http: HttpFactory, *, resolve_hosts: bool = True) -> None:
        self._http = http
        self._resolve = resolve_hosts

    def _get(self, url: str, params: Mapping[str, str] | None = None) -> httpx.Response:
        with self._http() as client:
            for _ in range(4):
                _check_url(url, self._resolve)
                try:
                    response = client.get(
                        url, params=params, headers={"User-Agent": _USER_AGENT}, timeout=12.0
                    )
                except httpx.HTTPError:
                    raise ToolError("network_error") from None
                if response.is_redirect and "location" in response.headers:
                    url, params = urljoin(url, response.headers["location"]), None
                    continue
                if response.status_code >= 400:
                    raise ToolError(f"http_{response.status_code}")
                if len(response.content) > 2_000_000:
                    raise ToolError("response_too_large")
                return response
        raise ToolError("too_many_redirects")

    def search(self, args: Mapping[str, Any]) -> str:
        response = self._get("https://html.duckduckgo.com/html/", {"q": args["query"]})
        results = []
        for block in re.findall(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)'
            r'(?=<a[^>]+class="result__a"|$)',
            response.text,
            re.DOTALL,
        )[:6]:
            href, title, rest = block
            snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.DOTALL)
            link = html.unescape(href)
            real = re.search(r"uddg=([^&]+)", link)
            if real:
                from urllib.parse import unquote

                link = unquote(real.group(1))
            results.append(
                f"- {html_to_text(title)}\n  {link}\n  "
                f"{html_to_text(snippet.group(1)) if snippet else ''}"
            )
        return "\n".join(results) or "no results"

    def read(self, args: Mapping[str, Any]) -> str:
        response = self._get(args["url"])
        kind = response.headers.get("content-type", "")
        if "html" not in kind and "text" not in kind and "json" not in kind:
            raise ToolError("not_text_content")
        text = html_to_text(response.text) if "html" in kind else response.text
        return text[:MAX_RESULT_CHARS]

    def weather(self, args: Mapping[str, Any]) -> str:
        geo = self._get(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": args["place"], "count": "1"},
        ).json()
        places = geo.get("results") or []
        if not places:
            raise ToolError("place_not_found")
        place = places[0]
        data = self._get(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": str(place["latitude"]),
                "longitude": str(place["longitude"]),
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "precipitation,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": "3",
                "timezone": "auto",
            },
        ).json()
        cur = data.get("current", {})
        daily = data.get("daily", {})
        lines = [
            f"{place.get('name')}, {place.get('country', '')}: "
            f"{cur.get('temperature_2m')}°C (feels {cur.get('apparent_temperature')}°C), "
            f"{_WEATHER.get(cur.get('weather_code'), 'conditions unknown')}, "
            f"humidity {cur.get('relative_humidity_2m')}%, wind {cur.get('wind_speed_10m')} km/h"
        ]
        for i, day in enumerate(daily.get("time", [])[:3]):
            lines.append(
                f"{day}: {daily['temperature_2m_min'][i]}–{daily['temperature_2m_max'][i]}°C, "
                f"rain chance {daily['precipitation_probability_max'][i]}%"
            )
        return "\n".join(lines)


_WEATHER = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain",
    63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    80: "rain showers", 81: "rain showers", 82: "violent showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with hail",
}  # fmt: skip


# ---------------------------------------------------------------------------
# PC: status, open apps/URLs, shell


def system_status(_: Mapping[str, Any]) -> str:
    lines = [f"OS: {platform.system()} {platform.release()} ({platform.machine()})"]
    lines.append(f"CPU cores: {os.cpu_count()}")
    if hasattr(os, "getloadavg"):
        lines.append("Load average: " + ", ".join(f"{v:.2f}" for v in os.getloadavg()))
    try:
        import psutil  # type: ignore[import-not-found,import-untyped,unused-ignore]

        mem = psutil.virtual_memory()
        lines.append(f"CPU use: {psutil.cpu_percent(interval=0.3)}%")
        lines.append(f"Memory: {mem.percent}% used of {mem.total / 2**30:.1f} GiB")
        battery = psutil.sensors_battery()
        if battery is not None:
            plug = "charging" if battery.power_plugged else "on battery"
            lines.append(f"Battery: {battery.percent:.0f}% ({plug})")
    except Exception:
        lines.append("(install psutil for CPU %, memory and battery details)")
    usage = shutil.disk_usage(Path.home())
    lines.append(f"Disk (home): {usage.free / 2**30:.1f} GiB free of {usage.total / 2**30:.1f} GiB")
    return "\n".join(lines)


_APP = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,60}")


def open_target(
    args: Mapping[str, Any], launcher: Callable[[list[str]], None] | None = None
) -> str:
    target = args["target"].strip()
    if re.match(r"^https?://", target, re.IGNORECASE):
        _check_url(target, resolve=False)
        webbrowser.open(target)
        return f"opened {target}"
    if not _APP.fullmatch(target):
        raise ToolError("invalid_app_name")
    run = launcher or (lambda cmd: subprocess.Popen(cmd, close_fds=True))  # noqa: S603
    if sys.platform == "win32":
        run(["cmd", "/c", "start", "", target])
    elif sys.platform == "darwin":
        run(["open", "-a", target])
    else:
        exe = shutil.which(target.split()[0].lower())
        if exe is None:
            raise ToolError("app_not_found")
        run([exe])
    return f"launched {target}"


def run_shell(args: Mapping[str, Any], cwd: Path | None = None) -> str:
    """Owner-approved exact command; bounded time and output."""
    try:
        done = subprocess.run(  # noqa: S602 - owner approves the exact command text
            args["command"],
            shell=True,
            cwd=str(cwd or Path.home()),
            capture_output=True,
            text=True,
            timeout=60,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise ToolError("command_timeout") from None
    out = (done.stdout + ("\n[stderr]\n" + done.stderr if done.stderr.strip() else "")).strip()
    return f"exit code {done.returncode}\n{out[: MAX_RESULT_CHARS - 40]}"


# ---------------------------------------------------------------------------
# Registry


PERSONAL_TOOLS = frozenset(
    {
        "memory.remember",
        "memory.recall",
        "memory.forget",
        "reminders.add",
        "reminders.list",
        "reminders.complete",
        "system.status",
        "shell.run",
    }
)


def _fmt_reminder(item: Mapping[str, Any]) -> str:
    due = item.get("due")
    when = _dt.datetime.fromisoformat(due).strftime("%a %d %b %H:%M") if due else "no time"
    mark = "done" if item.get("done") else "open"
    return f"[{item['id']}] {item['text']} — {when} ({mark})"


def assistant_registry(
    *,
    data_dir: Path,
    workspace: Path | None = None,
    cloud: bool = False,
    cloud_personal_tools: bool = False,
    enable_shell: bool = False,
    enable_web: bool = True,
    http: HttpFactory | None = None,
    resolve_hosts: bool = True,
    host_tools: bool = True,
) -> ToolRegistry:
    """Built-ins + assistant tools.

    Cloud sessions never get workspace file tools, and get personal tools
    (memory, reminders, PC status, shell) only with ``cloud_personal_tools``.
    """
    # host_tools=False (hosted web server): nothing that acts on or reports about
    # the machine MAX runs on — no workspace, PC status, shell or app launching.
    registry = builtin_registry(None if cloud or not host_tools else workspace)
    store = AssistantStore(data_dir)
    personal = not cloud or cloud_personal_tools
    tools: list[Tool] = []
    if personal:
        tools += [
            Tool(
                "memory.remember",
                "Save a lasting fact about the owner or their preferences.",
                ToolRisk.EFFECTFUL,
                (ToolParam("fact", str, "the fact, one sentence", max_length=500),),
                lambda a: store.remember(a["fact"]),
            ),
            Tool(
                "memory.recall",
                "Search remembered facts. Empty query lists the most recent ones.",
                ToolRisk.READ_ONLY,
                (ToolParam("query", str, "keywords", required=False, max_length=200),),
                lambda a: (
                    "\n".join(f"[{i['id']}] {i['fact']}" for i in store.recall(a.get("query", "")))
                    or "nothing remembered yet"
                ),
            ),
            Tool(
                "memory.forget",
                "Delete a remembered fact by its id.",
                ToolRisk.EFFECTFUL,
                (ToolParam("id", str, "fact id from memory.recall", max_length=16),),
                lambda a: store.forget(a["id"]),
            ),
            Tool(
                "reminders.add",
                "Add a reminder or to-do. 'due' accepts 'in 20 minutes', 'in 2 hours', "
                "'today 18:30', 'tomorrow 09:00' or 'YYYY-MM-DD HH:MM'; omit for a plain to-do.",
                ToolRisk.EFFECTFUL,
                (
                    ToolParam("text", str, "what to remember to do", max_length=300),
                    ToolParam("due", str, "when", required=False, max_length=40),
                ),
                lambda a: (
                    "added "
                    + _fmt_reminder(
                        store.add_reminder(a["text"], parse_due(a["due"]) if a.get("due") else None)
                    )
                ),
            ),
            Tool(
                "reminders.list",
                "List open reminders and to-dos (set include_done to see finished ones).",
                ToolRisk.READ_ONLY,
                (ToolParam("include_done", bool, "include finished items", required=False),),
                lambda a: (
                    "\n".join(
                        _fmt_reminder(i) for i in store.reminders(a.get("include_done", False))
                    )
                    or "no reminders"
                ),
            ),
            Tool(
                "reminders.complete",
                "Mark a reminder or to-do as done by id.",
                ToolRisk.EFFECTFUL,
                (ToolParam("id", str, "reminder id", max_length=16),),
                lambda a: store.complete(a["id"]),
            ),
        ]
        if host_tools:
            tools.append(
                Tool(
                    "system.status",
                    "This PC's OS, CPU, memory, disk and battery status.",
                    ToolRisk.READ_ONLY,
                    (),
                    system_status,
                )
            )
        if enable_shell and host_tools:
            tools.append(
                Tool(
                    "shell.run",
                    "Run a shell command on the owner's PC (owner must approve the exact "
                    "command). 60 s limit.",
                    ToolRisk.EFFECTFUL,
                    (ToolParam("command", str, "the command line", max_length=1_000),),
                    lambda a: run_shell(a, workspace),
                )
            )
    if host_tools:
        tools.append(
            Tool(
                "apps.open",
                "Open a website (http/https URL) or launch an installed app by name.",
                ToolRisk.EFFECTFUL,
                (ToolParam("target", str, "URL or app name", max_length=500),),
                open_target,
            )
        )
    if enable_web:
        web = WebTools(
            http or (lambda: httpx.Client(follow_redirects=False)), resolve_hosts=resolve_hosts
        )
        tools += [
            Tool(
                "web.search",
                "Search the web; returns titles, links and snippets.",
                ToolRisk.READ_ONLY,
                (ToolParam("query", str, "search terms", max_length=300),),
                web.search,
            ),
            Tool(
                "web.read",
                "Fetch a public web page and return its text.",
                ToolRisk.READ_ONLY,
                (ToolParam("url", str, "http(s) URL", max_length=2_000),),
                web.read,
            ),
            Tool(
                "weather.current",
                "Current weather and 3-day forecast for a city or place name.",
                ToolRisk.READ_ONLY,
                (ToolParam("place", str, "city or place", max_length=100),),
                web.weather,
            ),
        ]
    if host_tools and not cloud and sys.platform == "win32":
        from max_core.desktop_awareness import desktop_tools

        tools.extend(desktop_tools())
    for tool in tools:
        registry.register(tool)
    return registry


def assistant_store(data_dir: Path) -> AssistantStore:
    return AssistantStore(data_dir)


__all__ = [
    "PERSONAL_TOOLS",
    "AssistantStore",
    "WebTools",
    "assistant_registry",
    "html_to_text",
    "open_target",
    "parse_due",
    "quote_plus",
    "run_shell",
    "system_status",
]
