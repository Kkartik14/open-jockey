"""Cache path containment.

The cache joins caller-supplied ``kind``/``key``/``filename`` segments under
``settings().cache_root``. Bad inputs from a future internal caller could
otherwise escape that root (``../../etc/passwd``). These tests assert the
validator rejects every escape path we can think of, and that the happy path
still produces a writable file under ``cache_root``.
"""
from __future__ import annotations

import pytest

from aidj.store import cache

HEX_KEY = "abcdef0123456789" * 4  # 64-char hex, the realistic shape


@pytest.mark.parametrize(
    "kind,key,filename",
    [
        ("peaks", HEX_KEY, "../escaped.json"),
        ("peaks", HEX_KEY, "sub/escaped.json"),
        ("peaks", HEX_KEY, "with\\backslash.json"),
        ("../peaks", HEX_KEY, "ok.json"),
        ("peaks/sub", HEX_KEY, "ok.json"),
        ("peaks", "..", "ok.json"),
        ("peaks", "../" + HEX_KEY[3:], "ok.json"),
        ("peaks", HEX_KEY, ".."),
        ("peaks", HEX_KEY, "."),
        ("peaks", HEX_KEY, ""),
        ("peaks", "", "ok.json"),
        ("", HEX_KEY, "ok.json"),
        # Shell-meta characters / spaces — conservative reject so a future
        # caller can't surprise us with quoting issues.
        ("peaks", HEX_KEY, "name with space.json"),
        ("peaks", HEX_KEY, "$(rm -rf /).json"),
    ],
)
def test_cache_rejects_unsafe_segments(tmp_aidj, kind: str, key: str, filename: str) -> None:
    with pytest.raises(ValueError):
        cache.path_for(kind, key, filename, create_parent=False)


def test_cache_happy_path_writes_under_cache_root(tmp_aidj) -> None:
    """A legitimate (hex key + safe filename) write lands inside cache_root and
    survives a read."""
    p = cache.put_bytes("peaks", HEX_KEY, "peaks-2048.json", b'{"ok":true}')
    assert p.read_bytes() == b'{"ok":true}'
    # Resolve both sides so symlink/realpath quirks (macOS /tmp → /private/tmp)
    # don't trip the containment check in tests.
    root = tmp_aidj.cache_root.resolve()
    assert root in p.resolve().parents


def test_cache_rejects_short_key(tmp_aidj) -> None:
    with pytest.raises(ValueError, match="cache key too short"):
        cache.path_for("peaks", "ab", "ok.json", create_parent=False)
