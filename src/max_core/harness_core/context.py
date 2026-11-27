"""Plugin context: services, events and reversible effects.

Every registration a plugin makes returns a disposer; unloading the plugin runs
them in reverse order. Listeners are *observers or deniers only*: no event
result can approve an effectful tool. Permission stays in trusted loop code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Disposer = Callable[[], None]
Listener = Callable[[dict[str, Any]], Any]

EXTENSION_EVENTS = frozenset(
    {
        "turn/start",
        "turn/end",
        "step/start",
        "step/end",
        "agent/request",
        "tools/pre-execute",
        "tools/post-execute",
    }
)


@dataclass(slots=True)
class Plugin:
    name: str
    setup: Callable[[Context], None]
    _disposers: list[Disposer] = field(default_factory=list)


class Context:
    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Listener]] = {k: [] for k in EXTENSION_EVENTS}
        self._plugins: dict[str, Plugin] = {}
        self._loading: Plugin | None = None

    def _effect(self, disposer: Disposer) -> Disposer:
        if self._loading is not None:
            self._loading._disposers.append(disposer)
        return disposer

    def provide(self, name: str, service: Any) -> Disposer:
        if name in self._services:
            raise ValueError("duplicate_service")
        self._services[name] = service
        return self._effect(lambda: self._services.pop(name, None))

    def service(self, name: str) -> Any:
        return self._services.get(name)

    def on(self, event: str, listener: Listener) -> Disposer:
        if event not in EXTENSION_EVENTS:
            raise ValueError("unknown_extension_event")
        self._listeners[event].append(listener)

        def dispose() -> None:
            if listener in self._listeners[event]:
                self._listeners[event].remove(listener)

        return self._effect(dispose)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        """Observer events: listener failures are swallowed, never alter the turn."""
        for listener in tuple(self._listeners[event]):
            try:
                listener(dict(payload))
            except Exception:
                pass

    def deny_reason(self, event: str, payload: dict[str, Any]) -> str | None:
        """Gate events: any listener returning a string denies; errors fail closed."""
        for listener in tuple(self._listeners[event]):
            try:
                verdict = listener(dict(payload))
            except Exception:
                return "hook_failed"
            if isinstance(verdict, str) and verdict:
                return verdict[:64]
        return None

    def load(self, plugin: Plugin) -> None:
        if plugin.name in self._plugins:
            raise ValueError("duplicate_plugin")
        self._loading = plugin
        try:
            plugin.setup(self)
        except Exception:
            self._loading = None
            self._unwind(plugin)
            raise
        self._loading = None
        self._plugins[plugin.name] = plugin

    def unload(self, name: str) -> None:
        plugin = self._plugins.pop(name, None)
        if plugin is not None:
            self._unwind(plugin)

    @staticmethod
    def _unwind(plugin: Plugin) -> None:
        while plugin._disposers:
            plugin._disposers.pop()()
