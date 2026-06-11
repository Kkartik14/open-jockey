"""Plugin runtime — launches each plugin as a subprocess in its own uv venv,
talks JSON-RPC over newline-delimited stdio, captures stderr to a logfile.

Hardening:

- A dedicated reader thread pushes stdout lines onto a ``queue.Queue`` so
  ``call()`` can wait with a real timeout instead of blocking on ``readline``
  forever (Demucs/allin1 hangs are a likely failure mode).
- On timeout (or stdin write failure), the process is force-killed so the next
  ``call()`` starts a fresh one. The previous behaviour wedged the lock.
- Subprocesses run with ``UV_CACHE_DIR`` set to ``settings.uv_cache_root`` so
  plugins do not depend on the user's global uv cache (which is unreadable in
  some sandboxed environments).
- ``PluginError`` carries the tail of the plugin's stderr log so a "exited
  before responding" message includes whatever uv/Python actually said.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import IO, Any

from aidj.config import settings
from aidj.plugins.manifest import LoadedManifest

log = logging.getLogger(__name__)

_LOG_TAIL_LINES = 30
_KILL_GRACE_SEC = 2.0
_EOF_SENTINEL: object = None  # pushed onto the queue when the reader sees EOF


class PluginError(RuntimeError):
    """Raised when a plugin RPC call fails, times out, or the process exits."""

    def __init__(self, code: int, message: str, trace: str | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.trace = trace


class Plugin:
    """A long-lived plugin process accessed via newline-delimited JSON-RPC."""

    def __init__(
        self,
        loaded: LoadedManifest,
        *,
        default_timeout: float | None = None,
    ) -> None:
        self._loaded = loaded
        # Per-plugin default timeout comes from the manifest. ``default_timeout``
        # kwarg lets tests / future call sites override; otherwise we use what
        # the plugin author declared.
        self.default_timeout = (
            default_timeout if default_timeout is not None else loaded.manifest.default_timeout_sec
        )
        self._proc: subprocess.Popen[str] | None = None
        self._log_fp: IO[str] | None = None
        self._log_path: Path | None = None
        self._stdout_q: queue.Queue[str | None] | None = None
        self._reader_thread: threading.Thread | None = None
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
        log_fp, log_path = self._open_log()
        try:
            cmd = [
                "uv",
                "run",
                "--project",
                str(project_dir),
                "--python",
                m.python,
                "python",
                "-u",
                "-m",
                m.entrypoint_module,
            ]
            log.info("starting plugin %s@%s", self.name, self._loaded.version)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_fp,
                text=True,
                bufsize=1,
                env=self._build_env(),
            )
        except Exception:
            log_fp.close()
            raise

        self._log_fp = log_fp
        self._log_path = log_path
        self._proc = proc
        self._stdout_q = queue.Queue()
        self._reader_thread = threading.Thread(
            target=_reader_loop,
            args=(proc, self._stdout_q),
            daemon=True,
            name=f"plugin-{self.name}-reader",
        )
        self._reader_thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            if not self.is_alive:
                self._cleanup_after_exit()
                return
            assert self._proc is not None
            log.info("stopping plugin %s", self.name)
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("plugin %s did not exit in %.1fs; killing", self.name, timeout)
                self._proc.kill()
                self._proc.wait()
            finally:
                self._cleanup_after_exit()

    # -- RPC ---------------------------------------------------------------

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Invoke a method on the plugin and wait for its response.

        ``timeout`` defaults to ``self.default_timeout``. On timeout, the
        process is force-killed; the next ``call()`` starts a fresh one.
        """
        timeout = timeout if timeout is not None else self.default_timeout
        with self._lock:
            try:
                self._start_locked()
            except Exception as exc:
                raise PluginError(
                    -32002,
                    f"failed to start plugin '{self.name}': {exc}",
                    self._tail_log(),
                ) from exc

            assert (
                self._proc is not None
                and self._proc.stdin is not None
                and self._stdout_q is not None
            )

            self._next_id += 1
            req = {"id": self._next_id, "method": method, "params": params or {}}
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                tail = self._tail_log()
                self._force_kill_locked()
                raise PluginError(
                    -32099,
                    f"plugin '{self.name}' stdin closed: {exc}",
                    tail,
                ) from exc

            try:
                line = self._stdout_q.get(timeout=timeout)
            except queue.Empty:
                tail = self._tail_log()
                self._force_kill_locked()
                raise PluginError(
                    -32001,
                    f"plugin '{self.name}' timed out after {timeout:.1f}s on '{method}'",
                    tail,
                ) from None

            if line is None:
                rc = self._proc.poll() if self._proc is not None else None
                tail = self._tail_log()
                self._cleanup_after_exit()
                raise PluginError(
                    -32099,
                    f"plugin '{self.name}' exited (rc={rc}) before responding",
                    tail,
                )

            try:
                resp = json.loads(line)
            except json.JSONDecodeError as exc:
                tail = self._tail_log()
                self._force_kill_locked()
                raise PluginError(
                    -32700,
                    f"plugin '{self.name}' wrote non-JSON to stdout: {line.rstrip()!r}",
                    tail,
                ) from exc

            if not isinstance(resp, dict):
                tail = self._tail_log()
                self._force_kill_locked()
                raise PluginError(
                    -32600,
                    f"plugin '{self.name}' response is not a JSON object: {type(resp).__name__}",
                    tail,
                )

            # The SDK emits ``id=None`` only for parse errors on its own input.
            # For any *other* id mismatch the response is stale or buggy; tear
            # the plugin down so subsequent calls can't read leftover lines.
            resp_id = resp.get("id")
            if resp_id is not None and resp_id != self._next_id:
                tail = self._tail_log()
                self._force_kill_locked()
                raise PluginError(
                    -32603,
                    f"plugin '{self.name}' response id mismatch "
                    f"(got {resp_id!r}, expected {self._next_id})",
                    tail,
                )

            if "error" in resp:
                err = (
                    resp["error"]
                    if isinstance(resp["error"], dict)
                    else {"message": str(resp["error"])}
                )
                raise PluginError(
                    err.get("code", -1),
                    err.get("message", "unknown error"),
                    err.get("trace"),
                )

            if "result" not in resp:
                tail = self._tail_log()
                self._force_kill_locked()
                raise PluginError(
                    -32603,
                    f"plugin '{self.name}' response has neither 'result' nor 'error'",
                    tail,
                )

            return resp["result"]

    # -- internals (caller must hold _lock unless noted) -------------------

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Project-local uv cache by default. Plugins should not require access
        # to the user's global ~/.cache/uv (some sandboxes can't read it).
        env.setdefault("UV_CACHE_DIR", str(settings().uv_cache_root))
        return env

    def _open_log(self) -> tuple[IO[str], Path]:
        log_path = settings().logs_root / f"plugin-{self.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fp = log_path.open("a", encoding="utf-8")
        fp.write(f"\n--- starting {self.name}@{self._loaded.version} ---\n")
        fp.flush()
        return fp, log_path

    def _close_log(self) -> None:
        if self._log_fp is None:
            return
        with contextlib.suppress(Exception):
            self._log_fp.write(f"--- stopped {self.name} ---\n")
        with contextlib.suppress(Exception):
            self._log_fp.close()
        self._log_fp = None

    def _force_kill_locked(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.kill()
            with contextlib.suppress(Exception):
                self._proc.wait(timeout=_KILL_GRACE_SEC)
        self._cleanup_after_exit()

    def _cleanup_after_exit(self) -> None:
        self._close_log()
        self._proc = None
        self._stdout_q = None
        self._reader_thread = None
        self._log_path = None

    def _tail_log(self, lines: int = _LOG_TAIL_LINES) -> str | None:
        if self._log_path is None or not self._log_path.is_file():
            return None
        try:
            with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
                tail = collections.deque(f, maxlen=lines)
        except OSError:
            return None
        text = "".join(tail).strip()
        return text or None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _reader_loop(proc: subprocess.Popen[str], q: queue.Queue[str | None]) -> None:
    """Push every stdout line onto ``q``; signal EOF with ``None``."""
    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            q.put(line)
    finally:
        q.put(None)


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
        except Exception as exc:
            log.error("failed to load manifest at %s: %s", child, exc)
    return out
