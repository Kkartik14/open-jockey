"""Shared SQLite-compatible UTC timestamp helper.

Both ``analysis_runs`` and ``track_profiles`` (and any future row that wants a
canonical ``started_at`` / ``built_at`` / etc.) call this so the on-disk format
stays consistent — SQLite's ``datetime('now')`` default produces the same
shape, which means string-comparison ordering (``MAX(finished_at)``, etc.)
works without parsing.

Keep this module dependency-free; it gets imported by everything in the store.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Matches SQLite's ``datetime('now')`` default — same shape used by the
# DEFAULT clauses in db.py's SCHEMA_SQL.
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def utc_now_iso() -> str:
    """UTC timestamp in SQLite's default format. Stable across the codebase."""
    return datetime.now(UTC).strftime(TIMESTAMP_FMT)
