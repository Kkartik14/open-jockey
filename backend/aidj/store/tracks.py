"""Track repository — content-hash-keyed CRUD over the ``tracks`` table."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aidj.store import db
from aidj.store.hashing import hash_file
from aidj.store.models import Track

log = logging.getLogger(__name__)


# Keys callers may pass via ``probe`` to enrich a track. Anything else (including
# attempts to override identity columns like content_hash/source_path) is rejected.
# ``genre`` is here so a future tag-reading ingest pass can populate it directly;
# today it's set via ``set_genre`` from the API.
_PROBE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"duration_sec", "sample_rate", "channels", "bitrate", "genre"}
)


def _normalize_genre(genre: Any) -> str | None:
    """Normalise user/tag-sourced genres for consistent rollup buckets."""
    if genre is None:
        return None
    if not isinstance(genre, str):
        raise ValueError("genre must be a string or None")
    return genre.strip() or None


def ingest(path: Path | str, *, probe: dict[str, Any] | None = None) -> Track:
    """Hash a file and upsert it into the tracks table.

    ``probe`` may carry duration/sample_rate/channels/bitrate from a future
    audio-probe step. Other keys are rejected — including identity columns
    (``content_hash``/``source_path``/``file_size``/``format``), which the
    repository owns.
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)

    if probe:
        bad = set(probe) - _PROBE_ALLOWED_KEYS
        if bad:
            raise ValueError(
                f"probe contains keys that are not allowed: {sorted(bad)}; "
                f"allowed: {sorted(_PROBE_ALLOWED_KEYS)}"
            )

    fields: dict[str, Any] = {
        "content_hash": hash_file(p),
        "source_path": str(p),
        "file_size": p.stat().st_size,
        "format": p.suffix.lstrip(".").lower() or None,
    }
    if probe:
        fields.update(probe)
        if "genre" in fields:
            fields["genre"] = _normalize_genre(fields["genre"])

    cols = ",".join(fields.keys())
    placeholders = ",".join(["?"] * len(fields))
    update_clause = ",".join(
        f"{k}=excluded.{k}" for k in fields if k != "content_hash"
    )
    db.execute(
        f"INSERT INTO tracks ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(content_hash) DO UPDATE SET {update_clause}, last_seen=datetime('now')",
        tuple(fields.values()),
    )
    log.debug(
        "ingested track %s (%s, %d bytes)",
        fields["content_hash"][:12],
        fields["format"],
        fields["file_size"],
    )

    track = get(fields["content_hash"])
    if track is None:  # pragma: no cover — INSERT just succeeded
        raise RuntimeError(f"failed to read back ingested track {fields['content_hash']}")
    return track


def get(content_hash: str) -> Track | None:
    row = db.fetch_one("SELECT * FROM tracks WHERE content_hash=?", (content_hash,))
    return Track.from_row(row) if row else None


def list_all(*, limit: int = 1000) -> list[Track]:
    rows = db.fetch_all("SELECT * FROM tracks ORDER BY last_seen DESC LIMIT ?", (limit,))
    return [Track.from_row(r) for r in rows]


def delete(content_hash: str) -> bool:
    cur = db.execute("DELETE FROM tracks WHERE content_hash=?", (content_hash,))
    return cur.rowcount > 0


def set_genre(content_hash: str, genre: str | None) -> Track | None:
    """Update a track's genre; return the updated row or None if missing.

    Empty/whitespace strings normalise to NULL so the rollup groups them under
    a single ``(untagged)`` bucket rather than fragmenting the per-genre view.
    No casing normalisation — the user owns their taxonomy; if they want
    ``hip-hop`` and ``Hip-Hop`` to merge they can set them consistently.
    """
    cleaned = _normalize_genre(genre)
    cur = db.execute(
        "UPDATE tracks SET genre=? WHERE content_hash=?",
        (cleaned, content_hash),
    )
    if cur.rowcount == 0:
        return None
    return get(content_hash)
