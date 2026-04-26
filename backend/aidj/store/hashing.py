"""Stream-hashing for file content identity.

The track hash is sha256 of the raw bytes — stable across renames and moves.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1 << 20  # 1 MiB


def hash_file(path: Path | str, *, algo: str = "sha256") -> str:
    """Return the hex digest of the file's content."""
    h = hashlib.new(algo)
    p = Path(path)
    with p.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes, *, algo: str = "sha256") -> str:
    return hashlib.new(algo, data).hexdigest()


def derivation_key(parts: object) -> str:
    """Stable hash for derivation cache keys.

    Accepts any JSON-serialisable structure; produces a sha256 hex digest of its
    canonical JSON form. Used for keying cached artifacts (stems, intermediate
    renders) by their inputs.
    """
    import json

    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
