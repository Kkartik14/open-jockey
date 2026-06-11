"""In-process plugin registry.

Lazy: ``Plugin`` objects are constructed on first ``get()``. Each plugin's
subprocess is started lazily on its first ``call()``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from aidj.config import settings
from aidj.plugins.manifest import LoadedManifest
from aidj.plugins.runtime import Plugin, discover_plugins

log = logging.getLogger(__name__)


class Registry:
    def __init__(self, plugin_root: Path) -> None:
        self.plugin_root = plugin_root
        self._lock = threading.Lock()
        self._loaded: dict[str, LoadedManifest] = {}
        self._plugins: dict[str, Plugin] = {}
        self._discovered = False

    def _ensure_discovered(self) -> None:
        if self._discovered:
            return
        with self._lock:
            if self._discovered:
                return
            for lm in discover_plugins(self.plugin_root):
                self._loaded[lm.name] = lm
            self._discovered = True
            log.debug("discovered %d plugin(s) at %s", len(self._loaded), self.plugin_root)

    def manifests(self) -> list[LoadedManifest]:
        self._ensure_discovered()
        return list(self._loaded.values())

    def get(self, name: str) -> Plugin:
        self._ensure_discovered()
        if name not in self._loaded:
            raise KeyError(f"plugin not registered: {name}")
        if name not in self._plugins:
            with self._lock:
                if name not in self._plugins:
                    self._plugins[name] = Plugin(self._loaded[name])
        return self._plugins[name]

    def stop_all(self) -> None:
        with self._lock:
            for p in self._plugins.values():
                p.stop()
            self._plugins.clear()


_registry: Registry | None = None


def registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry(settings().plugins_root)
    return _registry


def reset_registry() -> None:
    """Stop and clear the active registry. Used by test teardown."""
    global _registry
    if _registry is not None:
        _registry.stop_all()
    _registry = None
