"""SQLite connection + schema bootstrap + tiny query helpers.

Single-user, single-process app — sync sqlite3 is fine. We rely on
``check_same_thread=False`` plus FastAPI's threadpool handling for sync handlers.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from aidj.config import settings

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    content_hash TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    duration_sec REAL,
    sample_rate INTEGER,
    channels INTEGER,
    format TEXT,
    bitrate INTEGER,
    file_size INTEGER,
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_source_path ON tracks(source_path);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_hash TEXT NOT NULL REFERENCES tracks(content_hash) ON DELETE CASCADE,
    analyzer_name TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
    output_json TEXT,
    confidence REAL,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (track_hash, analyzer_name, analyzer_version)
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_track ON analysis_runs(track_hash);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_status ON analysis_runs(status);

CREATE TABLE IF NOT EXISTS stems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_hash TEXT NOT NULL REFERENCES tracks(content_hash) ON DELETE CASCADE,
    separator TEXT NOT NULL,
    separator_version TEXT NOT NULL,
    stem_name TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    size_bytes INTEGER,
    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (track_hash, separator, separator_version, stem_name)
);
CREATE INDEX IF NOT EXISTS idx_stems_cache_key ON stems(cache_key);
CREATE INDEX IF NOT EXISTS idx_stems_last_used ON stems(last_used_at);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    intent TEXT,
    plan_json TEXT,
    render_artifact_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_track TEXT NOT NULL REFERENCES tracks(content_hash),
    to_track TEXT NOT NULL REFERENCES tracks(content_hash),
    from_cue_bar INTEGER,
    to_cue_bar INTEGER,
    scores_json TEXT,
    allowed_techniques TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidates(project_id);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')) DEFAULT 'queued',
    retries INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    error TEXT,
    result_json TEXT,
    queued_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs(kind);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        s = settings()
        s.ensure_dirs()
        _conn = _connect(s.db_path)
        _bootstrap(_conn)
        log.debug("opened sqlite at %s (schema v%d)", s.db_path, SCHEMA_VERSION)
    return _conn


def _bootstrap(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def reset_for_tests(db_path: Path) -> None:
    """Re-target the global connection at a different DB. Used by test fixtures."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = _connect(db_path)
    _bootstrap(_conn)


def close() -> None:
    """Close and clear the global connection. Used by test teardown."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, params).fetchall()


def execute(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
    return get_conn().execute(sql, params)
