"""Content-addressed-style cache for derived artifacts (stems, intermediates).

Keys are arbitrary stable strings (typically sha256 hex digests of the inputs that
produced an artifact). Each cache entry lives at::

    <cache_root>/<kind>/<key[:2]>/<key[2:]>/<filename>

Splitting the first two hex chars keeps directory fanout reasonable for large
caches.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

from aidj.config import settings


@dataclass(frozen=True)
class CacheRef:
    kind: str
    key: str
    filename: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.key}:{self.filename}"


# A safe cache-name segment: alphanumerics plus ``._-``. Excludes ``/``, ``\``,
# ``..``, control chars, and shell-meta characters. Conservative on purpose —
# cache callers (peaks, future demucs stems) only use hex digests / known
# fixed strings; nothing they pass should contain anything outside this set.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# ``.``/``..`` are syntactically inside the safe set but semantically traversal.
_FORBIDDEN_EXACT: frozenset[str] = frozenset({".", ".."})


def _validate_safe_name(label: str, value: str) -> None:
    """Reject inputs that could escape the cache root via path-join.

    Cache callers are internal today, but the cache is going to grow new
    consumers (demucs stems keyed off plugin output, projects keyed off
    user-supplied ids) — validating at the cache boundary is cheaper than
    auditing every future caller.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"cache {label} must be a non-empty string, got {value!r}")
    if value in _FORBIDDEN_EXACT or not _SAFE_NAME_RE.match(value):
        raise ValueError(
            f"cache {label} {value!r} contains characters not allowed in cache "
            "path segments — use [A-Za-z0-9._-] only"
        )


def _layout(kind: str, key: str) -> Path:
    _validate_safe_name("kind", kind)
    _validate_safe_name("key", key)
    if len(key) < 3:
        raise ValueError(f"cache key too short: {key!r}")

    parent = settings().cache_root / kind / key[:2] / key[2:]

    # Belt-and-suspenders: confirm the resolved path stays inside cache_root.
    # ``resolve(strict=False)`` works on non-existent paths and normalises any
    # ``..`` that slipped past the name check.
    root = settings().cache_root.resolve()
    resolved = parent.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"cache path escapes cache_root: {parent}")
    return parent


def path_for(kind: str, key: str, filename: str, *, create_parent: bool = True) -> Path:
    """Resolve the absolute path for an artifact. Optionally creates the directory."""
    _validate_safe_name("filename", filename)
    parent = _layout(kind, key)
    if create_parent:
        parent.mkdir(parents=True, exist_ok=True)
    return parent / filename


def exists(kind: str, key: str, filename: str) -> bool:
    return path_for(kind, key, filename, create_parent=False).is_file()


def put_bytes(kind: str, key: str, filename: str, data: bytes) -> Path:
    p = path_for(kind, key, filename)
    p.write_bytes(data)
    return p


def get_bytes(kind: str, key: str, filename: str) -> bytes | None:
    p = path_for(kind, key, filename, create_parent=False)
    if not p.is_file():
        return None
    return p.read_bytes()


def delete(kind: str, key: str, filename: str | None = None) -> int:
    """Delete one artifact, or the whole cache entry if filename is None.

    Returns the number of files removed.
    """
    if filename is not None:
        p = path_for(kind, key, filename, create_parent=False)
        if p.is_file():
            p.unlink()
            return 1
        return 0
    parent = _layout(kind, key)
    if not parent.is_dir():
        return 0
    count = 0
    for child in parent.iterdir():
        if child.is_file():
            child.unlink()
            count += 1
    with contextlib.suppress(OSError):
        parent.rmdir()
    return count
