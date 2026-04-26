"""Plugin runtime — launches a plugin as a subprocess in its own uv venv,
talks JSON-RPC over newline-delimited stdio, captures stderr to a logfile.

One ``Plugin`` instance per running plugin. Calls are serialised through an
internal lock so concurrent ``call()`` invocations are safe; the same lock guards
``start()`` so we never spawn twice.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, IO

from aidj.config import settings
from aidj.plugins.manifest import LoadedManifest

log = logging.getLogger(__name__)


class PluginError(RuntimeError):
    """Raised when a plugin RPC call fails."""

    def __init__(self, code: int, message: str, trace: str | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.trace = trace


class Plugin:
    """A long-lived plugin process accessed via newline-delimited JSON-RPC."""

    def __init__(self, loaded: LoadedManifest) -> None:
        self._loaded = loaded
        self._proc: subprocess.Popen[str] | None = None
        self._log_fp: IO[str] | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    @property
    def manifest(self) -> LoadedManifest:
        return self._loaded

    @property
    def name(self) -> str:
        return self._loaded.name

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.is_alive:
            return
        m = self._loaded.manifest
        project_dir = self._loaded.project_dir
        log_fp = self._open_log()
        try:
            cmd = [
                "uv", "run",
                "--project", str(project_dir),
                "--python", m.python,
                "python", "-u",  # unbuffered I/O
                "-m", m.entrypoint_module,
            ]
            log.info("starting plugin %s@%s", m.name, m.version)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_fp,
                text=True,
                bufsize=1,
            )
        except Exception:
            log_fp.close()
            raise
        self._log_fp = log_fp
        self._proc = proc

    def _open_log(self) -> IO[str]:
        log_path = settings().logs_root / f"plugin-{self._loaded.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fp = log_path.open("a")
        fp.write(f"\n--- starting {self._loaded.name}@{self._loaded.version} ---\n")
        fp.flush()
        return fp

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            if not self.is_alive:
                self._close_log()
                return
            assert self._proc is not None
            log.info("stopping plugin %s", self._loaded.name)
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("plugin %s did not exit in %.1fs; killing", self._loaded.name, timeout)
                self._proc.kill()
                self._proc.wait()
            finally:
                self._close_log()
                self._proc = None

    def _close_log(self) -> None:
        if self._log_fp is not None:
            try:
                self._log_fp.write(f"--- stopped {self._loaded.name} ---\n")
            except Exception:  # pragma: no cover — log already gone
                pass
            self._log_fp.close()
            self._log_fp = None

    # -- RPC ---------------------------------------------------------------

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke a method on the plugin and wait for its response."""
        with self._lock:
            self._start_locked()
            assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None

            self._next_id += 1
            req = {"id": self._next_id, "method": method, "params": params or {}}
            self._proc.stdin.write(json.dumps(req) + "\n")
            self._proc.stdin.flush()

            line = self._proc.stdout.readline()
            if not line:
                rc = self._proc.poll()
                raise PluginError(
                    -32099,
                    f"plugin '{self._loaded.name}' exited (rc={rc}) before responding",
                )
            resp = json.loads(line)
            if "error" in resp:
                err = resp["error"]
                raise PluginError(err.get("code", -1), err.get("message", "unknown error"), err.get("trace"))
            return resp.get("result")


def discover_plugins(plugin_root: Path) -> list[LoadedManifest]:
    """Return loaded manifests for every subdirectory of ``plugin_root`` that has one."""
    if not plugin_root.is_dir():
        return []
    out: list[LoadedManifest] = []
    for child in sorted(plugin_root.iterdir()):
        if not child.is_dir() or not (child / "manifest.yaml").is_file():
            continue
        try:
            out.append(LoadedManifest.load(child))
        except Exception as exc:  # pragma: no cover — bad manifest
            log.error("failed to load manifest at %s: %s", child, exc)
    return out


