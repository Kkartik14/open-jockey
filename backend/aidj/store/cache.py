"""Content-addressed-style cache for derived artifacts (stems, intermediates).

Keys are arbitrary stable strings (typically sha256 hex digests of the inputs that
produced an artifact). Each cache entry lives at::

    <cache_root>/<kind>/<key[:2]>/<key[2:]>/<filename>

Splitting the first two hex chars keeps directory fanout reasonable for large
caches.
"""
from __future__ import annotations

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


def _layout(kind: str, key: str) -> Path:
    if len(key) < 3:
        raise ValueError(f"cache key too short: {key!r}")
    return settings().cache_root / kind / key[:2] / key[2:]


def path_for(kind: str, key: str, filename: str, *, create_parent: bool = True) -> Path:
    """Resolve the absolute path for an artifact. Optionally creates the directory."""
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
    try:
        parent.rmdir()
    except OSError:
        pass
    return count
